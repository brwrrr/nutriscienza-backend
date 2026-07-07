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
from datetime import datetime, timedelta, timezone
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

from . import affiliate, scheduler, storage
from .config import settings
from .email_sender import (
    send_admin_failure,
    send_base_upsell_email,
    send_cancellation_email,
    send_plan_email,
)
from .models import IntakeRequest
from .nutrition import compute_targets
from .pdf_builder import build_pdf
from .plan_generator import generate_meal_plan, generate_workout_plan
from .stripe_handlers import create_checkout_session, create_portal_session, verify_webhook


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
    allow_origins=[
        settings.base_url,
        "https://nutriscienza.org",
        "https://www.nutriscienza.org",
        "https://nutriscienza-frontend.onrender.com",
        "http://localhost:5173",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    storage.init_db()
    # IMPORTANTE: usa il path configurato (disco persistente in prod), NON un
    # path relativo hardcoded. Su Render il filesystem è effimero: senza disco
    # montato su questo path i PDF spariscono ad ogni redeploy (vedi DEPLOY.md).
    Path(settings.pdf_storage_dir).mkdir(parents=True, exist_ok=True)
    log.info(
        "NutriScienza backend avviato (env=%s, db=%s, pdf_dir=%s)",
        settings.environment, settings.database_path, settings.pdf_storage_dir,
    )


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

    # Validazione ref affiliate. Se invalido o disattivato, lo droppiamo
    # silenziosamente — un ref non valido NON deve impedire l'acquisto.
    validated_ref: str | None = None
    try:
        validated_ref = affiliate.validate_ref_for_checkout(payload.affiliate_ref)
    except Exception:
        log.exception("Errore validazione affiliate_ref — ignorato")

    traffic = {
        "utm_source": payload.utm_source,
        "utm_medium": payload.utm_medium,
        "utm_campaign": payload.utm_campaign,
        "referrer": payload.referrer,
        "landing_page": payload.landing_page,
    }
    order_id = storage.create_order(payload, targets, affiliate_ref=validated_ref, traffic=traffic)

    try:
        session = create_checkout_session(
            order_id, payload.plan, payload.email,
            affiliate_ref=validated_ref,
        )
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

        # ── Commissione affiliato (one-shot Base) ─────────────────────────
        # Per Base mode=payment NON c'è invoice → bookiamo qui.
        # Per subscription bookiamo su invoice.payment_succeeded (più sotto)
        # così l'idempotenza è ancorata all'invoice_id.
        try:
            session_meta = session.get("metadata") or {}
            ref = session_meta.get("affiliate_ref")
            plan = session_meta.get("plan")
            if ref and plan == "base":
                affiliate.record_commission_oneshot(
                    order_id=order_id,
                    affiliate_ref=ref,
                    plan=plan,
                    stripe_session_id=session.get("id"),
                    stripe_customer_id=session.get("customer"),
                    customer_email=(session.get("customer_details") or {}).get("email") or "",
                )
        except Exception:
            log.exception("[%s] commission one-shot fallita — ignorata", order_id)

    elif event["type"] in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")
        if order_id:
            storage.update_status(order_id, "failed",
                                  error=f"stripe: {event['type']}")

    elif event["type"] == "invoice.payment_succeeded":
        # Stripe fires this on every successful subscription renewal.
        # Percorso PRIMARIO del rinnovo: il pagamento è già avvenuto, ma NON
        # generiamo subito il piano. Apriamo un ciclo di check-in e invitiamo il
        # cliente ad aggiornare i dati; il piano viene generato quando completa il
        # check-in (vedi POST /api/checkin). Lo sweep giornaliero invia i solleciti
        # e fa da backstop se questo webhook si perde.
        invoice: dict[str, Any] = event["data"]["object"]
        sub_id = invoice.get("subscription")
        invoice_id = invoice.get("id")
        if sub_id:
            # billing_reason == 'subscription_create' means first payment — already
            # handled by checkout.session.completed (piano mese 1 dal questionario),
            # quindi nessun check-in per il primo mese.
            if invoice.get("billing_reason") != "subscription_create":
                storage.set_subscriber_status(sub_id, "active")
                log.info("Rinnovo confermato per subscription %s (inv=%s) — richiedo il check-in",
                         sub_id, invoice_id)
                # Apertura ciclo + email richiesta check-in in background (<5s a Stripe).
                # request_checkin_by_stripe_subscription è idempotente sull'invoice_id.
                background_tasks.add_task(
                    scheduler.request_checkin_by_stripe_subscription, sub_id, invoice_id
                )

                # Registra il ricavo del rinnovo per i totali della dashboard.
                # I rinnovi non creano ordini, quindi senza questo non comparirebbero.
                try:
                    sub_row = storage.get_subscriber_by_stripe_sub(sub_id)
                    plan = sub_row["plan"] if sub_row else "completo"
                    amount_paid = int(invoice.get("amount_paid") or 0)
                    if invoice_id and amount_paid > 0:
                        storage.record_renewal(
                            stripe_invoice_id=invoice_id,
                            stripe_subscription_id=sub_id,
                            plan=plan,
                            amount_cents=amount_paid,
                            currency=(invoice.get("currency") or "eur").lower(),
                        )
                except Exception:
                    log.exception("Registrazione rinnovo fallita inv=%s — ignorata", invoice_id)

        # ── Commissione affiliato (subscription) ──────────────────────────
        # Bookiamo per OGNI invoice paid (incluso il primo pagamento).
        # Idempotenza garantita dalla UNIQUE su stripe_event_ref=inv:<id>.
        try:
            invoice_id = invoice.get("id")
            amount_paid = int(invoice.get("amount_paid") or 0)
            if sub_id and invoice_id and amount_paid > 0:
                affiliate.record_commission_subscription(
                    stripe_invoice_id=invoice_id,
                    stripe_subscription_id=sub_id,
                    stripe_customer_id=invoice.get("customer"),
                    amount_paid_cents=amount_paid,
                    currency=(invoice.get("currency") or "eur").lower(),
                )
        except Exception:
            log.exception("commission subscription fallita inv=%s — ignorata", invoice.get("id"))

    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if sub_id:
            storage.set_subscriber_status(sub_id, "past_due")
            log.warning("Pagamento fallito per subscription %s", sub_id)

    elif event["type"] == "charge.refunded":
        # Reversal commissione affiliato. Best-effort: se non troviamo
        # match continuiamo silenziosamente (potrebbe essere un refund
        # su un acquisto pre-affiliate-program).
        try:
            charge: dict[str, Any] = event["data"]["object"]
            invoice_id = charge.get("invoice")
            charge_id = charge.get("id")
            n = affiliate.reverse_commission_for_charge(invoice_id, charge_id)
            if n:
                log.info("Reverse commission: %d riga/he aggiornate (charge=%s)", n, charge_id)
        except Exception:
            log.exception("Reverse commission fallita")

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

    elif event["type"] == "charge.refunded":
        # Reversal commissioni affiliate — best effort, mai blocca.
        try:
            charge: dict[str, Any] = event["data"]["object"]
            invoice_id = charge.get("invoice")
            charge_id = charge.get("id")
            n = affiliate.reverse_commission_for_charge(invoice_id, charge_id)
            if n:
                log.info("Reversed %d commission(s) per charge=%s", n, charge_id)
        except Exception:
            log.exception("Reversal commissioni fallito — ignorato")

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
    awaiting = bool(row["checkin_due"])
    # Mese del piano che verrà sbloccato dal check-in (se un ciclo è aperto).
    pending_month = row["pending_plan_month"] or (row["plan_month"] + 1)
    return {
        "first_name": row["first_name"],
        "current_weight": intake_data.get("weight"),
        "plan": row["plan"],
        "plan_month": row["plan_month"],
        # True = il cliente ha pagato il rinnovo e il piano è in attesa del check-in.
        "awaiting_checkin": awaiting,
        "pending_plan_month": pending_month,
    }


@app.post("/api/checkin/{token}")
def checkin_post(token: str, body: CheckinPayload, background_tasks: BackgroundTasks):
    """
    Applica il check-in mensile. Il token è monouso.

    Se era in attesa un ciclo di check-in (rinnovo pagato), questa è la chiamata
    che SBLOCCA la generazione del piano: la avviamo in background con la riga
    personale di progresso. Altrimenti aggiorniamo solo il peso per il futuro.
    """
    if body.weight < 30 or body.weight > 300:
        raise HTTPException(status_code=422, detail="Peso non valido (30–300 kg)")

    ctx = storage.apply_checkin(token, body.weight)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Link non valido o già utilizzato")

    if ctx["was_due"]:
        note = scheduler.compute_progress_note(
            ctx["old_weight"], ctx["new_weight"], ctx["goal"]
        )
        # Generazione pesante (Claude + PDF + email) in background.
        background_tasks.add_task(
            scheduler.refresh_subscriber,
            ctx["sub_id"],
            force=True,
            invoice_id=ctx["checkin_invoice_id"],
            reason="checkin",
            progress_note=note,
        )
        return {
            "success": True,
            "plan_pending": True,
            "message": "Grazie per il check-in! Stiamo generando il tuo nuovo piano — "
                       "arriva via email tra pochi minuti.",
        }

    return {
        "success": True,
        "plan_pending": False,
        "message": "Peso aggiornato. Il prossimo piano userà il nuovo valore.",
    }


# ---------- Billing Portal (self-service cancellation) ----------
#
# Stripe-hosted Customer Portal. Permette al subscriber di:
#   - annullare l'abbonamento (configurato "end-of-period" → coerente con T&C)
#   - aggiornare la carta (riduce involuntary churn da carta scaduta)
#   - scaricare le fatture
#
# Auth via checkin_token: stessa identità già usata dal flusso check-in mensile.
# Funziona solo per subscriber 'active' (post-cancellazione lo status filtra il token).

@app.post("/api/billing-portal/{token}")
def billing_portal(token: str):
    row = storage.get_subscriber_by_checkin_token(token)
    if not row:
        raise HTTPException(status_code=404, detail="Link non valido o abbonamento già cancellato")

    # Il piano 'base' è one-time — nessuna subscription da gestire.
    if row["plan"] == "base":
        raise HTTPException(status_code=400, detail="Il Piano Base è un acquisto unico, non c'è nulla da gestire.")

    customer_id = row["stripe_customer_id"]
    sub_id = row["stripe_subscription_id"]

    # Backfill per subscriber legacy senza customer_id su DB.
    if not customer_id and sub_id:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            customer_id = sub.get("customer")
            if customer_id:
                storage.update_subscriber_customer_id(row["id"], customer_id)
        except Exception:
            log.exception("Recupero customer_id Stripe fallito sub=%s", sub_id)

    if not customer_id:
        raise HTTPException(
            status_code=400,
            detail="Account Stripe non collegato — scrivi a supporto@nutriscienza.org",
        )

    try:
        session = create_portal_session(
            customer_id=customer_id,
            return_url=f"{settings.base_url}/checkin.html?token={token}",
        )
    except stripe.error.StripeError:
        log.exception("Portal session creation failed cust=%s", customer_id)
        raise HTTPException(status_code=502, detail="Errore Stripe — riprova tra poco.")

    return {"url": session.url}


# ---------- Admin ----------

@app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats():
    return {
        "orders_by_status": storage.count_orders_by_status(),
    }


# ---------- Dashboard ----------

# Listino prezzi (in centesimi) — fonte di verità per il calcolo ricavi nella dashboard.
# Allineato a quanto pubblicato sul landing index.html.
PLAN_PRICE_CENTS: dict[str, int] = {
    "base": 1900,       # €19 una tantum
    "completo": 2900,   # €29/mese
    "coach": 9900,      # €99/mese
}

# Stima costi variabili per ordine (in centesimi):
# - API Anthropic per generazione (tier-dependent)
# - Stripe fees: ~1.5% + €0.25 per transazione
# Queste cifre sono stime; per accounting reale incrocia con dashboard Stripe.
PLAN_API_COST_CENTS: dict[str, int] = {
    "base": 10,         # 1 chiamata pasti
    "completo": 50,     # 4 chiamate pasti + 1 workout
    "coach": 140,       # 12 chiamate pasti + 1 workout
}

STRIPE_FEE_PCT = 0.015
STRIPE_FEE_FIXED_CENTS = 25


def _stripe_fee_cents(amount_cents: int) -> int:
    return int(amount_cents * STRIPE_FEE_PCT) + STRIPE_FEE_FIXED_CENTS


def _aggregate_period(rows, renewal_rows=()) -> dict:
    """
    Aggrega ricavi e costi su una lista di ordini paid+ E sui rinnovi del periodo.

    Gli ordini usano il listino (PLAN_PRICE_CENTS); i rinnovi usano l'importo reale
    pagato (amount_cents da Stripe). Entrambi contribuiscono a ricavi, costi API,
    fee Stripe e profitto, così i totali includono gli abbonamenti ricorrenti.
    """
    revenue = 0
    api_cost = 0
    stripe_fee = 0
    by_plan: dict[str, int] = {}

    for r in rows:
        plan = r["plan_chosen"]
        price = PLAN_PRICE_CENTS.get(plan, 0)
        revenue += price
        api_cost += PLAN_API_COST_CENTS.get(plan, 0)
        stripe_fee += _stripe_fee_cents(price)
        by_plan[plan] = by_plan.get(plan, 0) + 1

    renewals_count = 0
    renewal_revenue = 0
    for r in renewal_rows:
        renewals_count += 1
        plan = r["plan"]
        amt = int(r["amount_cents"] or 0)
        revenue += amt
        renewal_revenue += amt
        api_cost += PLAN_API_COST_CENTS.get(plan, 0)
        stripe_fee += _stripe_fee_cents(amt)
        by_plan[plan] = by_plan.get(plan, 0) + 1

    total_cost = api_cost + stripe_fee
    return {
        "count": len(rows) + renewals_count,
        "orders_count": len(rows),
        "renewals_count": renewals_count,
        "renewal_revenue_cents": renewal_revenue,
        "revenue_cents": revenue,
        "api_cost_cents": api_cost,
        "stripe_fee_cents": stripe_fee,
        "total_cost_cents": total_cost,
        "profit_cents": revenue - total_cost,
        "by_plan": by_plan,
    }


@app.get("/api/admin/dashboard", dependencies=[Depends(require_admin)])
def admin_dashboard():
    """
    Snapshot operativo: ricavi/costi per oggi/settimana/mese, distribuzione tier,
    transazioni recenti. Fonte di verità per accounting interno.
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Lunedì = inizio settimana ISO
    week_start = (today_start - timedelta(days=now.weekday()))
    month_start = today_start.replace(day=1)

    rows_today = storage.get_paid_orders_since(today_start.isoformat())
    rows_week = storage.get_paid_orders_since(week_start.isoformat())
    rows_month = storage.get_paid_orders_since(month_start.isoformat())

    ren_today = storage.get_renewals_since(today_start.isoformat())
    ren_week = storage.get_renewals_since(week_start.isoformat())
    ren_month = storage.get_renewals_since(month_start.isoformat())

    plan_distribution = storage.count_orders_by_plan_paid()
    active_subscribers = storage.count_subscribers_by_plan_active()
    latest = storage.get_latest_paid_orders(limit=10)

    latest_transactions = []
    for r in latest:
        plan = r["plan_chosen"]
        latest_transactions.append({
            "id": r["id"],
            "plan": plan,
            "email": r["email"],
            "amount_cents": PLAN_PRICE_CENTS.get(plan, 0),
            "status": r["status"],
            "date": r["updated_at"],
        })
    # Includi anche i rinnovi così la lista riflette i totali.
    for r in storage.get_latest_renewals(limit=10):
        latest_transactions.append({
            "id": r["stripe_subscription_id"] or "—",
            "plan": r["plan"],
            "email": "↻ rinnovo",
            "amount_cents": int(r["amount_cents"] or 0),
            "status": "renewal",
            "date": r["created_at"],
        })
    latest_transactions.sort(key=lambda x: x["date"] or "", reverse=True)
    latest_transactions = latest_transactions[:10]

    return {
        "today": _aggregate_period(rows_today, ren_today),
        "week": _aggregate_period(rows_week, ren_week),
        "month": _aggregate_period(rows_month, ren_month),
        "plan_distribution_lifetime": plan_distribution,
        "active_subscribers_by_plan": active_subscribers,
        "latest_transactions": latest_transactions,
        "pricing_cents": PLAN_PRICE_CENTS,
    }


@app.get("/api/admin/orders", dependencies=[Depends(require_admin)])
def admin_orders(limit: int = 100, offset: int = 0):
    rows = storage.get_all_orders(limit=limit, offset=offset)
    return [dict(r) for r in rows]


@app.get("/api/admin/subscribers", dependencies=[Depends(require_admin)])
def admin_subscribers():
    rows = storage.get_all_subscribers()
    return [dict(r) for r in rows]


@app.post("/api/admin/subscribers/{sub_id}/send-plan", dependencies=[Depends(require_admin)])
def admin_send_plan(sub_id: str, background_tasks: BackgroundTasks):
    """
    Invio MANUALE del piano del mese successivo per un subscriber.
    Rete di sicurezza se un rinnovo non è stato consegnato in automatico.
    force=True: bypassa le guardie di idempotenza temporale/stato — l'admin
    sa cosa sta facendo. Lavoro pesante in background.
    """
    row = storage.get_subscriber_by_id(sub_id)
    if not row:
        raise HTTPException(status_code=404, detail="Subscriber non trovato")
    background_tasks.add_task(
        scheduler.refresh_subscriber, sub_id, force=True, reason="admin_manual"
    )
    return {"queued": True, "subscriber_id": sub_id, "email": row["email"]}


@app.post("/api/admin/subscribers/{sub_id}/request-checkin", dependencies=[Depends(require_admin)])
def admin_request_checkin(sub_id: str, background_tasks: BackgroundTasks):
    """
    Trigger MANUALE del flusso check-in: apre un ciclo e invia l'email di richiesta
    al cliente. Il piano verrà generato quando completa il check-in — esattamente
    come per un rinnovo. Usalo per (ri)avviare il loop senza forzare la consegna.
    """
    row = storage.get_subscriber_by_id(sub_id)
    if not row:
        raise HTTPException(status_code=404, detail="Subscriber non trovato")
    if row["plan"] not in ("completo", "coach"):
        raise HTTPException(
            status_code=400,
            detail="Il check-in mensile riguarda solo i piani in abbonamento (Completo/Coach).",
        )
    background_tasks.add_task(scheduler.request_checkin_for_subscriber, sub_id)
    return {"queued": True, "subscriber_id": sub_id, "email": row["email"]}


@app.post("/api/admin/run-refresh", dependencies=[Depends(require_admin)])
def admin_run_refresh(background_tasks: BackgroundTasks):
    """
    Esegue lo sweep giornaliero IN-PROCESS (stesso DB del web service).
    Da invocare da un Render Cron Job via HTTP (curl) invece di
    `python -m app.scheduler`, che girerebbe su un DB separato e vuoto.
    """
    background_tasks.add_task(scheduler.run)
    return {"queued": True}


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


@app.get("/api/admin/orders/{order_id}/pdf", dependencies=[Depends(require_admin)])
def admin_download_pdf(order_id: str):
    """Scarica il PDF generato per un ordine. Richiede stato 'sent' e file presente sul disco."""
    from fastapi.responses import FileResponse

    order = storage.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Ordine non trovato")
    if not order.pdf_path:
        raise HTTPException(status_code=404, detail="Nessun PDF associato a questo ordine")
    pdf_file = Path(order.pdf_path)
    if not pdf_file.exists():
        raise HTTPException(
            status_code=410,
            detail="File PDF mancante sul disco (probabilmente generato prima del disco persistente). Usa Retry per rigenerarlo."
        )
    # Filename leggibile per il download
    safe_name = "".join(c for c in order.intake.first_name if c.isalnum() or c in " -_").strip() or "cliente"
    download_name = f"NutriScienza-{order.plan_chosen}-{safe_name}-{order_id[:8]}.pdf"
    return FileResponse(
        path=str(pdf_file),
        media_type="application/pdf",
        filename=download_name,
    )


@app.delete("/api/admin/orders/{order_id}", dependencies=[Depends(require_admin)])
def admin_delete_order(order_id: str):
    """
    Hard-delete di un ordine (ripulisce test / ordini falliti). Rimuove anche il
    PDF dal disco se presente. Operazione irreversibile.
    """
    result = storage.delete_order(order_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Ordine non trovato")
    if result.get("pdf_path"):
        try:
            Path(result["pdf_path"]).unlink(missing_ok=True)
        except Exception:
            log.exception("Rimozione PDF fallita per ordine %s", order_id)
    return {"deleted": True, "order_id": order_id}


# ---------- Admin: clienti (profilo aggregato per email) ----------

class CustomerFieldUpdate(BaseModel):
    scope: str           # 'order' | 'subscriber'
    record_id: str
    fields: dict


@app.get("/api/admin/customers", dependencies=[Depends(require_admin)])
def admin_list_customers(search: str | None = None):
    rows = storage.list_customers(search=search)
    out = []
    for b in rows:
        spent = sum(PLAN_PRICE_CENTS.get(p, 0) for p in b["paid_plans"])
        out.append({
            "email": b["email"],
            "first_name": b["first_name"],
            "orders_count": b["orders_count"],
            "paid_count": b["paid_count"],
            "plans": b["plans"],
            "has_subscriber": b["has_subscriber"],
            "subscription_status": b["subscription_status"],
            "acquisition_spent_cents": spent,
            "last_activity": b["last_activity"],
        })
    return out


@app.get("/api/admin/customers/{email}", dependencies=[Depends(require_admin)])
def admin_customer_profile(email: str):
    import json as _json
    orders = storage.get_customer_orders(email)
    subs = storage.get_customer_subscribers(email)
    if not orders and not subs:
        raise HTTPException(status_code=404, detail="Cliente non trovato")

    sub_ids = [s["stripe_subscription_id"] for s in subs if s["stripe_subscription_id"]]
    renewals = storage.get_renewals_for_subscriptions(sub_ids)

    # ── Anagrafica + dati antropometrici ──
    # Preferiamo l'intake del subscriber (è il record che guida la generazione del
    # piano e il bersaglio dell'edit manuale), così vista e modifica coincidono.
    profile: dict | None = None
    first_name = None
    if subs:
        try:
            profile = _json.loads(subs[0]["intake_json"])
            first_name = profile.get("firstName")
        except Exception:
            profile = None
    if profile is None:
        for o in orders:
            try:
                profile = _json.loads(o["intake_json"])
                first_name = profile.get("firstName")
                break
            except Exception:
                continue
    if profile is None:
        profile = {}

    # ── Pagamenti (acquisizione + rinnovi) ──
    payments = []
    total_paid = 0
    for o in orders:
        if o["status"] in ("paid", "generating", "sent"):
            amt = PLAN_PRICE_CENTS.get(o["plan_chosen"], 0)
            total_paid += amt
            payments.append({
                "type": "ordine",
                "plan": o["plan_chosen"],
                "amount_cents": amt,
                "date": o["updated_at"],
                "ref": o["id"],
            })
    for r in renewals:
        amt = int(r["amount_cents"] or 0)
        total_paid += amt
        payments.append({
            "type": "rinnovo",
            "plan": r["plan"],
            "amount_cents": amt,
            "date": r["created_at"],
            "ref": r["stripe_invoice_id"],
        })
    payments.sort(key=lambda p: p["date"] or "", reverse=True)

    # ── Piani generati (PDF disponibili + mesi consegnati) ──
    plans = []
    for o in orders:
        plans.append({
            "order_id": o["id"],
            "plan": o["plan_chosen"],
            "status": o["status"],
            "pdf_available": bool(o["status"] == "sent" and o["pdf_path"]),
            "created_at": o["created_at"],
        })

    # ── Upsell Base → Completo ──
    # Individua l'ordine Base pagato più recente (già ordinati per created_at DESC)
    # e riporta lo stato dell'email di upsell. Se il cliente è già abbonato attivo,
    # l'upsell non è pertinente (non lo si ricontatta).
    latest_base = next(
        (o for o in orders
         if o["plan_chosen"] == "base"
         and o["status"] in ("paid", "generating", "sent")
         and not o["anonymized_at"]),
        None,
    )
    is_active_sub = any(s["subscription_status"] in ("active", "past_due") for s in subs)
    base_upsell = {
        "eligible": latest_base is not None and not is_active_sub,
        "has_base_order": latest_base is not None,
        "is_active_subscriber": is_active_sub,
        "order_id": latest_base["id"] if latest_base else None,
        "sent_at": latest_base["base_upsell_sent_at"] if latest_base else None,
        "purchase_date": latest_base["created_at"] if latest_base else None,
    }

    return {
        "email": email,
        "first_name": first_name,
        "profile": profile or {},
        "base_upsell": base_upsell,
        "orders": [{
            "id": o["id"], "plan": o["plan_chosen"], "status": o["status"],
            "created_at": o["created_at"], "updated_at": o["updated_at"],
            "pdf_available": bool(o["status"] == "sent" and o["pdf_path"]),
            "affiliate_ref": o["affiliate_ref"],
            "utm_source": o["utm_source"], "utm_medium": o["utm_medium"],
            "utm_campaign": o["utm_campaign"], "referrer": o["referrer"],
            "anonymized": bool(o["anonymized_at"]),
            "base_upsell_sent_at": o["base_upsell_sent_at"],
        } for o in orders],
        "subscribers": [{
            "id": s["id"], "plan": s["plan"], "status": s["subscription_status"],
            "plan_month": s["plan_month"], "next_plan_due_at": s["next_plan_due_at"],
            "checkin_due": bool(s["checkin_due"]) if "checkin_due" in s.keys() else False,
            "created_at": s["created_at"], "cancelled_at": s["cancelled_at"],
        } for s in subs],
        "payments": payments,
        "plans_generated": plans,
        "totals": {
            "paid_cents": total_paid,
            "payments_count": len(payments),
            "active_subscriber": any(s["subscription_status"] == "active" for s in subs),
        },
    }


@app.patch("/api/admin/customers/update", dependencies=[Depends(require_admin)])
def admin_customer_update(body: CustomerFieldUpdate):
    """Aggiornamento manuale di campi dell'intake (anagrafica / antropometrici)."""
    if body.scope not in ("order", "subscriber"):
        raise HTTPException(status_code=422, detail="scope deve essere 'order' o 'subscriber'")
    ok = storage.update_intake_fields(body.scope, body.record_id, body.fields)
    if not ok:
        raise HTTPException(status_code=404, detail="Record non trovato")
    return {"updated": True}


@app.post("/api/admin/customers/{email}/request-update", dependencies=[Depends(require_admin)])
def admin_customer_request_update(email: str, background_tasks: BackgroundTasks):
    """
    Invia la richiesta di aggiornamento mensile (check-in) a tutti gli abbonamenti
    attivi del cliente. Il piano verrà generato quando il cliente completa il check-in.
    """
    subs = storage.get_customer_subscribers(email)
    targets = [s for s in subs
               if s["subscription_status"] == "active" and s["plan"] in ("completo", "coach")]
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="Nessun abbonamento attivo (Completo/Coach) per questo cliente.",
        )
    for s in targets:
        background_tasks.add_task(scheduler.request_checkin_for_subscriber, s["id"])
    return {"queued": True, "subscribers": [s["id"] for s in targets]}


@app.post("/api/admin/customers/{email}/force-plan", dependencies=[Depends(require_admin)])
def admin_customer_force_plan(email: str, background_tasks: BackgroundTasks):
    """
    Forza la generazione del piano per il cliente:
      - se ha un abbonamento attivo → refresh forzato (genera + invia subito);
      - altrimenti → ri-processa l'ultimo ordine pagato (rigenera PDF + email).
    """
    subs = storage.get_customer_subscribers(email)
    active = [s for s in subs if s["subscription_status"] == "active"]
    if active:
        for s in active:
            background_tasks.add_task(
                scheduler.refresh_subscriber, s["id"], force=True, reason="admin_force_customer"
            )
        return {"queued": True, "mode": "subscriber_refresh",
                "subscribers": [s["id"] for s in active]}

    # Nessun subscriber attivo → rigenera l'ultimo ordine pagato.
    orders = storage.get_customer_orders(email)
    paid = [o for o in orders if o["status"] in ("paid", "generating", "sent")]
    if not paid:
        raise HTTPException(status_code=400, detail="Nessun ordine pagato da rigenerare.")
    target = paid[0]  # già ordinati per created_at DESC
    storage.update_status(target["id"], "paid")
    background_tasks.add_task(_run_generation_pipeline, target["id"])
    return {"queued": True, "mode": "order_regenerate", "order_id": target["id"]}


@app.post("/api/admin/customers/{email}/send-upsell", dependencies=[Depends(require_admin)])
def admin_customer_send_upsell(email: str):
    """
    Invio MANUALE dell'email di upsell Base → Completo per l'ordine Base pagato più
    recente del cliente. Bypassa la finestra 30–45 giorni (è un override intenzionale
    dell'admin) e funziona anche se lo sweep automatico l'ha già inviata (re-invio).
    Rifiuta solo se non c'è un ordine Base pagato o se il cliente è già abbonato attivo.
    """
    orders = storage.get_customer_orders(email)
    subs = storage.get_customer_subscribers(email)
    if any(s["subscription_status"] in ("active", "past_due") for s in subs):
        raise HTTPException(
            status_code=400,
            detail="Il cliente ha già un abbonamento attivo — l'upsell non è pertinente.",
        )
    base = next(
        (o for o in orders
         if o["plan_chosen"] == "base"
         and o["status"] in ("paid", "generating", "sent")
         and not o["anonymized_at"]),
        None,
    )
    if base is None:
        raise HTTPException(status_code=400, detail="Nessun ordine Base pagato per questo cliente.")

    was_already_sent = bool(base["base_upsell_sent_at"])
    try:
        intake = IntakeRequest.model_validate_json(base["intake_json"])
    except Exception:
        raise HTTPException(status_code=400, detail="Intake illeggibile — impossibile personalizzare l'email.")

    try:
        resend_id = send_base_upsell_email(email=intake.email, first_name=intake.first_name)
    except Exception as exc:
        log.exception("Invio upsell manuale fallito per %s", email)
        raise HTTPException(status_code=502, detail=f"Invio email fallito: {exc}")

    storage.mark_base_upsell_sent(base["id"])
    return {
        "sent": True,
        "order_id": base["id"],
        "was_already_sent": was_already_sent,
        "resend_id": resend_id,
    }


@app.delete("/api/admin/customers/{email}", dependencies=[Depends(require_admin)])
def admin_customer_gdpr_delete(email: str):
    """
    Cancellazione GDPR. Anonimizza gli ordini pagati (conserva importi/date per la
    contabilità), hard-delete degli ordini non pagati, anonimizza i subscriber.
    Rimuove i PDF dal disco. Irreversibile.
    """
    result = storage.gdpr_delete_customer(email)
    for p in result.get("pdf_paths", []):
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            log.exception("Rimozione PDF GDPR fallita: %s", p)
    return {
        "deleted": True,
        "orders_anonymized": result["orders_anonymized"],
        "orders_deleted": result["orders_deleted"],
        "active_subscribers": result["active_subscribers"],
        "warning": ("Ci sono abbonamenti ATTIVI: annullali anche su Stripe per "
                    "fermare la fatturazione.") if result["active_subscribers"] else None,
    }


# ---------- Admin: traffico e sorgenti ----------

@app.get("/api/admin/traffic", dependencies=[Depends(require_admin)])
def admin_traffic():
    """
    Sorgenti di traffico con conversioni e ricavo di acquisizione — per decidere
    dove investire in promo. I rinnovi ricorrenti NON sono inclusi: questa è la
    metrica di acquisizione (primo pagamento per sorgente).
    """
    by_source = [dict(r) for r in storage.traffic_by_source()]
    by_referrer = [dict(r) for r in storage.traffic_by_referrer()]
    totals = storage.traffic_totals()
    for r in by_source:
        r["conversion_pct"] = round((r["paid"] / r["signups"]) * 100, 1) if r["signups"] else 0.0
    return {
        "totals": totals,
        "by_source": by_source,
        "by_referrer": by_referrer,
    }


# ---------- Affiliate program ----------
# Tutto isolato: i fallimenti qui non toccano checkout, generazione, email.

class AffiliateCreate(BaseModel):
    name: str
    email: str
    commission_rate: float = 0.30
    payout_method: str = "manual"
    custom_code: str | None = None


class AffiliatePayout(BaseModel):
    affiliate_id: str
    method: str
    external_ref: str | None = None
    notes: str | None = None
    currency: str = "eur"


class AffiliateRateUpdate(BaseModel):
    commission_rate: float


@app.post("/api/admin/affiliates", dependencies=[Depends(require_admin)])
def admin_create_affiliate(payload: AffiliateCreate):
    try:
        result = affiliate.create_affiliate(
            name=payload.name,
            email=payload.email,
            payout_method=payload.payout_method,
            commission_rate=payload.commission_rate,
            custom_code=payload.custom_code,
        )
        # Includi il portal_token così l'admin può inviarlo all'affiliato per il login
        aff = storage.get_affiliate_by_id(result["id"])
        return {
            **result,
            "portal_login_url": f"{settings.base_url}/affiliate.html?t={aff['portal_token']}",
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/admin/affiliates", dependencies=[Depends(require_admin)])
def admin_list_affiliates():
    rows = storage.list_affiliates()
    out = []
    for r in rows:
        d = dict(r)
        stats = affiliate.affiliate_stats(d["id"])
        out.append({**d, **stats})
    return out


@app.post("/api/admin/affiliates/{aff_id}/status", dependencies=[Depends(require_admin)])
def admin_update_affiliate_status(aff_id: str, status: str):
    if status not in ("active", "paused", "banned"):
        raise HTTPException(status_code=422, detail="Status non valido")
    storage.update_affiliate_status(aff_id, status)
    return {"ok": True}


@app.delete("/api/admin/affiliates/{aff_id}", dependencies=[Depends(require_admin)])
def admin_delete_affiliate(aff_id: str):
    """
    Hard-delete di un affiliato + cascata su commissioni e payout (per rimuovere
    affiliati di test). Irreversibile: distrugge anche lo storico interno delle
    commissioni. Non storna bonifici reali già eseguiti.
    """
    result = storage.delete_affiliate(aff_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Affiliate non trovato")
    return {"deleted": True, **result}


@app.post("/api/admin/affiliates/{aff_id}/commission-rate", dependencies=[Depends(require_admin)])
def admin_update_affiliate_commission_rate(aff_id: str, body: AffiliateRateUpdate):
    """Adjust the affiliate's commission rate. Stored as fraction (0.30 = 30%)."""
    aff = storage.get_affiliate_by_id(aff_id)
    if not aff:
        raise HTTPException(status_code=404, detail="Affiliate non trovato")
    try:
        storage.update_affiliate_commission_rate(aff_id, body.commission_rate)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "commission_rate": body.commission_rate}


@app.post("/api/admin/affiliates/approve-matured", dependencies=[Depends(require_admin)])
def admin_approve_matured():
    """
    Promuove le commissioni 'pending' col payable_at scaduto a 'approved'.
    Da chiamare manualmente o via cron giornaliero.
    """
    n = affiliate.approve_matured_commissions()
    return {"approved": n}


@app.get("/api/admin/affiliates/payouts-due", dependencies=[Depends(require_admin)])
def admin_payouts_due():
    """Snapshot di tutti gli affiliati con commissioni 'approved' in attesa di pagamento."""
    rows = storage.list_all_approved_commissions_grouped()
    return [dict(r) for r in rows]


@app.post("/api/admin/affiliates/payouts", dependencies=[Depends(require_admin)])
def admin_record_payout(body: AffiliatePayout):
    """
    Crea un payout e marca tutte le commissioni 'approved' di quell'affiliato come 'paid'.
    Atomica nel DB: o tutto o niente.
    """
    aff = storage.get_affiliate_by_id(body.affiliate_id)
    if not aff:
        raise HTTPException(status_code=404, detail="Affiliate non trovato")
    rows = storage.list_approved_commissions_for_payout(body.affiliate_id)
    rows = [r for r in rows if (r["currency"] or "eur").lower() == body.currency.lower()]
    if not rows:
        raise HTTPException(status_code=400, detail="Nessuna commission approved da pagare")
    total = sum(int(r["commission_amount_cents"]) for r in rows)
    payout_id = storage.create_payout_and_mark_paid(
        affiliate_id=body.affiliate_id,
        amount_cents=total,
        currency=body.currency.lower(),
        method=body.method,
        external_ref=body.external_ref,
        notes=body.notes,
    )
    return {"payout_id": payout_id, "amount_cents": total, "commissions_paid": len(rows)}


# ---------- Affiliate portal (pubblico, autenticato via token) ----------

def _affiliate_from_token(token: str | None) -> Any:
    if not token:
        raise HTTPException(status_code=401, detail="Token mancante")
    aff = storage.get_affiliate_by_portal_token(token)
    if not aff:
        raise HTTPException(status_code=401, detail="Token non valido")
    return aff


@app.get("/api/affiliate/me")
@limiter.limit("60/minute")
def affiliate_me(request: Request, t: str | None = None):
    aff = _affiliate_from_token(t)
    stats = affiliate.affiliate_stats(aff["id"])
    return {
        "id": aff["id"],
        "name": aff["name"],
        "email": aff["email"],
        "ref_code": aff["ref_code"],
        "referral_url": f"{settings.base_url}/?ref={aff['ref_code']}",
        "commission_rate": aff["commission_rate"],
        "status": aff["status"],
        "stats": stats,
    }


@app.get("/api/affiliate/commissions")
@limiter.limit("60/minute")
def affiliate_commissions(request: Request, t: str | None = None):
    aff = _affiliate_from_token(t)
    rows = storage.list_commissions_for_affiliate(aff["id"])
    return [dict(r) for r in rows]


@app.get("/api/affiliate/payouts")
@limiter.limit("60/minute")
def affiliate_payouts(request: Request, t: str | None = None):
    aff = _affiliate_from_token(t)
    rows = storage.list_payouts_for_affiliate(aff["id"])
    return [dict(r) for r in rows]


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

        # 1. Genera il piano alimentare via Claude (1 / 4 / 12 settimane in base al tier)
        log.info("[%s] richiesta piano alimentare a Claude (tier=%s)", order_id, order.intake.plan)
        meal_plan = generate_meal_plan(order.intake, order.targets)

        # 1b. Per Completo e Coach genera anche il programma di allenamento
        workout_plan = None
        if order.intake.plan in ("completo", "coach"):
            log.info("[%s] richiesta programma di allenamento a Claude", order_id)
            workout_plan = generate_workout_plan(order.intake, order.targets)

        # 2. Costruisci il PDF (su disco persistente in prod)
        pdf_path = f"{settings.pdf_storage_dir.rstrip('/')}/{order_id}.pdf"
        log.info("[%s] costruisco PDF -> %s", order_id, pdf_path)
        build_pdf(order.intake, order.targets, meal_plan, pdf_path, workout=workout_plan)

        # 3. Per i piani ricorrenti creiamo PRIMA il subscriber, così possiamo
        #    iniettare il link "Gestisci abbonamento" già nella welcome email
        #    (compliance Codice del Consumo art. 49 + allineamento T&C).
        manage_url = ""
        if order.plan_chosen in ("completo", "coach"):
            fresh = storage.get_order(order_id)
            aff_ref = None
            try:
                aff_ref = storage.get_order_affiliate_ref(order_id)
            except Exception:
                log.exception("[%s] lookup affiliate_ref fallito — ignorato", order_id)
            sub_id = storage.create_subscriber(
                order_id=order_id,
                intake=order.intake,
                targets=order.targets,
                stripe_subscription_id=getattr(fresh, "stripe_subscription_id", None) if fresh else None,
                stripe_customer_id=getattr(fresh, "stripe_customer_id", None) if fresh else None,
                affiliate_ref=aff_ref,
            )
            log.info("[%s] subscriber creato: %s (prossimo piano in ~30 giorni)", order_id, sub_id)
            sub_row = storage.get_subscriber_by_stripe_sub(
                getattr(fresh, "stripe_subscription_id", None) if fresh else None
            )
            if sub_row and sub_row["checkin_token"]:
                manage_url = f"{settings.base_url}/checkin.html?token={sub_row['checkin_token']}"

        # 4. Invia email con allegato (manage_url vuoto per Piano Base)
        log.info("[%s] invio email a %s", order_id, order.email)
        email_id = send_plan_email(order.intake, pdf_path, manage_url=manage_url)
        log.info("[%s] email inviata (resend_id=%s)", order_id, email_id)

        storage.update_status(order_id, "sent", pdf_path=pdf_path)
        log.info("[%s] pipeline completata", order_id)

    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log.exception("[%s] pipeline fallita", order_id)
        storage.update_status(order_id, "failed", error=err)
        send_admin_failure(order_id, err)
