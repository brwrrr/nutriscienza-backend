"""
Programma affiliati — modulo isolato.

Principio di design: nessuna funzione qui solleva eccezioni verso il chiamante.
Ogni operazione affiliate è "best effort" — se fallisce, il flusso pagamento
sottostante deve continuare intatto. I chiamanti DEVONO comunque wrappare in
try/except per ulteriore difesa.

Tracking:
  - L'affiliate_ref viene catturato dal frontend (?ref=CODE in URL),
    salvato in localStorage per 60 giorni, poi inviato dentro IntakeRequest.
  - Backend lo propaga ai metadata di Stripe Checkout Session e
    Subscription, così le invoice di rinnovo lo conservano automaticamente.

Ricavi:
  - Commissione 30% su ogni pagamento (lifetime per i piani ricorrenti).
  - 30 giorni di hold prima che la commission diventi "payable" (copre la
    finestra rimborsi Stripe + buffer).
  - Reversal automatico su charge.refunded.

Idempotenza:
  - Ogni commission è ancorata a uno Stripe object ID univoco
    (invoice_id per subscription, checkout_session_id per Base).
  - La UNIQUE constraint nel DB previene doppi accrediti.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import storage
from .config import settings

log = logging.getLogger("nutriscienza.affiliate")

# Commissione % per piano. Modificabile da admin in futuro.
DEFAULT_COMMISSION_RATE = 0.30

# Hold prima che una commission diventi pagabile (copre rimborsi 14gg + buffer).
COMMISSION_HOLD_DAYS = 30

# Prezzi in centesimi — single source of truth.
# Allineato a main.PLAN_PRICE_CENTS ma duplicato qui per evitare import circolari.
PLAN_PRICE_CENTS: dict[str, int] = {
    "base": 1900,
    "completo": 2900,
    "coach": 9900,
}


# ── Codice referral ───────────────────────────────────────────────────────────

def _generate_ref_code(length: int = 8) -> str:
    """Codice referral leggibile, no caratteri ambigui (0/O, 1/l/I)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalize_ref(ref: Optional[str]) -> Optional[str]:
    """Pulisce e uppercase un ref code da input utente."""
    if not ref:
        return None
    cleaned = ref.strip().upper()
    if not cleaned or len(cleaned) > 40:
        return None
    # Solo alfanumerico + dash/underscore
    if not all(c.isalnum() or c in "-_" for c in cleaned):
        return None
    return cleaned


# ── Creazione affiliato ──────────────────────────────────────────────────────

def create_affiliate(
    name: str,
    email: str,
    payout_method: str = "manual",
    commission_rate: float = DEFAULT_COMMISSION_RATE,
    custom_code: Optional[str] = None,
) -> dict:
    """
    Crea un nuovo affiliato. Il ref code è auto-generato se non fornito.
    Garantisce unicità del code provando fino a 5 volte.
    Solleva ValueError se email già presente o code custom collide.
    """
    if storage.get_affiliate_by_email(email):
        raise ValueError(f"Affiliate con email {email} esiste già")

    if custom_code:
        ref_code = normalize_ref(custom_code)
        if not ref_code:
            raise ValueError("Codice custom non valido")
        if storage.get_affiliate_by_ref(ref_code):
            raise ValueError(f"Codice {ref_code} già in uso")
    else:
        for _ in range(5):
            candidate = _generate_ref_code()
            if not storage.get_affiliate_by_ref(candidate):
                ref_code = candidate
                break
        else:
            raise RuntimeError("Impossibile generare codice univoco")

    aff_id = storage.create_affiliate(
        name=name,
        email=email,
        ref_code=ref_code,
        commission_rate=commission_rate,
        payout_method=payout_method,
    )
    return {
        "id": aff_id,
        "ref_code": ref_code,
        "referral_url": f"{settings.base_url}/?ref={ref_code}",
    }


# ── Validazione pre-checkout ─────────────────────────────────────────────────

def validate_ref_for_checkout(ref: Optional[str]) -> Optional[str]:
    """
    Chiamato in /api/intake. Ritorna il ref code se valido + affiliate attivo,
    altrimenti None. Mai solleva — un ref invalido non deve impedire l'acquisto.
    """
    try:
        normalized = normalize_ref(ref)
        if not normalized:
            return None
        aff = storage.get_affiliate_by_ref(normalized)
        if not aff or aff["status"] != "active":
            return None
        return normalized
    except Exception:
        log.exception("validate_ref_for_checkout fallita per ref=%r", ref)
        return None


# ── Booking commissioni ──────────────────────────────────────────────────────

def record_commission_oneshot(
    *,
    order_id: str,
    affiliate_ref: str,
    plan: str,
    stripe_session_id: str,
    stripe_customer_id: Optional[str],
    customer_email: str,
) -> Optional[str]:
    """
    Per piani one-shot (Base €19) — chiamato su checkout.session.completed.
    Idempotente via UNIQUE su stripe_event_ref.
    """
    try:
        aff = storage.get_affiliate_by_ref(affiliate_ref)
        if not aff or aff["status"] != "active":
            log.info("Ref %s non attivo — nessuna commission", affiliate_ref)
            return None

        # Self-referral block: l'affiliate non può guadagnare su se stesso.
        if aff["email"].lower() == customer_email.lower():
            log.warning("Self-referral bloccato: aff=%s order=%s", affiliate_ref, order_id)
            return None

        gross_cents = PLAN_PRICE_CENTS.get(plan, 0)
        if gross_cents == 0:
            log.error("Plan %s sconosciuto", plan)
            return None

        rate = float(aff["commission_rate"])
        commission_cents = int(gross_cents * rate)

        now = datetime.now(timezone.utc)
        payable_at = now + timedelta(days=COMMISSION_HOLD_DAYS)

        commission_id = storage.create_commission(
            affiliate_id=aff["id"],
            stripe_event_ref=f"cs:{stripe_session_id}",  # idempotency key
            order_id=order_id,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=None,
            plan=plan,
            gross_amount_cents=gross_cents,
            commission_amount_cents=commission_cents,
            currency="eur",
            status="pending",
            earned_at=now.isoformat(),
            payable_at=payable_at.isoformat(),
        )
        if commission_id:
            log.info(
                "Commission registrata: aff=%s plan=%s amount=%d cents",
                affiliate_ref, plan, commission_cents,
            )
        return commission_id
    except Exception:
        log.exception("record_commission_oneshot fallita order=%s ref=%s", order_id, affiliate_ref)
        return None


def record_commission_subscription(
    *,
    stripe_invoice_id: str,
    stripe_subscription_id: str,
    stripe_customer_id: Optional[str],
    amount_paid_cents: int,
    currency: str,
) -> Optional[str]:
    """
    Per piani subscription (Completo €29/mese, Coach €99/mese).
    Chiamato su invoice.payment_succeeded (sia primo pagamento che rinnovi).

    Recupera affiliate_ref dal subscriber (popolato sul primo pagamento)
    e crea una commission ancorata all'invoice. UNIQUE su invoice_id previene duplicati.
    """
    try:
        sub_row = storage.get_subscriber_by_stripe_sub(stripe_subscription_id)
        if not sub_row:
            # Possibile race: webhook arriva prima che il subscriber sia creato
            # dalla pipeline di generazione. In questo caso tentiamo lookup
            # diretto dall'order via stripe_subscription_id.
            order_aff = storage.get_order_affiliate_ref_by_subscription(stripe_subscription_id)
            if not order_aff:
                return None
            affiliate_ref = order_aff["affiliate_ref"]
            customer_email = order_aff["email"]
            plan = order_aff["plan_chosen"]
        else:
            affiliate_ref = sub_row["affiliate_ref"] if "affiliate_ref" in sub_row.keys() else None
            customer_email = sub_row["email"]
            plan = sub_row["plan"]

        if not affiliate_ref:
            return None

        aff = storage.get_affiliate_by_ref(affiliate_ref)
        if not aff or aff["status"] != "active":
            return None

        if aff["email"].lower() == customer_email.lower():
            log.warning("Self-referral bloccato sub=%s", stripe_subscription_id)
            return None

        rate = float(aff["commission_rate"])
        commission_cents = int(amount_paid_cents * rate)

        now = datetime.now(timezone.utc)
        payable_at = now + timedelta(days=COMMISSION_HOLD_DAYS)

        commission_id = storage.create_commission(
            affiliate_id=aff["id"],
            stripe_event_ref=f"inv:{stripe_invoice_id}",  # idempotency key
            order_id=None,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
            plan=plan,
            gross_amount_cents=amount_paid_cents,
            commission_amount_cents=commission_cents,
            currency=currency,
            status="pending",
            earned_at=now.isoformat(),
            payable_at=payable_at.isoformat(),
        )
        if commission_id:
            log.info(
                "Commission ricorrente: aff=%s sub=%s amount=%d cents",
                affiliate_ref, stripe_subscription_id, commission_cents,
            )
        return commission_id
    except Exception:
        log.exception("record_commission_subscription fallita inv=%s", stripe_invoice_id)
        return None


def reverse_commission_for_charge(stripe_invoice_id: Optional[str], stripe_charge_id: Optional[str]) -> int:
    """
    Chiamato su charge.refunded. Marca le commissioni associate come 'reversed'.
    Ritorna il numero di commissioni reversed.
    """
    try:
        n = 0
        if stripe_invoice_id:
            n += storage.reverse_commission_by_event_ref(f"inv:{stripe_invoice_id}")
        # Per Base (one-shot) Stripe fornisce charge_id ma non invoice_id —
        # li riconciliamo via stripe_customer_id se necessario in futuro.
        return n
    except Exception:
        log.exception("reverse_commission_for_charge fallita")
        return 0


# ── Approvazione automatica delle commissioni mature ─────────────────────────

def approve_matured_commissions() -> int:
    """
    Esegui via cron (tipicamente ogni ora) — promuove le commissioni 'pending'
    con payable_at <= now a 'approved', pronte per il prossimo payout.
    Le commissioni 'reversed' restano tali.
    """
    try:
        return storage.approve_pending_commissions_due()
    except Exception:
        log.exception("approve_matured_commissions fallita")
        return 0


# ── Statistiche per dashboard affiliato ──────────────────────────────────────

def affiliate_stats(affiliate_id: str) -> dict:
    """Aggregato per la dashboard /affiliate.html."""
    pending = storage.sum_commissions(affiliate_id, status="pending")
    approved = storage.sum_commissions(affiliate_id, status="approved")
    paid = storage.sum_commissions(affiliate_id, status="paid")
    reversed_ = storage.sum_commissions(affiliate_id, status="reversed")
    referrals = storage.count_unique_referrals(affiliate_id)
    return {
        "pending_cents": pending,
        "approved_cents": approved,
        "paid_cents": paid,
        "reversed_cents": reversed_,
        "lifetime_earned_cents": pending + approved + paid,
        "referrals_count": referrals,
    }
