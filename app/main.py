"""
FastAPI — punto di ingresso del backend NutriScienza.

Endpoint pubblici:
  POST  /api/intake                  — riceve il questionario, calcola i target, crea Checkout
  POST  /api/stripe/webhook          — riceve gli eventi Stripe (firma verificata)
  GET   /api/orders/{order_id}       — stato ordine per pagina /grazie (polling)
  GET   /api/checkin/{token}         — profilo subscriber per form check-in
  POST  /api/checkin/{token}         — aggiorna peso subscriber
  GET   /healthz                     — health check

Endpoint admin (Bearer ADMIN_API_KEY):
  GET   /api/admin/stats             — statistiche aggregate
  GET   /api/admin/orders            — lista ordini
  GET   /api/admin/subscribers       — lista subscriber
  POST  /api/admin/orders/{id}/retry — riprocessa ordine fallito
"""
import logging
import traceback
from pathlib import Path
from typing import Any

import sentry_sdk
import stripe
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import storage
from .config import settings
from .email_sender import send_admin_failure, send_cancellation_email, send_plan_email
from .models import IntakeRequest
from .nutrition import compute_targets
from .pdf_builder import build_pdf
from .plan_generator import generate_meal_plan
from .stripe_handlers import create_checkout_session, verify_webhook


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nutriscienza")

# ── Sentry ─────────────────────────────────────────────────────────────────────
if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=0.1,
    )
    log.info("Sentry attivato (env=%s)", settings.environment)

# ── Rate limiting ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="NutriScienza API", version="0.2.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Admin auth ─────────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)


def require_admin(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="Admin non configurato")
    if credentials is None or credentials.credentials != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Non autorizzato")

# CORS — in produzione restringere a domini frontend reali
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.base_url, "http://localhost:5173", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    storage.init_db()
    Path("./data/pdfs").mkdir(parents=True, exist_ok=True)
    log.info("NutriScienza backend avviato (env=%s)", settings.environment)


# ---------- Health ----------

@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": "0.1.0", "environment": settings.environment}


# ---------- Intake ----------

@app.post("/api/intake")
@limiter.limit("5/minute")
def intake(request: Request, payload: IntakeRequest):
    """
    Riceve il questionario completato, calcola i target nutrizionali, crea l'ordine
    e una Checkout Session Stripe. Ritorna l'URL del checkout.
    """
    targets = compute_targets(payload)
    order_id = storage.create_order(payload, targets)

    try:
        session = create_checkout_session(order_id, payload.plan, payload.email)
    except stripe.error.StripeError as e:
        log.exception("Stripe checkout fallito per ordine %s", order_id)
        storage.update_status(order_id, "failed", error=f"stripe: {e}")
        raise HTTPException(status_code=502, detail="Errore creazione checkout")

    storage.attach_session(order_id, session.id)

    return {
        "order_id": order_id,
        "checkout_url": session.url,
        "session_id": session.id,
        # Echo dei target per la pagina di conferma (non sono dati segreti)
        "targets_preview": {
            "target_kcal": targets.target_kcal,
            "protein_g": targets.protein_g,
            "carbs_g": targets.carbs_g,
            "fat_g": targets.fat_g,
        },
    }


# ---------- Stripe webhook ----------

@app.post("/api/stripe/webhook")
async def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    """
    Stripe POSTs eventi qui. Verifichiamo firma, e se è un pagamento riuscito
    triggeriamo la pipeline di generazione in background.

    Stripe aspetta una risposta < 5s — quindi NON facciamo lavoro pesante qui.
    """
    if stripe_signature is None:
        raise HTTPException(status_code=400, detail="Manca header Stripe-Signature")

    payload = await request.body()

    try:
        event = verify_webhook(payload, stripe_signature)
    except (stripe.error.SignatureVerificationError, ValueError) as e:
        log.warning("Webhook firma invalida: %s", e)
        raise HTTPException(status_code=400, detail="Firma non valida")

    log.info("Stripe event: %s (id=%s)", event["type"], event["id"])

    if event["type"] == "checkout.session.completed":
        session: dict[str, Any] = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")
        if not order_id:
            log.error("checkout.session.completed senza order_id in metadata: %s", session.get("id"))
            return JSONResponse({"received": True, "warning": "no order_id"})

        # Salva IDs Stripe sull'ordine (subscription + customer per i piani ricorrenti)
        storage.update_stripe_ids(
            order_id,
            stripe_subscription_id=session.get("subscription"),
            stripe_customer_id=session.get("customer"),
        )

        # Il pagamento è andato a buon fine — aggiorna stato e fai partire la pipeline
        storage.update_status(order_id, "paid")
        background_tasks.add_task(_run_generation_pipeline, order_id)

    elif event["type"] in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")
        if order_id:
            storage.update_status(order_id, "failed",
                                  error=f"stripe: {event['type']}")

    elif event["type"] == "invoice.payment_succeeded":
        # Stripe fires this on every successful subscription renewal.
        # We use it to keep subscriber status in sync. The actual plan
        # regeneration is handled by scheduler.py (cron), not here — this
        # keeps the webhook handler fast and simple.
        invoice: dict[str, Any] = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if sub_id:
            # billing_reason == 'subscription_create' means first payment — already
            # handled by checkout.session.completed, so skip to avoid double-generating.
            if invoice.get("billing_reason") != "subscription_create":
                storage.set_subscriber_status(sub_id, "active")
                log.info("Rinnovo confermato per subscription %s — cron genererà il piano", sub_id)

    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if sub_id:
            storage.set_subscriber_status(sub_id, "past_due")
            log.warning("Pagamento fallito per subscription %s", sub_id)

    elif event["type"] == "customer.subscription.deleted":
        sub: dict[str, Any] = event["data"]["object"]
        sub_id = sub.get("id")
        if sub_id:
            # Recupera info subscriber prima di cancellarlo
            sub_row = storage.get_subscriber_by_stripe_sub(sub_id)
            storage.cancel_subscriber(sub_id)
            log.info("Subscription cancellata: %s", sub_id)
            if sub_row:
                send_cancellation_email(
                    email=sub_row["email"],
                    first_name=sub_row["first_name"],
                    plan=sub_row["plan"],
                )

    return {"received": True}


# ---------- Status ----------

@app.get("/api/orders/{order_id}")
def get_order_status(order_id: str):
    order = storage.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Ordine non trovato")
    # Esponiamo solo info necessarie alla pagina /grazie
    return {
        "id": order.id,
        "status": order.status,
        "plan": order.plan_chosen,
        "email": order.email,
        "first_name": order.intake.first_name,
    }


# ---------- Check-in mensile ----------

class CheckinPayload(BaseModel):
    weight: float  # kg


@app.get("/api/checkin/{token}")
def checkin_get(token: str):
    """Ritorna le info del subscriber per pre-popolare il form di check-in."""
    row = storage.get_subscriber_by_checkin_token(token)
    if not row:
        raise HTTPException(status_code=404, detail="Link non valido o abbonamento cancellato")
    import json as _json
    intake_data = _json.loads(row["intake_json"])
    return {
        "first_name": row["first_name"],
        "current_weight": intake_data.get("weight"),
        "plan": row["plan"],
        "plan_month": row["plan_month"],
    }


@app.post("/api/checkin/{token}")
def checkin_post(token: str, body: CheckinPayload):
    """Aggiorna il peso corrente del subscriber. Il token è monouso."""
    if body.weight < 30 or body.weight > 300:
        raise HTTPException(status_code=422, detail="Peso non valido (30–300 kg)")
    ok = storage.update_subscriber_weight(token, body.weight)
    if not ok:
        raise HTTPException(status_code=404, detail="Link non valido o già utilizzato")
    return {"success": True, "message": "Peso aggiornato. Il prossimo piano userà il nuovo valore."}


# ---------- Admin ----------

@app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats():
    return {
        "orders_by_status": storage.count_orders_by_status(),
    }


@app.get("/api/admin/orders", dependencies=[Depends(require_admin)])
def admin_orders(limit: int = 100, offset: int = 0):
    rows = storage.get_all_orders(limit=limit, offset=offset)
    return [dict(r) for r in rows]


@app.get("/api/admin/subscribers", dependencies=[Depends(require_admin)])
def admin_subscribers():
    rows = storage.get_all_subscribers()
    return [dict(r) for r in rows]


@app.post("/api/admin/orders/{order_id}/retry", dependencies=[Depends(require_admin)])
def admin_retry(order_id: str, background_tasks: BackgroundTasks):
    """Riprocessa un ordine fallito (pipeline generazione + email)."""
    order = storage.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Ordine non trovato")
    if order.status not in ("failed", "paid", "generating"):
        raise HTTPException(
            status_code=400,
            detail=f"Retry disponibile solo per ordini failed/paid/generating (stato attuale: {order.status})"
        )
    storage.update_status(order_id, "paid")  # reset per far ripartire la pipeline
    background_tasks.add_task(_run_generation_pipeline, order_id)
    return {"queued": True, "order_id": order_id}


# ---------- Pipeline di generazione ----------

def _run_generation_pipeline(order_id: str) -> None:
    """
    Eseguito in background dopo `checkout.session.completed`.
    Non solleva eccezioni — registra fallimenti su DB e notifica admin.
    """
    log.info("[%s] start pipeline", order_id)
    try:
        order = storage.get_order(order_id)
        if not order:
            log.error("[%s] ordine non trovato", order_id)
            return

        storage.update_status(order_id, "generating")

        # 1. Genera il piano via Claude
        log.info("[%s] richiesta piano a Claude", order_id)
        meal_plan = generate_meal_plan(order.intake, order.targets)

        # 2. Costruisci il PDF
        pdf_path = f"./data/pdfs/{order_id}.pdf"
        log.info("[%s] costruisco PDF -> %s", order_id, pdf_path)
        build_pdf(order.intake, order.targets, meal_plan, pdf_path)

        # 3. Invia email con allegato
        log.info("[%s] invio email a %s", order_id, order.email)
        email_id = send_plan_email(order.intake, pdf_path)
        log.info("[%s] email inviata (resend_id=%s)", order_id, email_id)

        storage.update_status(order_id, "sent", pdf_path=pdf_path)
        log.info("[%s] pipeline completata", order_id)

        # Per i piani ricorrenti (completo / coach) creiamo il record subscriber
        # così il cron sa quando rigenerare il piano il mese prossimo.
        if order.plan_chosen in ("completo", "coach"):
            # Recupera i Stripe ID salvati sull'ordine dal webhook
            fresh = storage.get_order(order_id)
            sub_id = storage.create_subscriber(
                order_id=order_id,
                intake=order.intake,
                targets=order.targets,
                stripe_subscription_id=getattr(fresh, "stripe_subscription_id", None) if fresh else None,
                stripe_customer_id=getattr(fresh, "stripe_customer_id", None) if fresh else None,
            )
            log.info("[%s] subscriber creato: %s (prossimo piano in ~30 giorni)", order_id, sub_id)

    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log.exception("[%s] pipeline fallita", order_id)
        storage.update_status(order_id, "failed", error=err)
        send_admin_failure(order_id, err)
