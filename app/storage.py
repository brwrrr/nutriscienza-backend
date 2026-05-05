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


def create_order(intake: IntakeRequest, targets: NutritionTargets) -> str:
    order_id = "ord_" + uuid.uuid4().hex[:16]
    with _conn() as c:
        c.execute(
            """INSERT INTO orders (id, intake_json, targets_json, plan_chosen, email,
                                   status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending_payment', ?, ?)""",
            (
                order_id,
                intake.model_dump_json(by_alias=True),
                targets.model_dump_json(),
                intake.plan,
                intake.email,
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
                last_plan_sent_at, next_plan_due_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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


def mark_plan_sent(subscriber_id: str) -> None:
    """Called after a refresh plan has been successfully emailed. Advances the clock 30 days."""
    from datetime import timedelta
    now_dt = datetime.now(timezone.utc)
    next_due = (now_dt + timedelta(days=30)).isoformat()
    with _conn() as c:
        c.execute(
            """UPDATE subscribers
               SET last_plan_sent_at=?, next_plan_due_at=?, plan_month=plan_month+1
               WHERE id=?""",
            (now_dt.isoformat(), next_due, subscriber_id),
        )


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
