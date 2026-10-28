"""
Lead magnet "L'ordine giusto nel piatto" — richiesta guida, consegna, sequenza email.

Flusso:
  1. POST /api/lead  → upsert del lead + email transazionale con la guida (sempre).
  2. Sweep giornaliero (scheduler.run) → sequenza di 3 email SOLO se il lead ha dato
     il consenso marketing (art. 130 Codice Privacy), non si è disiscritto e non
     ha già acquistato. Giorni 2, 5, 9 dalla richiesta: mai più di 2 email in 7 giorni,
     coerente con il testo del consenso ("al massimo due a settimana").
  3. GET /api/unsubscribe/{token} → disiscrizione con un clic (anche via header
     List-Unsubscribe).

Tutto isolato: un errore qui non tocca checkout, generazione piani o rinnovi.
"""
import logging
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import resend

from .config import settings
from .storage import _conn, _now_iso

log = logging.getLogger("nutriscienza.leads")

GUIDE_URL_PATH = "/guide/guida-ordine-dei-cibi.pdf"  # file statico servito dal frontend

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    guide TEXT NOT NULL,
    marketing_consent INTEGER NOT NULL DEFAULT 0,
    consent_version TEXT,
    consent_at TEXT,
    utm_source TEXT,
    utm_medium TEXT,
    utm_campaign TEXT,
    referrer TEXT,
    landing_page TEXT,
    unsub_token TEXT NOT NULL,
    guide_sent_at TEXT,
    nurture_step INTEGER NOT NULL DEFAULT 0,
    last_nurture_at TEXT,
    unsubscribed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_token ON leads(unsub_token);

-- Contatore download PDF: solo aggregati giornalieri per canale, nessun dato personale.
CREATE TABLE IF NOT EXISTS guide_downloads (
    day TEXT NOT NULL,
    guide TEXT NOT NULL,
    channel TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, guide, channel)
);
"""

# Guide scaricabili: nome → path pubblico del PDF. Solo questi sono contati/reindirizzati.
GUIDES = {"ordine-dei-cibi": "/guide/guida-ordine-dei-cibi.pdf"}
DOWNLOAD_CHANNELS = ("email", "pagina")

# (step da inviare, giorni minimi dalla richiesta)
NURTURE_SCHEDULE = [(1, 2), (2, 5), (3, 9)]

_PAID = "('paid','generating','sent')"


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


# ---------- Storage ----------

def upsert_lead(email: str, guide: str, marketing_consent: bool, consent_version: str | None,
                traffic: dict) -> sqlite3.Row:
    """
    Crea il lead o aggiorna quello esistente. Regole sul consenso:
      • una nuova richiesta CON spunta dà (o rinnova) il consenso e annulla una
        disiscrizione precedente: è una nuova manifestazione di volontà esplicita;
      • una richiesta SENZA spunta non revoca un consenso già dato (la revoca
        avviene solo col link di disiscrizione);
      • la sorgente di traffico resta quella del primo contatto (first-touch).
    """
    init()
    email = email.strip().lower()
    now = _now_iso()
    with _conn() as c:
        row = c.execute("SELECT * FROM leads WHERE email=?", (email,)).fetchone()
        if row is None:
            c.execute(
                """INSERT INTO leads (id, email, guide, marketing_consent, consent_version, consent_at,
                       utm_source, utm_medium, utm_campaign, referrer, landing_page,
                       unsub_token, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), email, guide, int(marketing_consent),
                 consent_version if marketing_consent else None,
                 now if marketing_consent else None,
                 traffic.get("utm_source"), traffic.get("utm_medium"), traffic.get("utm_campaign"),
                 traffic.get("referrer"), traffic.get("landing_page"),
                 secrets.token_urlsafe(24), now, now),
            )
        elif marketing_consent:
            c.execute(
                """UPDATE leads SET marketing_consent=1, consent_version=?, consent_at=?,
                       unsubscribed_at=NULL, updated_at=? WHERE email=?""",
                (consent_version, now, now, email),
            )
        return c.execute("SELECT * FROM leads WHERE email=?", (email,)).fetchone()


def mark_guide_sent(lead_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE leads SET guide_sent_at=?, updated_at=? WHERE id=?", (_now_iso(), _now_iso(), lead_id))


def unsubscribe(token: str) -> bool:
    init()
    with _conn() as c:
        cur = c.execute(
            "UPDATE leads SET marketing_consent=0, unsubscribed_at=COALESCE(unsubscribed_at, ?), updated_at=? "
            "WHERE unsub_token=?",
            (_now_iso(), _now_iso(), token),
        )
        return cur.rowcount > 0


def leads_due(step: int, min_days: int) -> list[sqlite3.Row]:
    """Lead idonei al passo `step` della sequenza (finestra a soglia: un run saltato
    non perde nessuno, e nurture_step impedisce i doppioni)."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=min_days)).isoformat()
    # Almeno 2 giorni dall'email precedente: se il cron salta qualche giorno,
    # i passi non si accavallano nello stesso run.
    gap = (now - timedelta(days=2)).isoformat()
    with _conn() as c:
        return c.execute(
            f"""SELECT * FROM leads
                WHERE marketing_consent = 1
                  AND unsubscribed_at IS NULL
                  AND guide_sent_at IS NOT NULL
                  AND nurture_step = ?
                  AND created_at <= ?
                  AND (last_nurture_at IS NULL OR last_nurture_at <= ?)
                  AND email NOT IN (SELECT email FROM orders WHERE status IN {_PAID})
                ORDER BY created_at ASC""",
            (step - 1, cutoff, gap),
        ).fetchall()


def mark_nurture_sent(lead_id: str, step: int) -> None:
    with _conn() as c:
        c.execute("UPDATE leads SET nurture_step=?, last_nurture_at=?, updated_at=? WHERE id=?",
                  (step, _now_iso(), _now_iso(), lead_id))


def leads_by_source() -> list[sqlite3.Row]:
    """Lead per sorgente + quanti hanno poi acquistato (stessa email, ordine pagato)."""
    init()
    with _conn() as c:
        return c.execute(
            f"""SELECT COALESCE(NULLIF(l.utm_source,''), 'diretto/organico') AS source,
                       COALESCE(NULLIF(l.utm_medium,''), '(nessuno)') AS medium,
                       COALESCE(NULLIF(l.utm_campaign,''), '(nessuna)') AS campaign,
                       COUNT(*) AS leads,
                       SUM(l.marketing_consent) AS marketing_opt_in,
                       SUM(CASE WHEN EXISTS (SELECT 1 FROM orders o WHERE o.email = l.email
                                             AND o.status IN {_PAID}) THEN 1 ELSE 0 END) AS buyers
                FROM leads l
                GROUP BY source, medium, campaign
                ORDER BY leads DESC"""
        ).fetchall()


def record_download(guide: str, channel: str) -> None:
    init()
    if channel not in DOWNLOAD_CHANNELS:
        channel = "altro"
    day = datetime.now(timezone.utc).date().isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO guide_downloads (day, guide, channel, count) VALUES (?,?,?,1)
               ON CONFLICT(day, guide, channel) DO UPDATE SET count = count + 1""",
            (day, guide, channel),
        )


def download_stats() -> dict:
    """Totali download per canale + ultimi 7 giorni, e richieste guida per confronto."""
    init()
    since7 = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    with _conn() as c:
        by_channel = {r["channel"]: r["n"] for r in c.execute(
            "SELECT channel, SUM(count) AS n FROM guide_downloads GROUP BY channel")}
        last7 = c.execute("SELECT COALESCE(SUM(count),0) FROM guide_downloads WHERE day >= ?", (since7,)).fetchone()[0]
        lead_totals = c.execute(
            "SELECT COUNT(*) AS leads, COALESCE(SUM(marketing_consent),0) AS opt_in FROM leads").fetchone()
    return {
        "total": sum(by_channel.values()),
        "by_channel": by_channel,
        "last_7_days": last7,
        "guide_requests": lead_totals["leads"],
        "marketing_opt_in": lead_totals["opt_in"],
    }


def delete_lead(email: str) -> int:
    """Cancellazione GDPR (art. 17)."""
    init()
    with _conn() as c:
        return c.execute("DELETE FROM leads WHERE email=?", (email.strip().lower(),)).rowcount


# ---------- Email ----------

def _unsub_url(row: sqlite3.Row) -> str:
    return f"{settings.api_base_url}/api/unsubscribe/{row['unsub_token']}"


def _shell(kicker: str, title: str, body_html: str, footer_html: str) -> str:
    return f"""\
<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,Segoe UI,Inter,sans-serif;background:#FBF9F4;margin:0;padding:24px;color:#2A2A2A;">
  <div style="max-width:560px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;border:1px solid #E5E0D3;">
    <div style="background:#2D5F3F;padding:6px 0;"></div>
    <div style="padding:32px 36px 28px;">
      <div style="font-family:Georgia,serif;font-size:22px;font-weight:700;color:#2D5F3F;">Nutri<span style="color:#C9A66B;">Scienza</span></div>
      <p style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#A88349;font-weight:700;margin:24px 0 8px;">{kicker}</p>
      <h1 style="font-family:Georgia,serif;font-size:26px;color:#2D5F3F;margin:0 0 16px;line-height:1.25;">{title}</h1>
      {body_html}
      <p style="font-size:14px;color:#6B6B6B;line-height:1.6;margin-top:24px;">Hai domande? Rispondi a questa email o scrivici a
        <a href="mailto:{settings.support_email}" style="color:#2D5F3F;">{settings.support_email}</a>.</p>
    </div>
    <div style="background:#1A2E22;padding:18px 36px;color:rgba(255,255,255,0.7);font-size:12px;line-height:1.5;">
      {footer_html}<br><br>
      NutriScienza è uno strumento digitale: i piani sono generati con formule validate e intelligenza artificiale, non da un nutrizionista. Contenuti educativi, non sostituiscono il parere del medico.<br><br>
      © NutriScienza · <a href="{settings.base_url}" style="color:#C9A66B;text-decoration:none;">nutriscienza.org</a>
    </div>
  </div>
</body></html>"""


def _p(text: str) -> str:
    return f'<p style="font-size:15px;line-height:1.6;color:#2A2A2A;margin:0 0 14px;">{text}</p>'


def _button(url: str, label: str, gold: bool = False) -> str:
    bg, fg = ("#C9A66B", "#1A2E22") if gold else ("#2D5F3F", "#FFFFFF")
    return (f'<div style="text-align:center;margin:26px 0;"><a href="{url}" style="display:inline-block;background:{bg};'
            f'color:{fg};padding:14px 30px;border-radius:8px;font-weight:700;text-decoration:none;font-size:16px;">{label}</a></div>')


def _plan_url(campaign: str) -> str:
    return (f"{settings.base_url}/questionario.html?piano=base"
            f"&utm_source=email&utm_medium=nurture&utm_campaign={campaign}")


def _send(row: sqlite3.Row, subject: str, html: str, marketing: bool) -> str:
    payload = {
        "from": settings.from_email,
        "to": [row["email"]],
        "subject": subject,
        "html": html,
    }
    if marketing:
        payload["headers"] = {
            "List-Unsubscribe": f"<{_unsub_url(row)}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    return resend.Emails.send(payload)["id"]


def send_guide_email(row: sqlite3.Row) -> str:
    # Passa dal backend per contare il download (poi redirect al PDF statico).
    guide_url = f"{settings.api_base_url}/api/guide/ordine-dei-cibi/download?c=email"
    body = (
        _p("Ecco la guida che hai richiesto. Sono 5 pagine: cosa dicono gli studi sull'ordine dei cibi, "
           "la regola in 3 passi e come adattarla a pasta, pizza, colazione e aperitivo.")
        + _button(guide_url, "Scarica la guida (PDF) →")
        + _p("<strong>Il consiglio da provare già a cena:</strong> inizia con un piatto di verdure, "
             "poi il secondo, e per ultimi pane, pasta o patate. Stessi cibi, curva glicemica più bassa.")
    )
    if row["marketing_consent"]:
        footer = (f'Ricevi questa email perché hai richiesto la guida e accettato di ricevere i nostri consigli. '
                  f'<a href="{_unsub_url(row)}" style="color:#C9A66B;">Disiscriviti con un clic</a>.')
    else:
        footer = "Ricevi questa email perché hai richiesto la guida su nutriscienza.org. Non ti invieremo altre email promozionali."
    html = _shell("La tua guida gratuita", "L'ordine giusto nel piatto 🌿", body, footer)
    return _send(row, "La tua guida: L'ordine giusto nel piatto", html, marketing=bool(row["marketing_consent"]))


def _nurture_content(step: int) -> tuple[str, str, str, str]:
    """(subject, kicker, title, body_html)"""
    if step == 1:
        return (
            "Il problema del primo piatto (e come risolverlo)",
            "Consiglio pratico",
            "Pasta sì, ma al momento giusto",
            _p("Il pranzo all'italiana mette il primo prima del secondo: proprio l'ordine opposto a quello degli studi.")
            + _p("Non serve rinunciare alla pasta. Tre modi semplici per rimettere l'ordine a posto:")
            + _p("1. <strong>Contorno in apertura:</strong> un'insalata o verdure cotte prima di tutto, anche mentre la pasta cuoce.<br>"
                 "2. <strong>Secondo prima del primo:</strong> a casa nessuno te lo vieta. Una porzione di pasta più piccola alla fine sazia di più.<br>"
                 "3. <strong>Piatto unico furbo:</strong> pasta con legumi, tonno o ricotta e una base di verdure: inizia dalle verdure.")
            + _p("La parte che la guida non può fare: dirti <em>quanta</em> pasta, quante proteine e quante calorie servono a te. "
                 "Quello dipende da età, peso, altezza, attività e obiettivo.")
            + _button(_plan_url("nurture_1"), "Calcola il tuo piano →"),
        )
    if step == 2:
        return (
            "L'ordine giusto, nelle quantità giuste",
            "Il prossimo passo",
            "Dall'ordine alle quantità",
            _p("L'ordine dei cibi migliora la risposta a ogni pasto. Ma per cambiare peso e composizione corporea "
               "conta soprattutto quanto mangi nell'arco della giornata.")
            + _p("Il <strong>Piano Base</strong> fa il calcolo per te:")
            + _p("• fabbisogno calorico con la formula Mifflin-St Jeor e il tuo livello di attività;<br>"
                 "• proteine, carboidrati e grassi sui riferimenti LARN;<br>"
                 "• un piano alimentare in PDF con pasti già nell'ordine giusto, adattato alla tua alimentazione "
                 "(onnivora, vegetariana, vegana).")
            + _p("Rispondi a un questionario di 3 minuti e ricevi il piano via email entro 24 ore. "
                 "<strong>€19 una tantum</strong>, nessun abbonamento.")
            + _button(_plan_url("nurture_2"), "Crea il tuo Piano Base · €19", gold=True),
        )
    return (
        "3 domande che ci fanno spesso sul Piano Base",
        "Domande frequenti",
        "Prima di decidere",
        _p("<strong>Chi prepara il piano?</strong><br>Nessun nutrizionista: NutriScienza è uno strumento digitale. "
           "Calorie e macronutrienti sono calcolati con formule validate dalla letteratura (Mifflin-St Jeor, LARN); "
           "i pasti sono composti con l'intelligenza artificiale sui tuoi numeri e sulle tue preferenze.")
        + _p("<strong>È adatto a me se ho una patologia?</strong><br>No: se hai diabete, malattie renali, disturbi "
             "alimentari o sei in gravidanza, rivolgiti al tuo medico. Il piano è pensato per adulti sani.")
        + _p("<strong>Devo abbonarmi?</strong><br>No. Il Piano Base costa €19 una volta sola. Se poi vorrai un piano "
             "ricalcolato ogni mese sui tuoi progressi, esiste il Piano Completo, ma non è obbligatorio.")
        + _p("Questa è l'ultima email della serie sulla guida.")
        + _button(_plan_url("nurture_3"), "Crea il tuo Piano Base · €19"),
    )


def send_nurture_email(row: sqlite3.Row, step: int) -> str:
    subject, kicker, title, body = _nurture_content(step)
    footer = (f'Ricevi questa email perché hai richiesto la guida "L\'ordine giusto nel piatto" e accettato di ricevere '
              f'i nostri consigli. <a href="{_unsub_url(row)}" style="color:#C9A66B;">Disiscriviti con un clic</a>.')
    return _send(row, subject, _shell(kicker, title, body, footer), marketing=True)


def send_due_nurture() -> dict[str, int]:
    """Chiamata dallo sweep giornaliero. Un fallimento su un lead non blocca gli altri;
    il passo non viene marcato, così riprova al run successivo."""
    init()
    sent = due = 0
    for step, min_days in NURTURE_SCHEDULE:
        for row in leads_due(step, min_days):
            due += 1
            try:
                rid = send_nurture_email(row, step)
                mark_nurture_sent(row["id"], step)
                sent += 1
                log.info("[lead %s] nurture %d inviata (resend_id=%s)", row["id"], step, rid)
            except Exception:
                log.exception("[lead %s] invio nurture %d fallito", row["id"], step)
    return {"due": due, "sent": sent}
