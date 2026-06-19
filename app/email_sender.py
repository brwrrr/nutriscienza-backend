"""
Invio email transazionali via Resend.
Email principale: consegna del piano PDF al cliente subito dopo la generazione.
"""
import base64
from pathlib import Path

import resend

from .config import settings
from .models import IntakeRequest

resend.api_key = settings.resend_api_key


PLAN_DESCRIPTIONS = {
    "base": "Piano Base — 7 giorni",
    "completo": "Piano Completo — 4 settimane + allenamento",
    "coach": "Piano Coach — 12 settimane periodizzate",
}


def _email_html(intake: IntakeRequest, manage_url: str = "") -> str:
    plan_desc = PLAN_DESCRIPTIONS[intake.plan]
    # Per i piani in abbonamento (completo/coach) mostriamo il link di gestione
    # già dal day-1, in linea con i T&C e con il Codice del Consumo (art. 49+).
    manage_block = ""
    if manage_url and intake.plan in ("completo", "coach"):
        manage_block = f"""
      <p style="font-size:13px;color:#6B6B6B;line-height:1.6;text-align:center;margin:18px 0 0;">
        Vuoi annullare o aggiornare la carta in qualsiasi momento?
        <a href="{manage_url}" style="color:#2D5F3F;font-weight:600;">Gestisci il tuo abbonamento →</a>
      </p>"""
    return f"""\
<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,Segoe UI,Inter,sans-serif;background:#FBF9F4;margin:0;padding:24px;color:#2A2A2A;">
  <div style="max-width:560px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;border:1px solid #E5E0D3;">
    <div style="background:#2D5F3F;padding:6px 0;"></div>
    <div style="padding:32px 36px 28px;">
      <div style="font-family:Georgia,serif;font-size:22px;font-weight:700;color:#2D5F3F;margin-bottom:6px;">
        Nutri<span style="color:#C9A66B;">Scienza</span>
      </div>
      <p style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#A88349;font-weight:700;margin:24px 0 8px;">
        Il tuo piano è pronto
      </p>
      <h1 style="font-family:Georgia,serif;font-size:28px;color:#2D5F3F;margin:0 0 16px;line-height:1.2;">
        Ciao {intake.first_name}, ecco il tuo piano.
      </h1>
      <p style="font-size:15px;line-height:1.6;color:#2A2A2A;">
        Trovi in allegato il tuo <strong>{plan_desc}</strong>, costruito sui tuoi numeri reali e
        in linea con le linee guida LARN della Società Italiana di Nutrizione Umana.
      </p>
      <p style="font-size:15px;line-height:1.6;color:#2A2A2A;">
        Apri il PDF, leggi la pagina del profilo per vedere come abbiamo calcolato il tuo fabbisogno,
        e segui il menù dei 7 giorni. La lista della spesa è già pronta per te.
      </p>
      <div style="background:#E8F0EB;border-left:3px solid #2D5F3F;padding:14px 18px;border-radius:0 6px 6px 0;margin:24px 0;font-size:14px;line-height:1.55;">
        <strong style="color:#2D5F3F;">Tre cose da sapere prima di iniziare:</strong>
        <ol style="margin:8px 0 0;padding-left:18px;">
          <li>Pesati una volta a settimana, sempre alla stessa ora.</li>
          <li>Idratati: 2-2,5 L di acqua al giorno.</li>
          <li>La domenica è prevista flessibilità — è parte del metodo.</li>
        </ol>
      </div>
      <p style="font-size:14px;color:#6B6B6B;line-height:1.6;">
        Hai domande sul piano? Rispondi a questa email o scrivici a
        <a href="mailto:{settings.support_email}" style="color:#2D5F3F;">{settings.support_email}</a> —
        ti rispondiamo entro 48 ore.
      </p>{manage_block}
      <hr style="border:none;border-top:1px solid #E5E0D3;margin:28px 0 18px;">
      <p style="font-size:12px;color:#6B6B6B;line-height:1.5;">
        Questo piano ha finalità educative e non sostituisce il parere di un medico in presenza di
        patologie.
      </p>
    </div>
    <div style="background:#1A2E22;padding:18px 36px;color:rgba(255,255,255,0.7);font-size:12px;">
      © NutriScienza · <a href="{settings.base_url}" style="color:#C9A66B;text-decoration:none;">nutriscienza.org</a>
    </div>
  </div>
</body>
</html>"""


def send_plan_email(intake: IntakeRequest, pdf_path: str, manage_url: str = "") -> str:
    """
    Invia email con PDF in allegato. Ritorna l'id Resend.
    Solleva eccezione se l'invio fallisce.
    """
    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    pdf_filename = f"NutriScienza-{PLAN_DESCRIPTIONS[intake.plan].split(' — ')[0].replace(' ', '_')}-{intake.first_name}.pdf"

    plan_desc = PLAN_DESCRIPTIONS[intake.plan]
    response = resend.Emails.send({
        "from": settings.from_email,
        "to": [intake.email],
        "subject": f"Il tuo {plan_desc} è pronto, {intake.first_name} 🌿",
        "html": _email_html(intake, manage_url=manage_url),
        "attachments": [{
            "filename": pdf_filename,
            "content": pdf_b64,
        }],
    })
    return response["id"]


def _refresh_email_html(first_name: str, plan: str, plan_month: int, checkin_url: str = "",
                        progress_note: str = "") -> str:
    plan_desc = PLAN_DESCRIPTIONS.get(plan, plan)
    ordinal = {
        1: "primo", 2: "secondo", 3: "terzo", 4: "quarto", 5: "quinto",
        6: "sesto", 7: "settimo", 8: "ottavo", 9: "nono", 10: "decimo",
    }.get(plan_month, f"{plan_month}°")
    # Riga personale basata sul progresso dichiarato al check-in (peso precedente
    # vs nuovo). Calcolata a monte in scheduler.compute_progress_note — qui la
    # mostriamo solo se presente.
    progress_block = ""
    if progress_note:
        progress_block = f"""
      <div style="background:#FBF6EA;border-left:3px solid #C9A66B;padding:14px 18px;border-radius:0 6px 6px 0;margin:0 0 8px;font-size:15px;line-height:1.55;color:#7A5C20;">
        {progress_note}
      </div>"""
    return f"""\
<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,Segoe UI,Inter,sans-serif;background:#FBF9F4;margin:0;padding:24px;color:#2A2A2A;">
  <div style="max-width:560px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;border:1px solid #E5E0D3;">
    <div style="background:#2D5F3F;padding:6px 0;"></div>
    <div style="padding:32px 36px 28px;">
      <div style="font-family:Georgia,serif;font-size:22px;font-weight:700;color:#2D5F3F;margin-bottom:6px;">
        Nutri<span style="color:#C9A66B;">Scienza</span>
      </div>
      <p style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#A88349;font-weight:700;margin:24px 0 8px;">
        Piano mensile aggiornato
      </p>
      <h1 style="font-family:Georgia,serif;font-size:28px;color:#2D5F3F;margin:0 0 16px;line-height:1.2;">
        Ciao {first_name}, il tuo {ordinal} mese è pronto.
      </h1>{progress_block}
      <p style="font-size:15px;line-height:1.6;color:#2A2A2A;">
        Grazie per il check-in! Trovi in allegato il piano del tuo <strong>{plan_month}° mese</strong>
        con il {plan_desc}, ricalcolato sul peso che ci hai appena comunicato — stessi principi
        nutrizionali, varietà nei pasti per non annoiarti.
      </p>
      <div style="background:#E8F0EB;border-left:3px solid #2D5F3F;padding:14px 18px;border-radius:0 6px 6px 0;margin:24px 0;font-size:14px;line-height:1.55;">
        <strong style="color:#2D5F3F;">Come usare al meglio il piano di questo mese:</strong>
        <ol style="margin:8px 0 0;padding-left:18px;">
          <li>Confrontalo con quello del mese scorso — nota le variazioni caloriche.</li>
          <li>Usa la lista della spesa aggiornata per fare la spesa il weekend.</li>
          <li>I tuoi target sono stati ricalcolati sul peso che hai indicato al check-in.</li>
        </ol>
      </div>
      <p style="font-size:13px;color:#6B6B6B;line-height:1.6;text-align:center;margin-top:8px;">
        Vuoi annullare o aggiornare la carta?
        <a href="{checkin_url}" style="color:#2D5F3F;font-weight:600;">Gestisci abbonamento</a>
      </p>
      <p style="font-size:14px;color:#6B6B6B;line-height:1.6;">
        Hai domande sul piano? Scrivi a
        <a href="mailto:{settings.support_email}" style="color:#2D5F3F;">{settings.support_email}</a> —
        ti rispondiamo entro 48 ore.
      </p>
      <hr style="border:none;border-top:1px solid #E5E0D3;margin:28px 0 18px;">
      <p style="font-size:12px;color:#6B6B6B;line-height:1.5;">
        Questo piano ha finalità educative e non sostituisce il parere di un medico in presenza di
        patologie.
      </p>
    </div>
    <div style="background:#1A2E22;padding:18px 36px;color:rgba(255,255,255,0.7);font-size:12px;">
      © NutriScienza · <a href="{settings.base_url}" style="color:#C9A66B;text-decoration:none;">nutriscienza.org</a>
    </div>
  </div>
</body>
</html>"""


def send_refresh_plan_email(
    email: str,
    first_name: str,
    plan: str,
    plan_month: int,
    pdf_path: str,
    checkin_url: str = "",
    progress_note: str = "",
) -> str:
    """
    Invia il piano mensile aggiornato ai subscriber Piano Completo / Coach,
    DOPO che hanno completato il check-in. `progress_note` (opzionale) è la riga
    personale sul progresso del mese (es. "Complimenti, hai perso 2 kg!").
    Ritorna l'id Resend.
    """
    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    plan_desc = PLAN_DESCRIPTIONS.get(plan, plan)
    pdf_filename = f"NutriScienza-Mese{plan_month}-{first_name}.pdf"

    response = resend.Emails.send({
        "from": settings.from_email,
        "to": [email],
        "subject": f"Il tuo piano del mese {plan_month} è pronto, {first_name} 🌿",
        "html": _refresh_email_html(first_name, plan, plan_month, checkin_url, progress_note),
        "attachments": [{"filename": pdf_filename, "content": pdf_b64}],
    })
    return response["id"]


def _checkin_request_html(first_name: str, plan_month: int, checkin_url: str,
                          is_reminder: bool = False) -> str:
    """Email che invita al check-in mensile. Stessa shell brandizzata."""
    eyebrow = "Promemoria check-in" if is_reminder else "È ora del check-in"
    if is_reminder:
        intro = (
            f"Ti avevamo scritto qualche giorno fa: il piano del tuo "
            f"<strong>{plan_month}° mese</strong> è pronto per essere generato, "
            f"ma ci serve un aggiornamento al volo sul tuo peso attuale."
        )
    else:
        intro = (
            f"Il tuo abbonamento si è rinnovato — grazie! Prima di costruire il piano "
            f"del tuo <strong>{plan_month}° mese</strong>, facci sapere come stai andando: "
            f"bastano 10 secondi e il peso di oggi."
        )
    return f"""\
<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,Segoe UI,Inter,sans-serif;background:#FBF9F4;margin:0;padding:24px;color:#2A2A2A;">
  <div style="max-width:560px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;border:1px solid #E5E0D3;">
    <div style="background:#2D5F3F;padding:6px 0;"></div>
    <div style="padding:32px 36px 28px;">
      <div style="font-family:Georgia,serif;font-size:22px;font-weight:700;color:#2D5F3F;margin-bottom:6px;">
        Nutri<span style="color:#C9A66B;">Scienza</span>
      </div>
      <p style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#A88349;font-weight:700;margin:24px 0 8px;">
        {eyebrow}
      </p>
      <h1 style="font-family:Georgia,serif;font-size:28px;color:#2D5F3F;margin:0 0 16px;line-height:1.2;">
        Ciao {first_name}, sblocca il tuo piano del mese.
      </h1>
      <p style="font-size:15px;line-height:1.6;color:#2A2A2A;">
        {intro}
      </p>
      <div style="text-align:center;margin:26px 0;">
        <a href="{checkin_url}" style="display:inline-block;background:#2D5F3F;color:white;padding:14px 32px;border-radius:8px;font-weight:700;text-decoration:none;font-size:16px;">
          Fai il check-in →
        </a>
      </div>
      <p style="font-size:14px;line-height:1.6;color:#6B6B6B;">
        Appena lo completi, generiamo il tuo nuovo piano personalizzato e te lo inviamo
        via email entro pochi minuti.
      </p>
      <p style="font-size:14px;color:#6B6B6B;line-height:1.6;">
        Hai domande? Scrivi a
        <a href="mailto:{settings.support_email}" style="color:#2D5F3F;">{settings.support_email}</a>.
      </p>
      <hr style="border:none;border-top:1px solid #E5E0D3;margin:28px 0 18px;">
      <p style="font-size:12px;color:#6B6B6B;line-height:1.5;">
        Ricevi questa email perché hai un abbonamento NutriScienza attivo. Il pagamento del
        rinnovo è già stato elaborato; il check-in serve solo a personalizzare il piano.
      </p>
    </div>
    <div style="background:#1A2E22;padding:18px 36px;color:rgba(255,255,255,0.7);font-size:12px;">
      © NutriScienza · <a href="{settings.base_url}" style="color:#C9A66B;text-decoration:none;">nutriscienza.org</a>
    </div>
  </div>
</body>
</html>"""


def send_checkin_request_email(email: str, first_name: str, plan_month: int,
                               checkin_url: str) -> str:
    """Inviata subito dopo il rinnovo: invita il cliente a fare il check-in."""
    response = resend.Emails.send({
        "from": settings.from_email,
        "to": [email],
        "subject": f"{first_name}, sblocca il tuo piano del mese {plan_month} 🌿",
        "html": _checkin_request_html(first_name, plan_month, checkin_url, is_reminder=False),
    })
    return response["id"]


def send_checkin_reminder_email(email: str, first_name: str, plan_month: int,
                                checkin_url: str) -> str:
    """Sollecito (max 3, ogni 2 giorni) se il check-in non è ancora stato fatto."""
    response = resend.Emails.send({
        "from": settings.from_email,
        "to": [email],
        "subject": f"Promemoria: il tuo piano del mese {plan_month} ti aspetta, {first_name}",
        "html": _checkin_request_html(first_name, plan_month, checkin_url, is_reminder=True),
    })
    return response["id"]


def send_cancellation_email(email: str, first_name: str, plan: str) -> None:
    """Email di conferma cancellazione abbonamento. Non solleva eccezioni."""
    plan_desc = PLAN_DESCRIPTIONS.get(plan, plan)
    html = f"""\
<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,Segoe UI,Inter,sans-serif;background:#FBF9F4;margin:0;padding:24px;color:#2A2A2A;">
  <div style="max-width:560px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;border:1px solid #E5E0D3;">
    <div style="background:#2D5F3F;padding:6px 0;"></div>
    <div style="padding:32px 36px 28px;">
      <div style="font-family:Georgia,serif;font-size:22px;font-weight:700;color:#2D5F3F;margin-bottom:6px;">
        Nutri<span style="color:#C9A66B;">Scienza</span>
      </div>
      <p style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#A88349;font-weight:700;margin:24px 0 8px;">
        Abbonamento cancellato
      </p>
      <h1 style="font-family:Georgia,serif;font-size:26px;color:#2D5F3F;margin:0 0 16px;line-height:1.2;">
        Ciao {first_name}, il tuo abbonamento è stato cancellato.
      </h1>
      <p style="font-size:15px;line-height:1.6;color:#2A2A2A;">
        Abbiamo ricevuto la richiesta di cancellazione del tuo <strong>{plan_desc}</strong>.
        Non riceverai ulteriori addebiti e nessun piano verrà generato il mese prossimo.
      </p>
      <p style="font-size:15px;line-height:1.6;color:#2A2A2A;">
        I piani già ricevuti rimangono tuoi — puoi continuare a usarli quando vuoi.
      </p>
      <div style="background:#E8F0EB;border-left:3px solid #2D5F3F;padding:14px 18px;border-radius:0 6px 6px 0;margin:24px 0;font-size:14px;">
        Hai cancellato per errore o vuoi ricominciare?
        <a href="{settings.base_url}" style="color:#2D5F3F;font-weight:600;">Torna su NutriScienza →</a>
      </div>
      <p style="font-size:14px;color:#6B6B6B;line-height:1.6;">
        Per qualsiasi problema scrivi a
        <a href="mailto:{settings.support_email}" style="color:#2D5F3F;">{settings.support_email}</a>.
      </p>
      <hr style="border:none;border-top:1px solid #E5E0D3;margin:28px 0 18px;">
      <p style="font-size:12px;color:#6B6B6B;">
        Questo è un messaggio automatico di conferma cancellazione.
      </p>
    </div>
    <div style="background:#1A2E22;padding:18px 36px;color:rgba(255,255,255,0.7);font-size:12px;">
      © NutriScienza · <a href="{settings.base_url}" style="color:#C9A66B;text-decoration:none;">nutriscienza.org</a>
    </div>
  </div>
</body>
</html>"""
    try:
        resend.Emails.send({
            "from": settings.from_email,
            "to": [email],
            "subject": f"Abbonamento NutriScienza cancellato — {first_name}",
            "html": html,
        })
    except Exception:
        pass  # non blocca il flusso principale


def send_admin_failure(order_id: str, error: str) -> None:
    """Notifica interna se la generazione fallisce."""
    try:
        resend.Emails.send({
            "from": settings.from_email,
            "to": [settings.support_email],
            "subject": f"[NutriScienza] Generazione fallita per ordine {order_id}",
            "html": f"<p>Ordine: <code>{order_id}</code></p><pre>{error}</pre>",
        })
    except Exception:
        pass  # non vogliamo che un errore qui mascheri l'errore originale
