"""
scheduler.py — Cron job per il rinnovo mensile dei piani Piano Completo / Coach.

Come funziona
─────────────
Viene eseguito OGNI GIORNO (es. alle 07:00 UTC). Controlla quali subscriber
hanno next_plan_due_at <= adesso e, per ognuno, esegue la pipeline:
  1. Richiede un nuovo piano a Claude (con gli stessi dati di intake)
  2. Costruisce il PDF
  3. Invia l'email "Mese X è pronto" con il PDF allegato
  4. Aggiorna next_plan_due_at = ora + 30 giorni

Se la generazione per un subscriber fallisce, logga l'errore e notifica
l'admin — ma continua con i subscriber successivi senza far saltare il run.

Come eseguirlo
──────────────
  # Manuale (debug):
  cd backend && python -m app.scheduler

  # Su Render (Cron Job):
  Command: python -m app.scheduler
  Schedule: 0 7 * * *        (ogni giorno alle 07:00 UTC)

  # Su Fly.io (machines cron):
  fly cron add --schedule "0 7 * * *" -- python -m app.scheduler
"""

import logging
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings
from .email_sender import send_admin_failure, send_refresh_plan_email
from .models import IntakeRequest, NutritionTargets
from .nutrition import compute_targets
from .pdf_builder import build_pdf
from .plan_generator import generate_meal_plan, generate_workout_plan
from . import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [scheduler] %(message)s",
)
log = logging.getLogger("nutriscienza.scheduler")


# Finestra di guardia: se un piano è già stato inviato negli ultimi N giorni,
# un secondo trigger NON ricarica il piano (evita doppio invio quando il
# webhook del rinnovo e lo sweep giornaliero si sovrappongono). Il pulsante
# manuale dell'admin usa force=True e bypassa questa guardia.
RECENT_SEND_GUARD_DAYS = 25


def refresh_subscriber(
    sub_id: str,
    *,
    force: bool = False,
    invoice_id: str | None = None,
    reason: str = "cron",
) -> str:
    """
    Genera e invia il piano del mese successivo per UN subscriber.

    Punto d'ingresso unico condiviso da: webhook rinnovo Stripe, sweep
    giornaliero (cron/endpoint) e pulsante manuale admin. Gestisce da sé
    errori e idempotenza — non solleva mai (sicuro come BackgroundTask).

    Ritorna uno stato: "sent" | "failed" | "skipped:not_found"
    | "skipped:inactive" | "skipped:duplicate" | "skipped:recent".
    """
    row = storage.get_subscriber_by_id(sub_id)
    if row is None:
        log.warning("refresh_subscriber: subscriber %s non trovato (reason=%s)", sub_id, reason)
        return "skipped:not_found"

    email: str = row["email"]
    plan_month: int = row["plan_month"] + 1  # next month to send

    # Idempotenza forte: stesso invoice Stripe già fulfillato → skip.
    if invoice_id and row["last_invoice_id"] == invoice_id:
        log.info("[%s] invoice %s già processato — skip (reason=%s)", sub_id, invoice_id, reason)
        return "skipped:duplicate"

    if not force:
        if row["subscription_status"] != "active":
            log.info("[%s] status=%s non attivo — skip (reason=%s)",
                     sub_id, row["subscription_status"], reason)
            return "skipped:inactive"

        # Idempotenza temporale: piano già inviato di recente in questo ciclo.
        last_sent = row["last_plan_sent_at"]
        if last_sent:
            try:
                last_dt = datetime.fromisoformat(last_sent)
                if datetime.now(timezone.utc) - last_dt < timedelta(days=RECENT_SEND_GUARD_DAYS):
                    log.info("[%s] piano già inviato il %s (<%dgg) — skip (reason=%s)",
                             sub_id, last_sent, RECENT_SEND_GUARD_DAYS, reason)
                    return "skipped:recent"
            except ValueError:
                pass  # data malformata → procedi col refresh

    log.info("[%s] inizio refresh — %s (mese %d, reason=%s, force=%s)",
             sub_id, email, plan_month, reason, force)
    try:
        _deliver_plan(row, plan_month)
        storage.mark_plan_sent(sub_id, invoice_id=invoice_id)
        log.info("[%s] ✓ piano mese %d inviato a %s", sub_id, plan_month, email)
        return "sent"
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        log.error("[%s] ✗ refresh fallito per %s:\n%s", sub_id, email, err_text)
        try:
            send_admin_failure(
                order_id=f"sub:{sub_id}",
                error=f"Refresh mese {plan_month} fallito per {email} (reason={reason})\n\n{err_text}",
            )
        except Exception:
            log.exception("[%s] impossibile inviare notifica admin", sub_id)
        return "failed"


def refresh_by_stripe_subscription(stripe_sub_id: str, invoice_id: str | None = None) -> str:
    """
    Mappa una subscription Stripe al subscriber interno e ne fa il refresh.
    Usato dal webhook `invoice.payment_succeeded` sui rinnovi: il cliente ha
    appena pagato → consegniamo subito il piano, senza dipendere dal cron.
    """
    row = storage.get_subscriber_by_stripe_sub(stripe_sub_id)
    if row is None:
        log.warning("refresh_by_stripe_subscription: nessun subscriber per sub %s", stripe_sub_id)
        return "skipped:not_found"
    return refresh_subscriber(row["id"], invoice_id=invoice_id, reason="stripe_renewal")


def run() -> dict[str, int]:
    """
    Entry point dello sweep giornaliero (cron / endpoint admin).

    Backstop self-healing: processa ogni subscriber attivo con
    next_plan_due_at <= adesso non ancora servito in questo ciclo.
    Il percorso primario è il webhook del rinnovo Stripe.

    Ritorna un dizionario con i contatori del run:
        {"due": N, "success": N, "skipped": N, "failed": N}
    """
    storage.init_db()

    due_rows = storage.get_subscribers_due_for_refresh()
    log.info("Subscriber in scadenza oggi: %d", len(due_rows))

    counts = {"due": len(due_rows), "success": 0, "skipped": 0, "failed": 0}

    for row in due_rows:
        status = refresh_subscriber(row["id"], reason="cron")
        if status == "sent":
            counts["success"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["skipped"] += 1

    # ── Affiliate: promuovi le commissioni mature ────────────────────────
    # Pending → approved per quelle col payable_at scaduto (30gg di hold).
    # Best-effort: un fallimento qui non deve compromettere il run principale.
    try:
        from . import affiliate as _affiliate
        approved = _affiliate.approve_matured_commissions()
        if approved:
            log.info("Commissioni affiliate promosse a 'approved': %d", approved)
        counts["affiliate_approved"] = approved
    except Exception:
        log.exception("Approvazione commissioni mature fallita — ignorata")
        counts["affiliate_approved"] = 0

    log.info(
        "Run completato — %d processati, %d successi, %d falliti, %d saltati",
        counts["due"], counts["success"], counts["failed"], counts["skipped"],
    )
    return counts


def _deliver_plan(row: sqlite3.Row, plan_month: int) -> None:
    """
    Pipeline completa per un singolo subscriber: rigenera piano, costruisce PDF,
    invia email. NON aggiorna il DB — è il chiamante (refresh_subscriber) a
    chiamare mark_plan_sent così da controllare idempotenza e invoice_id.
    Solleva eccezione se qualcosa va storto — il chiamante gestisce il fallimento.
    """
    sub_id: str = row["id"]
    email: str = row["email"]
    first_name: str = row["first_name"]
    plan: str = row["plan"]

    # 1. Ricostruisci i modelli Pydantic dall'intake salvato
    intake = IntakeRequest.model_validate_json(row["intake_json"])
    targets = NutritionTargets.model_validate_json(row["targets_json"])

    # 2. Ricalcola i target nutrizionali in base all'intake originale.
    #    Questo permette di riflettere eventuali aggiornamenti al profilo
    #    (es. check-in mensile con nuovo peso) se intake_json è stato aggiornato.
    targets = compute_targets(intake)

    # 3. Genera il nuovo piano via Claude
    log.info("[%s] richiesta piano a Claude", sub_id)
    meal_plan = generate_meal_plan(intake, targets)

    # 3b. Per Completo / Coach genera anche il programma di allenamento aggiornato
    workout_plan = None
    if intake.plan in ("completo", "coach"):
        log.info("[%s] richiesta programma di allenamento a Claude", sub_id)
        workout_plan = generate_workout_plan(intake, targets)

    # 4. Costruisci il PDF nel disco persistente (sottodir refreshes per non
    #    confondere con i PDF dell'ordine iniziale)
    pdf_dir = Path(settings.pdf_storage_dir) / "refreshes"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = str(pdf_dir / f"{sub_id}_mese{plan_month}.pdf")

    log.info("[%s] costruisco PDF -> %s", sub_id, pdf_path)
    build_pdf(intake, targets, meal_plan, pdf_path, workout=workout_plan)

    # 5. Invia l'email di refresh con link check-in per il mese successivo
    checkin_token = row["checkin_token"] or ""
    checkin_url = (
        f"{settings.base_url}/checkin.html?token={checkin_token}"
        if checkin_token else ""
    )
    log.info("[%s] invio email mese %d a %s", sub_id, plan_month, email)
    resend_id = send_refresh_plan_email(
        email=email,
        first_name=first_name,
        plan=plan,
        plan_month=plan_month,
        pdf_path=pdf_path,
        checkin_url=checkin_url,
    )
    log.info("[%s] email inviata (resend_id=%s)", sub_id, resend_id)


if __name__ == "__main__":
    result = run()
    # Exit code 1 se tutti i subscriber falliti (utile per alerting su Render/Fly)
    if result["due"] > 0 and result["success"] == 0 and result["failed"] > 0:
        raise SystemExit(1)
