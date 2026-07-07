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
from .email_sender import (
    send_admin_failure,
    send_base_upsell_email,
    send_checkin_reminder_email,
    send_checkin_request_email,
    send_refresh_plan_email,
)
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

# Solleciti check-in: cadenza e numero massimo. Dopo MAX_REMINDERS smettiamo
# di scrivere (il cliente ha già pagato; non lo tempestiamo all'infinito).
REMINDER_INTERVAL_DAYS = 2
MAX_CHECKIN_REMINDERS = 3


def compute_progress_note(old_weight, new_weight, goal: str | None) -> str | None:
    """
    Costruisce la riga personale sul progresso, confrontando il peso del check-in
    con quello del mese precedente. Nessuna chiamata LLM, nessuno storage di storico:
    usiamo il peso già presente nell'intake (mese scorso) vs quello appena inviato.

    Guardrail: mai congratularsi per una regressione; incoraggiare con tatto su un
    passo indietro; valorizzare la costanza in caso di peso stabile.
    """
    try:
        old_w = float(old_weight)
        new_w = float(new_weight)
    except (TypeError, ValueError):
        return None

    diff = round(new_w - old_w, 1)      # >0 = aumentato, <0 = calato
    lost = round(old_w - new_w, 1)      # >0 = ha perso peso

    if abs(diff) < 0.3:
        return ("Hai mantenuto il peso stabile rispetto al mese scorso: la costanza è "
                "la base di ogni risultato. Continuiamo così.")

    if goal == "dimagrire":
        if diff < 0:
            return (f"Complimenti! Hai perso {lost:.1f} kg dal mese scorso. Ottimo lavoro — "
                    f"il piano di questo mese costruisce su questo progresso.")
        return (f"Questo mese il peso è salito di {diff:.1f} kg: può capitare, fa parte del "
                f"percorso. Ho ricalibrato il piano per rimetterti in carreggiata.")

    if goal == "massa":
        if diff > 0:
            return (f"Ottimo! Hai messo su {diff:.1f} kg dal mese scorso — segno che il surplus "
                    f"sta funzionando. Continuiamo a costruire.")
        return (f"Questo mese il peso è calato di {lost:.1f} kg: ho aumentato leggermente "
                f"l'apporto per supportare la crescita.")

    # mantenere / salute
    return (f"Variazione di {diff:+.1f} kg rispetto al mese scorso. Ho aggiornato i target "
            f"per tenerti in equilibrio.")


def refresh_subscriber(
    sub_id: str,
    *,
    force: bool = False,
    invoice_id: str | None = None,
    reason: str = "cron",
    progress_note: str | None = None,
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
        _deliver_plan(row, plan_month, progress_note=progress_note)
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


def _checkin_url(row: sqlite3.Row) -> str:
    token = row["checkin_token"] or ""
    return f"{settings.base_url}/checkin.html?token={token}" if token else ""


def request_checkin_for_subscriber(sub_id: str, invoice_id: str | None = None) -> str:
    """
    Apre un ciclo di check-in per il subscriber e invia l'email di richiesta.

    Usato sia dal rinnovo Stripe (invoice_id valorizzato) sia dal trigger manuale
    admin (invoice_id None). Sul trigger manuale, se il ciclo è già aperto,
    rimandiamo comunque l'email (re-invio del sollecito). Non solleva mai.
    """
    row = storage.get_subscriber_by_id(sub_id)
    if row is None:
        log.warning("request_checkin: subscriber %s non trovato", sub_id)
        return "skipped:not_found"

    plan_month = row["plan_month"] + 1
    status = storage.open_checkin_cycle(sub_id, plan_month=plan_month, invoice_id=invoice_id)
    # 'opened' = nuovo ciclo; 'skipped:already_open' = ciclo manuale già aperto → re-invio.
    if status not in ("opened", "skipped:already_open"):
        log.info("[%s] ciclo check-in non aperto (%s) inv=%s", sub_id, status, invoice_id)
        return status

    fresh = storage.get_subscriber_by_id(sub_id)
    pending_month = fresh["pending_plan_month"] or plan_month
    try:
        send_checkin_request_email(
            email=fresh["email"],
            first_name=fresh["first_name"],
            plan_month=pending_month,
            checkin_url=_checkin_url(fresh),
        )
        log.info("[%s] email richiesta check-in inviata (mese %d, %s)",
                 sub_id, pending_month, status)
    except Exception:
        log.exception("[%s] invio richiesta check-in fallito", sub_id)
        return "opened:email_failed"
    return status


def request_checkin_by_stripe_subscription(stripe_sub_id: str, invoice_id: str | None = None) -> str:
    """
    Percorso PRIMARIO sui rinnovi (webhook `invoice.payment_succeeded`).

    Il pagamento è già avvenuto; NON generiamo subito il piano. Apriamo invece
    un ciclo di check-in e inviamo l'email che invita il cliente ad aggiornare i
    propri dati. Il piano verrà generato quando completa il check-in
    (`refresh_subscriber` chiamato dall'endpoint /api/checkin).

    Idempotente sull'invoice_id (vedi storage.open_checkin_cycle). Non solleva mai.
    """
    row = storage.get_subscriber_by_stripe_sub(stripe_sub_id)
    if row is None:
        log.warning("request_checkin: nessun subscriber per sub %s", stripe_sub_id)
        return "skipped:not_found"
    return request_checkin_for_subscriber(row["id"], invoice_id=invoice_id)


def _send_due_reminders() -> dict[str, int]:
    """
    Invia i solleciti di check-in ai subscriber che hanno pagato il rinnovo ma
    non hanno ancora aggiornato i dati. Cadenza: ogni REMINDER_INTERVAL_DAYS,
    fino a MAX_CHECKIN_REMINDERS. Dopo, silenzio (il cliente ha già pagato).
    """
    now = datetime.now(timezone.utc)
    awaiting = storage.get_subscribers_awaiting_checkin()
    counts = {"awaiting": len(awaiting), "reminded": 0, "skipped": 0}

    for row in awaiting:
        sent = row["checkin_reminders_sent"] or 0
        if sent >= MAX_CHECKIN_REMINDERS:
            counts["skipped"] += 1
            continue
        # Tempo trascorso dall'ultimo contatto (sollecito o richiesta iniziale).
        anchor = row["last_reminder_at"] or row["checkin_requested_at"]
        if anchor:
            try:
                if now - datetime.fromisoformat(anchor) < timedelta(days=REMINDER_INTERVAL_DAYS):
                    counts["skipped"] += 1
                    continue
            except ValueError:
                pass  # data malformata → manda comunque

        plan_month = row["pending_plan_month"] or (row["plan_month"] + 1)
        try:
            send_checkin_reminder_email(
                email=row["email"],
                first_name=row["first_name"],
                plan_month=plan_month,
                checkin_url=_checkin_url(row),
            )
            storage.record_checkin_reminder(row["id"])
            counts["reminded"] += 1
            log.info("[%s] sollecito check-in #%d inviato (mese %d)",
                     row["id"], sent + 1, plan_month)
        except Exception:
            log.exception("[%s] invio sollecito check-in fallito", row["id"])
            counts["skipped"] += 1
    return counts


def _open_missed_checkins() -> dict[str, int]:
    """
    Backstop: se il webhook del rinnovo non è arrivato, un subscriber attivo può
    avere next_plan_due_at scaduto senza un ciclo di check-in aperto. Qui glielo
    apriamo (senza invoice) e inviamo la richiesta di check-in. Idempotente:
    salta chi ha già un ciclo aperto.
    """
    due_rows = storage.get_subscribers_due_for_refresh()
    counts = {"due": len(due_rows), "opened": 0, "skipped": 0}

    for row in due_rows:
        if row["checkin_due"]:
            counts["skipped"] += 1
            continue
        plan_month = row["plan_month"] + 1
        status = storage.open_checkin_cycle(row["id"], plan_month=plan_month, invoice_id=None)
        if status != "opened":
            counts["skipped"] += 1
            continue
        fresh = storage.get_subscriber_by_id(row["id"])
        try:
            send_checkin_request_email(
                email=fresh["email"],
                first_name=fresh["first_name"],
                plan_month=plan_month,
                checkin_url=_checkin_url(fresh),
            )
            counts["opened"] += 1
            log.info("[%s] backstop: ciclo check-in aperto (mese %d)", row["id"], plan_month)
        except Exception:
            log.exception("[%s] backstop: invio richiesta check-in fallito", row["id"])
            counts["skipped"] += 1
    return counts


def _send_base_upsells() -> dict[str, int]:
    """
    Invia l'email di upsell Base → Completo agli acquirenti del Piano Base a ~1 mese
    dall'acquisto (finestra configurabile, default 30–45 giorni). Idempotente: ogni
    ordine viene marcato dopo l'invio, così non viene mai ricontattato due volte.

    Best-effort: un fallimento su un singolo ordine non blocca gli altri né il run.
    """
    due = storage.get_base_orders_due_for_upsell(
        min_days=settings.base_upsell_min_days,
        max_days=settings.base_upsell_max_days,
    )
    counts = {"due": len(due), "sent": 0, "skipped": 0}

    for row in due:
        order_id = row["id"]
        try:
            intake = IntakeRequest.model_validate_json(row["intake_json"])
            first_name = intake.first_name
            email = intake.email
        except Exception:
            # Intake malformato/anonimizzato → non possiamo personalizzare: salta.
            log.warning("[%s] upsell Base: intake illeggibile — skip", order_id)
            counts["skipped"] += 1
            continue

        try:
            resend_id = send_base_upsell_email(email=email, first_name=first_name)
            storage.mark_base_upsell_sent(order_id)
            counts["sent"] += 1
            log.info("[%s] upsell Base→Completo inviato a %s (resend_id=%s)",
                     order_id, email, resend_id)
        except Exception:
            # Non marchiamo: l'ordine resta idoneo al retry al prossimo sweep.
            log.exception("[%s] invio upsell Base fallito per %s", order_id, email)
            counts["skipped"] += 1
    return counts


def run() -> dict[str, int]:
    """
    Entry point dello sweep giornaliero (cron / endpoint admin).

    Con il check-in gating, il cron NON genera più piani. Fa tre cose:
      1. Invia i solleciti di check-in dovuti (ogni 2 giorni, max 3).
      2. Backstop: apre un ciclo di check-in per i rinnovi il cui webhook è andato
         perso (next_plan_due_at scaduto e nessun ciclo aperto).
      3. Upsell: invia l'email Base → Completo agli acquirenti Base a ~1 mese
         dall'acquisto (una sola volta per ordine).
    La generazione del piano avviene quando il cliente completa il check-in.

    Ritorna i contatori del run.
    """
    storage.init_db()

    reminders = _send_due_reminders()
    backstop = _open_missed_checkins()
    upsells = _send_base_upsells()
    log.info("Check-in in attesa: %d, solleciti inviati: %d | backstop aperti: %d | "
             "upsell Base inviati: %d/%d",
             reminders["awaiting"], reminders["reminded"], backstop["opened"],
             upsells["sent"], upsells["due"])

    counts = {
        "awaiting_checkin": reminders["awaiting"],
        "reminders_sent": reminders["reminded"],
        "backstop_opened": backstop["opened"],
        "base_upsells_sent": upsells["sent"],
    }

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
        "Run completato — %d in attesa check-in, %d solleciti, %d backstop aperti",
        counts["awaiting_checkin"], counts["reminders_sent"], counts["backstop_opened"],
    )
    return counts


def _deliver_plan(row: sqlite3.Row, plan_month: int, progress_note: str | None = None) -> None:
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
    build_pdf(intake, targets, meal_plan, pdf_path, workout=workout_plan,
              progress_note=progress_note)

    # 5. Invia l'email col piano + riga personale sul progresso (se presente).
    #    Il link check-in serve qui solo per "Gestisci abbonamento".
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
        progress_note=progress_note or "",
    )
    log.info("[%s] email inviata (resend_id=%s)", sub_id, resend_id)


if __name__ == "__main__":
    run()
