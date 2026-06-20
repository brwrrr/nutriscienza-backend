"""
Persistenza ordini in SQLite — abbastanza per MVP / fino a ~10k ordini/mese.
Da migrare a Postgres quando si scala.
"""
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import settings
from .models import IntakeRequest, NutritionTargets, OrderStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    intake_json TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    plan_chosen TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment',
    stripe_session_id TEXT,
    stripe_subscription_id TEXT,
    stripe_customer_id TEXT,
    pdf_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(stripe_session_id);
CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(email);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS subscribers (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    email TEXT NOT NULL,
    first_name TEXT NOT NULL,
    intake_json TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    plan TEXT NOT NULL,
    stripe_subscription_id TEXT UNIQUE,
    stripe_customer_id TEXT,
    subscription_status TEXT NOT NULL DEFAULT 'active',
    plan_month INTEGER NOT NULL DEFAULT 1,
    checkin_token TEXT UNIQUE,
    last_plan_sent_at TEXT,
    next_plan_due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_subscribers_due ON subscribers(next_plan_due_at, subscription_status);
CREATE INDEX IF NOT EXISTS idx_subscribers_stripe ON subscribers(stripe_subscription_id);
CREATE INDEX IF NOT EXISTS idx_subscribers_email ON subscribers(email);

-- ── Affiliate program ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS affiliates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    ref_code TEXT NOT NULL UNIQUE,
    commission_rate REAL NOT NULL DEFAULT 0.30,
    payout_method TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'wise' | 'paypal' | 'sepa'
    payout_details TEXT,                            -- JSON
    status TEXT NOT NULL DEFAULT 'active',          -- 'active' | 'paused' | 'banned'
    portal_token TEXT UNIQUE,                       -- magic-link login token (rotated on use)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_affiliates_ref ON affiliates(ref_code);
CREATE INDEX IF NOT EXISTS idx_affiliates_email ON affiliates(email);

CREATE TABLE IF NOT EXISTS commissions (
    id TEXT PRIMARY KEY,
    affiliate_id TEXT NOT NULL REFERENCES affiliates(id),
    stripe_event_ref TEXT NOT NULL UNIQUE,         -- idempotency: 'cs:<id>' or 'inv:<id>'
    order_id TEXT,                                  -- per Base
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    plan TEXT NOT NULL,
    gross_amount_cents INTEGER NOT NULL,
    commission_amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'eur',
    status TEXT NOT NULL DEFAULT 'pending',         -- 'pending'|'approved'|'paid'|'reversed'
    earned_at TEXT NOT NULL,
    payable_at TEXT NOT NULL,
    paid_at TEXT,
    payout_id TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_commissions_affiliate ON commissions(affiliate_id, status);
CREATE INDEX IF NOT EXISTS idx_commissions_payable ON commissions(status, payable_at);
CREATE INDEX IF NOT EXISTS idx_commissions_subscription ON commissions(stripe_subscription_id);

CREATE TABLE IF NOT EXISTS payouts (
    id TEXT PRIMARY KEY,
    affiliate_id TEXT NOT NULL REFERENCES affiliates(id),
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'eur',
    method TEXT NOT NULL,
    external_ref TEXT,                              -- transaction id su Wise/PayPal/SEPA
    notes TEXT,
    paid_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payouts_affiliate ON payouts(affiliate_id);

-- ── Rinnovi abbonamento ──────────────────────────────────────────────────────
-- I rinnovi mensili (invoice.payment_succeeded, billing_reason != subscription_create)
-- NON creano un nuovo ordine. Li registriamo qui per includerli nei totali della
-- dashboard. Idempotente sull'invoice Stripe (UNIQUE) contro i retry del webhook.
CREATE TABLE IF NOT EXISTS renewals (
    id TEXT PRIMARY KEY,
    stripe_invoice_id TEXT NOT NULL UNIQUE,
    stripe_subscription_id TEXT,
    plan TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'eur',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_renewals_created ON renewals(created_at);
CREATE INDEX IF NOT EXISTS idx_renewals_sub ON renewals(stripe_subscription_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(SCHEMA)
        # Migrazione forward-compatible: aggiunge colonne introdotte dopo il deploy iniziale.
        # SQLite non supporta IF NOT EXISTS su ALTER TABLE — usiamo try/except.
        for _migration in [
            "ALTER TABLE subscribers ADD COLUMN checkin_token TEXT",
            "ALTER TABLE orders ADD COLUMN affiliate_ref TEXT",
            "ALTER TABLE subscribers ADD COLUMN affiliate_ref TEXT",
            # Idempotenza rinnovi: ultimo invoice Stripe già fulfillato per il subscriber.
            "ALTER TABLE subscribers ADD COLUMN last_invoice_id TEXT",
            # ── Check-in gating (il piano del mese si genera SOLO dopo il check-in) ──
            # checkin_due=1 → il cliente ha pagato il rinnovo ma non ha ancora fatto
            # il check-in; il piano è in attesa. Tornerà a 0 appena il cliente invia.
            "ALTER TABLE subscribers ADD COLUMN checkin_due INTEGER NOT NULL DEFAULT 0",
            # Invoice Stripe che ha aperto il ciclo di check-in corrente (idempotenza).
            "ALTER TABLE subscribers ADD COLUMN checkin_invoice_id TEXT",
            # Quando è stato richiesto il check-in (base per il timing dei solleciti).
            "ALTER TABLE subscribers ADD COLUMN checkin_requested_at TEXT",
            # Quanti solleciti sono stati inviati per il ciclo corrente (cap a 3).
            "ALTER TABLE subscribers ADD COLUMN checkin_reminders_sent INTEGER NOT NULL DEFAULT 0",
            # Timestamp dell'ultimo sollecito inviato (cadenza: ogni 2 giorni).
            "ALTER TABLE subscribers ADD COLUMN last_reminder_at TEXT",
            # Mese del piano da consegnare una volta completato il check-in.
            "ALTER TABLE subscribers ADD COLUMN pending_plan_month INTEGER",
            # ── Traffic attribution (sorgenti per decidere dove investire in promo) ──
            # Catturati al submit del questionario (first-touch da localStorage).
            "ALTER TABLE orders ADD COLUMN utm_source TEXT",
            "ALTER TABLE orders ADD COLUMN utm_medium TEXT",
            "ALTER TABLE orders ADD COLUMN utm_campaign TEXT",
            "ALTER TABLE orders ADD COLUMN referrer TEXT",
            "ALTER TABLE orders ADD COLUMN landing_page TEXT",
            # ── GDPR ──
            # Marca le righe il cui PII è stato cancellato/anonimizzato su richiesta.
            "ALTER TABLE orders ADD COLUMN anonymized_at TEXT",
        ]:
            try:
                c.execute(_migration)
            except sqlite3.OperationalError:
                pass  # colonna già presente


@contextmanager
def _conn():
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_order(
    intake: IntakeRequest,
    targets: NutritionTargets,
    affiliate_ref: str | None = None,
    traffic: dict | None = None,
) -> str:
    order_id = "ord_" + uuid.uuid4().hex[:16]
    traffic = traffic or {}
    with _conn() as c:
        c.execute(
            """INSERT INTO orders (id, intake_json, targets_json, plan_chosen, email,
                                   status, affiliate_ref,
                                   utm_source, utm_medium, utm_campaign, referrer, landing_page,
                                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending_payment', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id,
                intake.model_dump_json(by_alias=True),
                targets.model_dump_json(),
                intake.plan,
                intake.email,
                affiliate_ref,
                traffic.get("utm_source"),
                traffic.get("utm_medium"),
                traffic.get("utm_campaign"),
                traffic.get("referrer"),
                traffic.get("landing_page"),
                _now_iso(),
                _now_iso(),
            ),
        )
    return order_id


def attach_session(order_id: str, stripe_session_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE orders SET stripe_session_id=?, updated_at=? WHERE id=?",
            (stripe_session_id, _now_iso(), order_id),
        )


def get_order(order_id: str) -> Optional[OrderStatus]:
    with _conn() as c:
        row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return None
    return _row_to_order(row)


def get_order_by_session(session_id: str) -> Optional[OrderStatus]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM orders WHERE stripe_session_id=?", (session_id,)
        ).fetchone()
    return _row_to_order(row) if row else None


def update_status(order_id: str, status: str, **fields) -> None:
    sets = ["status=?", "updated_at=?"]
    args = [status, _now_iso()]
    for k, v in fields.items():
        if k in {"pdf_path", "error", "stripe_session_id"}:
            sets.append(f"{k}=?")
            args.append(v)
    args.append(order_id)
    with _conn() as c:
        c.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", tuple(args))


def update_stripe_ids(order_id: str, stripe_subscription_id: str | None, stripe_customer_id: str | None) -> None:
    """Store Stripe subscription + customer IDs on the order after checkout completes."""
    with _conn() as c:
        c.execute(
            "UPDATE orders SET stripe_subscription_id=?, stripe_customer_id=?, updated_at=? WHERE id=?",
            (stripe_subscription_id, stripe_customer_id, _now_iso(), order_id),
        )


def _row_to_order(row: sqlite3.Row) -> OrderStatus:
    return OrderStatus(
        id=row["id"],
        intake=IntakeRequest.model_validate_json(row["intake_json"]),
        targets=NutritionTargets.model_validate_json(row["targets_json"]),
        plan_chosen=row["plan_chosen"],
        email=row["email"],
        status=row["status"],
        stripe_session_id=row["stripe_session_id"],
        stripe_subscription_id=row["stripe_subscription_id"],
        stripe_customer_id=row["stripe_customer_id"],
        pdf_path=row["pdf_path"],
        error=row["error"],
    )


# ── Subscribers ───────────────────────────────────────────────────────────────

def create_subscriber(
    order_id: str,
    intake: "IntakeRequest",
    targets: "NutritionTargets",
    stripe_subscription_id: str | None = None,
    stripe_customer_id: str | None = None,
    affiliate_ref: str | None = None,
) -> str:
    """
    Called once after the initial plan is sent for completo/coach orders.
    Sets next_plan_due_at = 30 days from now so the cron picks it up on schedule.
    """
    from datetime import timedelta
    sub_id = "sub_" + uuid.uuid4().hex[:16]
    checkin_token = secrets.token_urlsafe(32)
    next_due = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO subscribers
               (id, order_id, email, first_name, intake_json, targets_json, plan,
                stripe_subscription_id, stripe_customer_id, checkin_token,
                affiliate_ref, last_plan_sent_at, next_plan_due_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sub_id,
                order_id,
                intake.email,
                intake.first_name,
                intake.model_dump_json(by_alias=True),
                targets.model_dump_json(),
                intake.plan,
                stripe_subscription_id,
                stripe_customer_id,
                checkin_token,
                affiliate_ref,
                _now_iso(),   # last_plan_sent_at — the initial plan counts as month 1
                next_due,
                _now_iso(),
            ),
        )
    return sub_id


def get_subscribers_due_for_refresh() -> list[sqlite3.Row]:
    """
    Returns all active subscribers whose next refresh is due (next_plan_due_at <= now).
    Ordered oldest-due first so nothing gets starved.
    """
    now = _now_iso()
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM subscribers
               WHERE subscription_status = 'active'
               AND next_plan_due_at <= ?
               ORDER BY next_plan_due_at ASC""",
            (now,),
        ).fetchall()
    return rows


def mark_plan_sent(subscriber_id: str, invoice_id: str | None = None) -> None:
    """Called after a refresh plan has been successfully emailed. Advances the clock 30 days.

    Se `invoice_id` è fornito (rinnovo via webhook Stripe) lo registra su
    last_invoice_id per garantire l'idempotenza: lo stesso invoice non
    rigenera mai due volte il piano, anche con retry del webhook.
    """
    from datetime import timedelta
    now_dt = datetime.now(timezone.utc)
    next_due = (now_dt + timedelta(days=30)).isoformat()
    with _conn() as c:
        if invoice_id is not None:
            c.execute(
                """UPDATE subscribers
                   SET last_plan_sent_at=?, next_plan_due_at=?, plan_month=plan_month+1,
                       last_invoice_id=?
                   WHERE id=?""",
                (now_dt.isoformat(), next_due, invoice_id, subscriber_id),
            )
        else:
            c.execute(
                """UPDATE subscribers
                   SET last_plan_sent_at=?, next_plan_due_at=?, plan_month=plan_month+1
                   WHERE id=?""",
                (now_dt.isoformat(), next_due, subscriber_id),
            )


def get_subscriber_by_id(subscriber_id: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM subscribers WHERE id=?",
            (subscriber_id,),
        ).fetchone()


def cancel_subscriber(stripe_subscription_id: str) -> None:
    """Called on customer.subscription.deleted webhook."""
    with _conn() as c:
        c.execute(
            """UPDATE subscribers
               SET subscription_status='cancelled', cancelled_at=?
               WHERE stripe_subscription_id=?""",
            (_now_iso(), stripe_subscription_id),
        )


def get_subscriber_by_stripe_sub(stripe_subscription_id: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM subscribers WHERE stripe_subscription_id=?",
            (stripe_subscription_id,),
        ).fetchone()


def set_subscriber_status(stripe_subscription_id: str, status: str) -> None:
    """Generic status updater — used for past_due, active, etc."""
    with _conn() as c:
        c.execute(
            "UPDATE subscribers SET subscription_status=? WHERE stripe_subscription_id=?",
            (status, stripe_subscription_id),
        )


def update_subscriber_customer_id(subscriber_id: str, stripe_customer_id: str) -> None:
    """
    Backfill helper. Subscriber legacy creati prima del fix possono avere
    stripe_customer_id NULL — al primo accesso al billing portal lo
    recuperiamo da Stripe e lo salviamo qui per le chiamate successive.
    """
    with _conn() as c:
        c.execute(
            "UPDATE subscribers SET stripe_customer_id=? WHERE id=?",
            (stripe_customer_id, subscriber_id),
        )


# ── Check-in mensile ──────────────────────────────────────────────────────────

def get_subscriber_by_checkin_token(token: str) -> sqlite3.Row | None:
    """Recupera il subscriber tramite il token del link check-in."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM subscribers WHERE checkin_token=? AND subscription_status='active'",
            (token,),
        ).fetchone()


def update_subscriber_weight(token: str, new_weight_kg: float) -> bool:
    """
    Aggiorna il peso corrente nell'intake_json del subscriber.
    Rigenera anche un nuovo checkin_token così il link è monouso.
    Ritorna True se l'aggiornamento ha trovato un record.

    NOTA: percorso legacy. Il flusso attuale usa `apply_checkin`, che oltre
    ad aggiornare il peso chiude il ciclo di check-in e sblocca la generazione
    del piano. Mantenuto per retro-compatibilità.
    """
    row = get_subscriber_by_checkin_token(token)
    if not row:
        return False
    intake_data = json.loads(row["intake_json"])
    intake_data["weight"] = new_weight_kg
    new_token = secrets.token_urlsafe(32)
    with _conn() as c:
        c.execute(
            "UPDATE subscribers SET intake_json=?, checkin_token=? WHERE checkin_token=?",
            (json.dumps(intake_data), new_token, token),
        )
    return True


def apply_checkin(token: str, new_weight_kg: float) -> Optional[dict]:
    """
    Applica il check-in mensile: aggiorna il peso, ruota il token (monouso) e,
    se era in attesa un ciclo di check-in (cliente ha pagato il rinnovo),
    lo CHIUDE (checkin_due=0) così non riceve più solleciti.

    Ritorna un dizionario di contesto per il chiamante (endpoint), oppure None
    se il token non è valido / l'abbonamento non è attivo:
        {
          "sub_id", "old_weight", "new_weight", "first_name", "email",
          "plan", "goal", "was_due" (bool), "checkin_invoice_id",
          "pending_plan_month" (int)
        }
    Se `was_due` è True, il chiamante deve avviare la generazione del piano.
    """
    row = get_subscriber_by_checkin_token(token)
    if not row:
        return None
    intake_data = json.loads(row["intake_json"])
    old_weight = intake_data.get("weight")
    intake_data["weight"] = new_weight_kg
    new_token = secrets.token_urlsafe(32)
    was_due = bool(row["checkin_due"])
    pending_month = row["pending_plan_month"] or (row["plan_month"] + 1)

    with _conn() as c:
        if was_due:
            # Cliente ha agito → chiudi il ciclo e azzera i solleciti.
            c.execute(
                """UPDATE subscribers
                   SET intake_json=?, checkin_token=?, checkin_due=0,
                       checkin_reminders_sent=0
                   WHERE id=?""",
                (json.dumps(intake_data), new_token, row["id"]),
            )
        else:
            c.execute(
                "UPDATE subscribers SET intake_json=?, checkin_token=? WHERE id=?",
                (json.dumps(intake_data), new_token, row["id"]),
            )

    return {
        "sub_id": row["id"],
        "old_weight": old_weight,
        "new_weight": new_weight_kg,
        "first_name": row["first_name"],
        "email": row["email"],
        "plan": row["plan"],
        "goal": intake_data.get("goal"),
        "was_due": was_due,
        "checkin_invoice_id": row["checkin_invoice_id"],
        "pending_plan_month": pending_month,
    }


def open_checkin_cycle(sub_id: str, plan_month: int, invoice_id: str | None = None) -> str:
    """
    Apre un ciclo di check-in per il subscriber: il cliente ha pagato il rinnovo
    (o lo sweep di backstop ha rilevato un rinnovo previsto) ma deve fare il
    check-in prima di ricevere il piano del mese.

    Idempotente:
      - stesso invoice che ha già aperto il ciclo corrente → 'skipped:duplicate'
      - stesso invoice già consegnato in passato            → 'skipped:already_delivered'
      - backstop (invoice_id=None) ma c'è già un ciclo aperto → 'skipped:already_open'

    Ritorna: 'opened' | 'skipped:not_found' | 'skipped:duplicate'
             | 'skipped:already_delivered' | 'skipped:already_open'
    """
    row = get_subscriber_by_id(sub_id)
    if row is None:
        return "skipped:not_found"
    if invoice_id and row["checkin_invoice_id"] == invoice_id and row["checkin_due"]:
        return "skipped:duplicate"
    if invoice_id and row["last_invoice_id"] == invoice_id:
        return "skipped:already_delivered"
    if invoice_id is None and row["checkin_due"]:
        return "skipped:already_open"

    with _conn() as c:
        c.execute(
            """UPDATE subscribers
               SET checkin_due=1, checkin_invoice_id=?, checkin_requested_at=?,
                   checkin_reminders_sent=0, last_reminder_at=NULL,
                   pending_plan_month=?
               WHERE id=?""",
            (invoice_id, _now_iso(), plan_month, sub_id),
        )
    return "opened"


def get_subscribers_awaiting_checkin() -> list[sqlite3.Row]:
    """Subscriber attivi con un ciclo di check-in aperto (per i solleciti)."""
    with _conn() as c:
        return c.execute(
            """SELECT * FROM subscribers
               WHERE subscription_status='active' AND checkin_due=1
               ORDER BY checkin_requested_at ASC""",
        ).fetchall()


def record_checkin_reminder(sub_id: str) -> None:
    """Incrementa il contatore dei solleciti e registra il timestamp."""
    with _conn() as c:
        c.execute(
            """UPDATE subscribers
               SET checkin_reminders_sent = checkin_reminders_sent + 1,
                   last_reminder_at = ?
               WHERE id=?""",
            (_now_iso(), sub_id),
        )


def close_checkin_cycle(sub_id: str) -> None:
    """Chiude esplicitamente un ciclo di check-in (usato come rete di sicurezza)."""
    with _conn() as c:
        c.execute(
            "UPDATE subscribers SET checkin_due=0 WHERE id=?",
            (sub_id,),
        )


# ── Admin ─────────────────────────────────────────────────────────────────────

def get_all_orders(limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            """SELECT id, plan_chosen, email, status, created_at, updated_at, error
               FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()


def get_all_subscribers() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            """SELECT id, email, first_name, plan, subscription_status,
                      plan_month, last_plan_sent_at, next_plan_due_at, created_at, cancelled_at
               FROM subscribers ORDER BY created_at DESC""",
        ).fetchall()


def count_orders_by_status() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) as n FROM orders GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------- Dashboard queries ----------

def get_paid_orders_since(since_iso: str) -> list[sqlite3.Row]:
    """
    Orders that reached at least 'paid' status since `since_iso`.
    Used per period (today / week / month) to compute revenue + costs.
    Status 'paid', 'generating', and 'sent' all imply the customer has been charged.
    """
    with _conn() as c:
        return c.execute(
            """SELECT id, plan_chosen, email, status, created_at, updated_at
               FROM orders
               WHERE status IN ('paid', 'generating', 'sent')
                 AND updated_at >= ?
               ORDER BY updated_at DESC""",
            (since_iso,),
        ).fetchall()


def count_subscribers_by_plan_active() -> dict[str, int]:
    """Subscriber count per plan, only those currently active or past_due (still billable)."""
    with _conn() as c:
        rows = c.execute(
            """SELECT plan, COUNT(*) as n
               FROM subscribers
               WHERE subscription_status IN ('active', 'past_due')
               GROUP BY plan"""
        ).fetchall()
    return {r["plan"]: r["n"] for r in rows}


def count_orders_by_plan_paid() -> dict[str, int]:
    """One-time + recurring counts of paid orders by plan (lifetime)."""
    with _conn() as c:
        rows = c.execute(
            """SELECT plan_chosen, COUNT(*) as n
               FROM orders
               WHERE status IN ('paid', 'generating', 'sent')
               GROUP BY plan_chosen"""
        ).fetchall()
    return {r["plan_chosen"]: r["n"] for r in rows}


def get_latest_paid_orders(limit: int = 10) -> list[sqlite3.Row]:
    """Most recent transactions (paid+) for the dashboard transaction list."""
    with _conn() as c:
        return c.execute(
            """SELECT id, plan_chosen, email, status, updated_at
               FROM orders
               WHERE status IN ('paid', 'generating', 'sent')
               ORDER BY updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


# ── Rinnovi abbonamento (per i totali della dashboard) ──────────────────────────

def record_renewal(
    stripe_invoice_id: str,
    stripe_subscription_id: str | None,
    plan: str,
    amount_cents: int,
    currency: str = "eur",
) -> Optional[str]:
    """
    Registra un pagamento di rinnovo. Idempotente sull'invoice Stripe: un retry
    del webhook non duplica il ricavo. Ritorna None se già registrato.
    """
    ren_id = "ren_" + uuid.uuid4().hex[:16]
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO renewals
                   (id, stripe_invoice_id, stripe_subscription_id, plan,
                    amount_cents, currency, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ren_id, stripe_invoice_id, stripe_subscription_id, plan,
                 amount_cents, currency, _now_iso()),
            )
        return ren_id
    except sqlite3.IntegrityError:
        return None  # invoice già registrato (retry webhook)


def get_renewals_since(since_iso: str) -> list[sqlite3.Row]:
    """Rinnovi registrati dal timestamp dato — per i totali per periodo."""
    with _conn() as c:
        return c.execute(
            """SELECT plan, amount_cents, currency, created_at
               FROM renewals
               WHERE created_at >= ?
               ORDER BY created_at DESC""",
            (since_iso,),
        ).fetchall()


def get_latest_renewals(limit: int = 10) -> list[sqlite3.Row]:
    """Rinnovi più recenti per la lista transazioni della dashboard."""
    with _conn() as c:
        return c.execute(
            """SELECT stripe_subscription_id, plan, amount_cents, created_at
               FROM renewals ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


# ────────────────────────────────────────────────────────────────────────────
# Affiliate program — tutto additivo, nessuna funzione esistente toccata.
# ────────────────────────────────────────────────────────────────────────────

def get_order_affiliate_ref(order_id: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT affiliate_ref FROM orders WHERE id=?", (order_id,)).fetchone()
    return row["affiliate_ref"] if row else None


def get_order_affiliate_ref_by_subscription(stripe_subscription_id: str) -> sqlite3.Row | None:
    """Fallback per quando il subscriber non è ancora stato creato."""
    with _conn() as c:
        return c.execute(
            """SELECT affiliate_ref, email, plan_chosen
               FROM orders
               WHERE stripe_subscription_id=? AND affiliate_ref IS NOT NULL
               LIMIT 1""",
            (stripe_subscription_id,),
        ).fetchone()


def attach_affiliate_to_subscriber(stripe_subscription_id: str, affiliate_ref: str) -> None:
    """
    Backfill: il subscriber viene creato dopo il primo pagamento, ma l'affiliate_ref
    è sull'order. Quando creiamo il subscriber lo passiamo direttamente; questa
    funzione è qui per migrazioni / backfill manuali.
    """
    with _conn() as c:
        c.execute(
            "UPDATE subscribers SET affiliate_ref=? WHERE stripe_subscription_id=?",
            (affiliate_ref, stripe_subscription_id),
        )


# ── Affiliates CRUD ──────────────────────────────────────────────────────────

def create_affiliate(
    name: str,
    email: str,
    ref_code: str,
    commission_rate: float = 0.30,
    payout_method: str = "manual",
) -> str:
    aff_id = "aff_" + uuid.uuid4().hex[:16]
    portal_token = secrets.token_urlsafe(32)
    with _conn() as c:
        c.execute(
            """INSERT INTO affiliates
               (id, name, email, ref_code, commission_rate, payout_method,
                status, portal_token, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (aff_id, name, email, ref_code, commission_rate, payout_method,
             portal_token, _now_iso()),
        )
    return aff_id


def get_affiliate_by_ref(ref_code: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM affiliates WHERE ref_code=?", (ref_code,)
        ).fetchone()


def get_affiliate_by_email(email: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM affiliates WHERE email=?", (email,)
        ).fetchone()


def get_affiliate_by_id(affiliate_id: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM affiliates WHERE id=?", (affiliate_id,)
        ).fetchone()


def get_affiliate_by_portal_token(token: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM affiliates WHERE portal_token=? AND status='active'",
            (token,),
        ).fetchone()


def rotate_portal_token(affiliate_id: str) -> str:
    new_token = secrets.token_urlsafe(32)
    with _conn() as c:
        c.execute(
            "UPDATE affiliates SET portal_token=? WHERE id=?",
            (new_token, affiliate_id),
        )
    return new_token


def list_affiliates() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            """SELECT id, name, email, ref_code, commission_rate, status, created_at
               FROM affiliates ORDER BY created_at DESC"""
        ).fetchall()


def update_affiliate_status(affiliate_id: str, status: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE affiliates SET status=? WHERE id=?",
            (status, affiliate_id),
        )


def update_affiliate_commission_rate(affiliate_id: str, commission_rate: float) -> None:
    """Admin-only: tweak the % commission for an affiliate.
    Stored as fraction (0.30 = 30%). Future commissions use the new rate;
    already-recorded commissions are immutable."""
    if not 0.0 <= commission_rate <= 1.0:
        raise ValueError("commission_rate deve essere tra 0 e 1 (es. 0.30 = 30%)")
    with _conn() as c:
        c.execute(
            "UPDATE affiliates SET commission_rate=? WHERE id=?",
            (commission_rate, affiliate_id),
        )


# ── Commissions ──────────────────────────────────────────────────────────────

def create_commission(
    *,
    affiliate_id: str,
    stripe_event_ref: str,
    order_id: Optional[str],
    stripe_customer_id: Optional[str],
    stripe_subscription_id: Optional[str],
    plan: str,
    gross_amount_cents: int,
    commission_amount_cents: int,
    currency: str,
    status: str,
    earned_at: str,
    payable_at: str,
) -> Optional[str]:
    """
    Idempotente: la UNIQUE su stripe_event_ref previene doppi accrediti
    in caso di webhook retry da Stripe. Ritorna None se la commission esiste già.
    """
    com_id = "com_" + uuid.uuid4().hex[:16]
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO commissions
                   (id, affiliate_id, stripe_event_ref, order_id,
                    stripe_customer_id, stripe_subscription_id, plan,
                    gross_amount_cents, commission_amount_cents, currency,
                    status, earned_at, payable_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (com_id, affiliate_id, stripe_event_ref, order_id,
                 stripe_customer_id, stripe_subscription_id, plan,
                 gross_amount_cents, commission_amount_cents, currency,
                 status, earned_at, payable_at),
            )
        return com_id
    except sqlite3.IntegrityError:
        # Duplicate stripe_event_ref — webhook retry. Idempotent skip.
        return None


def reverse_commission_by_event_ref(stripe_event_ref: str) -> int:
    with _conn() as c:
        cur = c.execute(
            """UPDATE commissions
               SET status='reversed'
               WHERE stripe_event_ref=? AND status IN ('pending', 'approved')""",
            (stripe_event_ref,),
        )
        return cur.rowcount


def approve_pending_commissions_due() -> int:
    """Promuove pending → approved per tutte quelle col payable_at scaduto."""
    with _conn() as c:
        cur = c.execute(
            """UPDATE commissions
               SET status='approved'
               WHERE status='pending' AND payable_at <= ?""",
            (_now_iso(),),
        )
        return cur.rowcount


def list_commissions_for_affiliate(
    affiliate_id: str, limit: int = 200
) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            """SELECT id, stripe_event_ref, plan, gross_amount_cents,
                      commission_amount_cents, currency, status,
                      earned_at, payable_at, paid_at
               FROM commissions
               WHERE affiliate_id=?
               ORDER BY earned_at DESC LIMIT ?""",
            (affiliate_id, limit),
        ).fetchall()


def list_approved_commissions_for_payout(affiliate_id: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            """SELECT id, commission_amount_cents, currency
               FROM commissions
               WHERE affiliate_id=? AND status='approved'""",
            (affiliate_id,),
        ).fetchall()


def list_all_approved_commissions_grouped() -> list[sqlite3.Row]:
    """Per export CSV admin: chi è da pagare e quanto."""
    with _conn() as c:
        return c.execute(
            """SELECT a.id as affiliate_id, a.name, a.email, a.ref_code,
                      a.payout_method, a.payout_details,
                      SUM(c.commission_amount_cents) as total_cents,
                      c.currency,
                      COUNT(c.id) as commission_count
               FROM commissions c
               JOIN affiliates a ON a.id = c.affiliate_id
               WHERE c.status='approved'
               GROUP BY a.id, c.currency
               HAVING total_cents > 0
               ORDER BY total_cents DESC"""
        ).fetchall()


def sum_commissions(affiliate_id: str, status: str) -> int:
    with _conn() as c:
        row = c.execute(
            """SELECT COALESCE(SUM(commission_amount_cents), 0) as s
               FROM commissions
               WHERE affiliate_id=? AND status=?""",
            (affiliate_id, status),
        ).fetchone()
    return int(row["s"] or 0)


def count_unique_referrals(affiliate_id: str) -> int:
    """Numero di clienti unici (per email) generati dall'affiliato."""
    with _conn() as c:
        row = c.execute(
            """SELECT COUNT(DISTINCT o.email) as n
               FROM orders o
               WHERE o.affiliate_ref = (SELECT ref_code FROM affiliates WHERE id=?)
                 AND o.status IN ('paid', 'generating', 'sent')""",
            (affiliate_id,),
        ).fetchone()
    return int(row["n"] or 0)


# ── Payouts ──────────────────────────────────────────────────────────────────

def create_payout_and_mark_paid(
    affiliate_id: str,
    amount_cents: int,
    currency: str,
    method: str,
    external_ref: str | None,
    notes: str | None,
) -> str:
    """
    Atomica: crea il record payout E marca tutte le commissions 'approved'
    di quell'affiliato come 'paid' con FK al payout. Garantisce coerenza:
    nessuna commission paid senza payout, nessun payout senza commissioni.
    """
    payout_id = "pay_" + uuid.uuid4().hex[:16]
    now = _now_iso()
    with _conn() as c:
        c.execute(
            """INSERT INTO payouts
               (id, affiliate_id, amount_cents, currency, method,
                external_ref, notes, paid_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (payout_id, affiliate_id, amount_cents, currency, method,
             external_ref, notes, now),
        )
        c.execute(
            """UPDATE commissions
               SET status='paid', paid_at=?, payout_id=?
               WHERE affiliate_id=? AND status='approved' AND currency=?""",
            (now, payout_id, affiliate_id, currency),
        )
    return payout_id


def list_payouts_for_affiliate(affiliate_id: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            """SELECT id, amount_cents, currency, method, external_ref, paid_at
               FROM payouts
               WHERE affiliate_id=?
               ORDER BY paid_at DESC""",
            (affiliate_id,),
        ).fetchall()


# ════════════════════════════════════════════════════════════════════════════
# Admin: cancellazioni (ordini / affiliati)
# ════════════════════════════════════════════════════════════════════════════

PAID_STATUSES = ("paid", "generating", "sent")


def delete_order(order_id: str) -> Optional[dict]:
    """
    Hard-delete di un ordine (per ripulire test / ordini falliti).
    Ritorna {'pdf_path', 'email', 'status'} così il chiamante può rimuovere
    anche il PDF dal disco, oppure None se l'ordine non esiste.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT pdf_path, email, status FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM orders WHERE id=?", (order_id,))
    return {"pdf_path": row["pdf_path"], "email": row["email"], "status": row["status"]}


def delete_affiliate(affiliate_id: str) -> Optional[dict]:
    """
    Hard-delete di un affiliato + cascata su commissioni e payout collegati.
    Pensato per rimuovere affiliati di test. Ritorna un riassunto di quante righe
    sono state rimosse, o None se l'affiliato non esiste.

    ATTENZIONE: distrugge anche lo storico commissioni/payout di quell'affiliato.
    Per i payout già eseguiti questo cancella solo il record interno, NON storna
    bonifici reali.
    """
    with _conn() as c:
        row = c.execute("SELECT id, name FROM affiliates WHERE id=?", (affiliate_id,)).fetchone()
        if not row:
            return None
        com = c.execute("DELETE FROM commissions WHERE affiliate_id=?", (affiliate_id,))
        com_n = com.rowcount
        pay = c.execute("DELETE FROM payouts WHERE affiliate_id=?", (affiliate_id,))
        pay_n = pay.rowcount
        c.execute("DELETE FROM affiliates WHERE id=?", (affiliate_id,))
    return {"name": row["name"], "commissions_deleted": com_n, "payouts_deleted": pay_n}


# ════════════════════════════════════════════════════════════════════════════
# Admin: profilo cliente (aggregato per email — copre ordini + subscriber)
# ════════════════════════════════════════════════════════════════════════════

def list_customers(search: Optional[str] = None) -> list[dict]:
    """
    Elenco clienti unici (chiave = email, case-insensitive) con aggregati per la
    tabella admin. Aggrega ordini e subscriber. Il calcolo monetario esatto è nel
    profilo dettagliato; qui esponiamo i conteggi e i piani pagati.
    """
    with _conn() as c:
        orders = c.execute(
            """SELECT id, email, plan_chosen, status, created_at, updated_at, intake_json
               FROM orders ORDER BY created_at DESC"""
        ).fetchall()
        subs = c.execute(
            """SELECT email, first_name, plan, subscription_status, next_plan_due_at
               FROM subscribers"""
        ).fetchall()

    cust: dict[str, dict] = {}

    def _bucket(email: str) -> dict:
        key = (email or "").lower()
        if key not in cust:
            cust[key] = {
                "email": email,
                "first_name": None,
                "orders_count": 0,
                "paid_count": 0,
                "paid_plans": [],
                "plans": set(),
                "has_subscriber": False,
                "subscription_status": None,
                "last_activity": None,
            }
        return cust[key]

    for o in orders:
        b = _bucket(o["email"])
        b["orders_count"] += 1
        b["plans"].add(o["plan_chosen"])
        if not b["first_name"]:
            try:
                b["first_name"] = json.loads(o["intake_json"]).get("firstName")
            except Exception:
                pass
        if o["status"] in PAID_STATUSES:
            b["paid_count"] += 1
            b["paid_plans"].append(o["plan_chosen"])
        ts = o["updated_at"] or o["created_at"]
        if ts and (b["last_activity"] is None or ts > b["last_activity"]):
            b["last_activity"] = ts

    for s in subs:
        b = _bucket(s["email"])
        b["has_subscriber"] = True
        # Stato "migliore": active prevale su cancelled/past_due nella vista lista.
        if b["subscription_status"] in (None, "cancelled"):
            b["subscription_status"] = s["subscription_status"]
        if not b["first_name"] and s["first_name"]:
            b["first_name"] = s["first_name"]
        b["plans"].add(s["plan"])

    out = []
    for b in cust.values():
        b["plans"] = sorted(b["plans"])
        out.append(b)

    if search:
        q = search.lower().strip()
        out = [b for b in out
               if q in (b["email"] or "").lower()
               or q in (b["first_name"] or "").lower()]

    out.sort(key=lambda b: b["last_activity"] or "", reverse=True)
    return out


def get_customer_orders(email: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            """SELECT id, plan_chosen, email, status, stripe_subscription_id,
                      stripe_customer_id, pdf_path, error, affiliate_ref,
                      utm_source, utm_medium, utm_campaign, referrer, landing_page,
                      intake_json, targets_json, created_at, updated_at, anonymized_at
               FROM orders WHERE LOWER(email)=? ORDER BY created_at DESC""",
            (email.lower(),),
        ).fetchall()


def get_customer_subscribers(email: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM subscribers WHERE LOWER(email)=? ORDER BY created_at DESC",
            (email.lower(),),
        ).fetchall()


def get_renewals_for_subscriptions(sub_ids: list[str]) -> list[sqlite3.Row]:
    if not sub_ids:
        return []
    placeholders = ",".join("?" for _ in sub_ids)
    with _conn() as c:
        return c.execute(
            f"""SELECT stripe_invoice_id, stripe_subscription_id, plan, amount_cents,
                       currency, created_at
                FROM renewals WHERE stripe_subscription_id IN ({placeholders})
                ORDER BY created_at DESC""",
            tuple(sub_ids),
        ).fetchall()


def update_intake_fields(scope: str, record_id: str, fields: dict) -> bool:
    """
    Aggiornamento manuale dei dati cliente. `scope` ∈ {'order','subscriber'}.
    Fa merge di `fields` dentro intake_json. Se vengono passati 'email' o
    'firstName'/'first_name' sincronizza anche le colonne dedicate.
    Ritorna False se il record non esiste.
    """
    table = "orders" if scope == "order" else "subscribers"
    with _conn() as c:
        row = c.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        if not row:
            return False
        intake = json.loads(row["intake_json"])
        for k, v in fields.items():
            intake[k] = v
        # Sincronizza colonne dedicate.
        new_email = fields.get("email")
        new_first = fields.get("firstName") or fields.get("first_name")
        sets = ["intake_json=?"]
        args: list = [json.dumps(intake)]
        if new_email is not None:
            sets.append("email=?")
            args.append(new_email)
        if scope == "subscriber" and new_first is not None:
            sets.append("first_name=?")
            args.append(new_first)
        if scope == "order":
            sets.append("updated_at=?")
            args.append(_now_iso())
        args.append(record_id)
        c.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id=?", tuple(args))
    return True


# ── GDPR: cancellazione dati cliente ─────────────────────────────────────────

def _anonymized_intake(raw_json: str) -> str:
    """Rimpiazza gli identificatori diretti e i dati sanitari con placeholder
    validi, mantenendo uno schema IntakeRequest valido (serve per i record paid
    che restano per la contabilità)."""
    try:
        d = json.loads(raw_json)
    except Exception:
        d = {}
    d["firstName"] = "(rimosso)"
    d["email"] = "deleted@gdpr.local"
    d["age"] = 18
    d["height"] = 170
    d["weight"] = 70.0
    d["targetWeight"] = None
    d["allergies"] = []
    d["dislikes"] = None
    d["affiliateRef"] = None
    return json.dumps(d)


def gdpr_delete_customer(email: str) -> dict:
    """
    Cancellazione GDPR. Strategia (scelta operativa):
      - Ordini PAGATI (paid/generating/sent): anonimizzati — PII e dati sanitari
        rimossi, ma importo/piano/date conservati per obblighi contabili e per la
        riconciliazione con Stripe.
      - Ordini NON pagati (test/abbandonati): hard-delete.
      - Subscriber: PII anonimizzata; stripe_ids mantenuti per la fatturazione.
    Ritorna un riassunto + lista pdf_path da rimuovere dal disco.
    """
    em = email.lower()
    now = _now_iso()
    anonymized = 0
    deleted = 0
    pdf_paths: list[str] = []
    active_subs = 0
    with _conn() as c:
        orders = c.execute(
            "SELECT id, status, pdf_path, intake_json FROM orders WHERE LOWER(email)=?",
            (em,),
        ).fetchall()
        for o in orders:
            if o["pdf_path"]:
                pdf_paths.append(o["pdf_path"])
            if o["status"] in PAID_STATUSES:
                c.execute(
                    """UPDATE orders
                       SET email='deleted@gdpr.local', intake_json=?, error=NULL,
                           pdf_path=NULL, anonymized_at=?, updated_at=?
                       WHERE id=?""",
                    (_anonymized_intake(o["intake_json"]), now, now, o["id"]),
                )
                anonymized += 1
            else:
                c.execute("DELETE FROM orders WHERE id=?", (o["id"],))
                deleted += 1

        subs = c.execute(
            "SELECT id, subscription_status, intake_json FROM subscribers WHERE LOWER(email)=?",
            (em,),
        ).fetchall()
        for s in subs:
            if s["subscription_status"] == "active":
                active_subs += 1
            c.execute(
                """UPDATE subscribers
                   SET email='deleted@gdpr.local', first_name='(rimosso)', intake_json=?
                   WHERE id=?""",
                (_anonymized_intake(s["intake_json"]), s["id"]),
            )
            anonymized += 1
    return {
        "orders_anonymized": anonymized,
        "orders_deleted": deleted,
        "active_subscribers": active_subs,
        "pdf_paths": pdf_paths,
    }


# ════════════════════════════════════════════════════════════════════════════
# Admin: traffico e sorgenti (per decidere dove investire in promo)
# ════════════════════════════════════════════════════════════════════════════

# Prezzi in centesimi per la stima ricavi-per-sorgente direttamente in SQL.
_REVENUE_CASE = (
    "CASE plan_chosen WHEN 'base' THEN 1900 "
    "WHEN 'completo' THEN 2900 WHEN 'coach' THEN 9900 ELSE 0 END"
)
_PAID_IN = "('paid','generating','sent')"


def traffic_by_source() -> list[sqlite3.Row]:
    """
    Aggregato per sorgente/medium/campagna: visite-form (signups), conversioni
    pagate e ricavo di acquisizione (primo pagamento). I rinnovi ricorrenti non
    sono inclusi qui — questa è la metrica di ACQUISIZIONE, quella che conta per
    decidere la spesa pubblicitaria.
    """
    with _conn() as c:
        return c.execute(
            f"""SELECT
                  COALESCE(NULLIF(utm_source,''), 'diretto/organico') AS source,
                  COALESCE(NULLIF(utm_medium,''), '(nessuno)')        AS medium,
                  COALESCE(NULLIF(utm_campaign,''), '(nessuna)')      AS campaign,
                  COUNT(*) AS signups,
                  SUM(CASE WHEN status IN {_PAID_IN} THEN 1 ELSE 0 END) AS paid,
                  SUM(CASE WHEN status IN {_PAID_IN} THEN {_REVENUE_CASE} ELSE 0 END) AS revenue_cents
                FROM orders
                GROUP BY source, medium, campaign
                ORDER BY revenue_cents DESC, signups DESC"""
        ).fetchall()


def traffic_by_referrer() -> list[sqlite3.Row]:
    """Rollup dei referrer per il traffico SENZA utm_source (organico/diretto):
    aiuta a vedere quali siti esterni portano clienti senza tag campagna."""
    with _conn() as c:
        return c.execute(
            f"""SELECT
                  COALESCE(NULLIF(referrer,''), '(diretto)') AS referrer,
                  COUNT(*) AS signups,
                  SUM(CASE WHEN status IN {_PAID_IN} THEN 1 ELSE 0 END) AS paid,
                  SUM(CASE WHEN status IN {_PAID_IN} THEN {_REVENUE_CASE} ELSE 0 END) AS revenue_cents
                FROM orders
                WHERE utm_source IS NULL OR utm_source=''
                GROUP BY referrer
                ORDER BY revenue_cents DESC, signups DESC"""
        ).fetchall()


def traffic_totals() -> dict:
    """Totali complessivi per le KPI in cima al tab Traffico."""
    with _conn() as c:
        row = c.execute(
            f"""SELECT
                  COUNT(*) AS signups,
                  SUM(CASE WHEN status IN {_PAID_IN} THEN 1 ELSE 0 END) AS paid,
                  SUM(CASE WHEN status IN {_PAID_IN} THEN {_REVENUE_CASE} ELSE 0 END) AS revenue_cents
                FROM orders"""
        ).fetchone()
    return {
        "signups": row["signups"] or 0,
        "paid": row["paid"] or 0,
        "revenue_cents": row["revenue_cents"] or 0,
    }
