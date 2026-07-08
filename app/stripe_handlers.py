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


def create_checkout_session(
    order_id: str,
    plan: Plan,
    email: str,
    affiliate_ref: str | None = None,
) -> stripe.checkout.Session:
    """Crea una Stripe Checkout Session per l'ordine."""
    price_id = settings.price_id_for_plan[plan]
    mode = PLAN_MODE[plan]

    base_metadata: dict[str, str] = {"order_id": order_id, "plan": plan}
    if affiliate_ref:
        base_metadata["affiliate_ref"] = affiliate_ref

    session = stripe.checkout.Session.create(
        mode=mode,
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email,
        success_url=f"{settings.base_url}/grazie?order_id={order_id}&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.base_url}/questionario.html?annullato=1",
        metadata=base_metadata,
        # In subscription mode i metadata della session non si propagano in automatico
        # alla subscription — li mettiamo anche nei subscription_data per coerenza.
        # Questo garantisce che affiliate_ref sopravviva ai rinnovi via subscription.metadata.
        subscription_data=(
            {"metadata": base_metadata}
            if mode == "subscription" else None
        ),
        locale="it",
        billing_address_collection="auto",
        allow_promotion_codes=True,
    )
    return session


def verify_webhook(payload: bytes, sig_header: str) -> stripe.Event:
    """
    Verifica la firma del webhook e ritorna l'evento parsato.

    Prova prima il secret live, poi quello test (se configurato): test mode e
    live mode firmano con whsec_ diversi, e lo stesso endpoint riceve entrambi.
    """
    secrets = [
        s for s in (settings.stripe_webhook_secret, settings.stripe_webhook_secret_test) if s
    ]
    last_err: Exception | None = None
    for secret in secrets:
        try:
            return stripe.Webhook.construct_event(payload, sig_header, secret)
        except stripe.error.SignatureVerificationError as e:
            last_err = e
    raise last_err or ValueError("Nessun webhook secret configurato")


def create_portal_session(customer_id: str, return_url: str) -> stripe.billing_portal.Session:
    """
    Crea una Stripe Customer Portal Session.

    Permette al subscriber di gestire l'abbonamento in self-service
    (annullamento, aggiornamento carta, fatture). Riduce ticket al supporto
    e dispute "couldn't cancel" — entrambe killer di profittabilità.

    Configurazione richiesta una tantum sul Stripe Dashboard:
      Settings → Billing → Customer portal → Activate
        - Cancel subscriptions: ON, "at end of billing period" (allinea ai T&C)
        - Update payment method: ON
        - View invoices: ON
        - Cancellation reason: ON (raccoglie segnale per ridurre churn)
    """
    return stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
        locale="it",
    )
