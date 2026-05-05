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


def run() -> dict[str, int]:
    """
    Entry point del cron job.

    Ritorna un dizionario con i contatori del run:
        {"due": N, "success": N, "skipped": N, "failed": N}
    """
    storage.init_db()

    due_rows = storage.get_subscribers_due_for_refresh()
    log.info("Subscriber in scadenza oggi: %d", len(due_rows))

    counts = {"due": len(due_rows), "success": 0, "skipped": 0, "failed": 0}

    for row in due_rows:
        sub_id: str = row["id"]
        email: str = row["email"]
        first_name: str = row["first_name"]
        plan: str = row["plan"]
        plan_month: int = row["plan_month"] + 1  # next month to send

        log.info("[%s] inizio refresh — %s (mese %d)", sub_id, email, plan_month)

        try:
            _refresh_one(row, plan_month)
            counts["success"] += 1
            log.info("[%s] ✓ piano mese %d inviato a %s", sub_id, plan_month, email)

        except Exception as exc:
            counts["failed"] += 1
            err_text = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            log.error("[%s] ✗ refresh fallito per %s:\n%s", sub_id, email, err_text)
            # Notifica admin via email — non rilanciare l'eccezione
            try:
                send_admin_failure(
                    order_id=f"sub:{sub_id}",
                    error=f"Refresh mese {plan_month} fallito per {email}\n\n{err_text}",
                )
            except Exception:
                log.exception("[%s] impossibile inviare notifica admin", sub_id)

    log.info(
        "Run completato — %d processati, %d successi, %d falliti, %d saltati",
        counts["due"], counts["success"], counts["failed"], counts["skipped"],
    )
    return counts


def _refresh_one(row: sqlite3.Row, plan_month: int) -> None:
    """
    Pipeline completa per un singolo subscriber.
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

    # 6. Aggiorna next_plan_due_at nel DB — fa avanzare l'orologio di 30 giorni
    storage.mark_plan_sent(sub_id)


if __name__ == "__main__":
    result = run()
    # Exit code 1 se tutti i subscriber falliti (utile per alerting su Render/Fly)
    if result["due"] > 0 and result["success"] == 0 and result["failed"] > 0:
        raise SystemExit(1)
