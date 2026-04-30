"""
Integrazione Stripe — Checkout Session + Webhook.

Flusso:
  1. POST /api/intake crea l'ordine, poi questa funzione crea la Checkout Session.
  2. Cliente paga su Stripe Checkout (hosted, PCI scope minimo).
  3. Stripe chiama POST /api/stripe/webhook con event `checkout.session.completed`.
  4. Verifichiamo la firma, recuperiamo l'order_id dai metadata, e triggeriamo la generazione.
"""
import stripe

from .config import settings
from .models import Plan

stripe.api_key = settings.stripe_secret_key


# Modalità di checkout per piano:
#   - base: one-time payment (€19 una tantum)
#   - completo, coach: subscription
PLAN_MODE: dict[str, str] = {
    "base": "payment",
    "completo": "subscription",
    "coach": "subscription",
}


def create_checkout_session(order_id: str, plan: Plan, email: str) -> stripe.checkout.Session:
    """Crea una Stripe Checkout Session per l'ordine."""
    price_id = settings.price_id_for_plan[plan]
    mode = PLAN_MODE[plan]

    session = stripe.checkout.Session.create(
        mode=mode,
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email,
        success_url=f"{settings.base_url}/grazie?order_id={order_id}&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.base_url}/questionario.html?annullato=1",
        metadata={"order_id": order_id, "plan": plan},
        # In subscription mode i metadata della session non si propagano in automatico
        # alla subscription — li mettiamo anche nei subscription_data per coerenza.
        subscription_data=(
            {"metadata": {"order_id": order_id, "plan": plan}}
            if mode == "subscription" else None
        ),
        locale="it",
        billing_address_collection="auto",
        allow_promotion_codes=True,
    )
    return session


def verify_webhook(payload: bytes, sig_header: str) -> stripe.Event:
    """Verifica la firma del webhook e ritorna l'evento parsato."""
    return stripe.Webhook.construct_event(
        payload, sig_header, settings.stripe_webhook_secret
    )
