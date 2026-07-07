"""Configurazione centralizzata via variabili d'ambiente."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Stripe
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_base: str
    stripe_price_completo: str
    stripe_price_coach: str

    # Anthropic
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-6"

    # Resend
    resend_api_key: str
    from_email: str = "NutriScienza <piani@nutriscienza.org>"
    support_email: str = "supporto@nutriscienza.org"

    # Upsell Base → Completo (email automatica ~1 mese dopo l'acquisto Base)
    # Per attivare lo sconto "primo mese a €24": crea una Promotion Code su Stripe
    # (es. €5 off, first invoice only, once per customer) e metti QUI il CODICE che
    # il cliente digita al checkout (non il promo_..._id). Se vuoto, l'email NON
    # menziona lo sconto e propone il Completo a prezzo pieno.
    base_upsell_promo_code: str = ""       # es. "RICALCOLO24"
    base_upsell_offer_price: str = "24"    # prezzo scontato primo mese (€)
    base_upsell_full_price: str = "29"     # prezzo pieno Completo (€)
    # Finestra di invio (giorni dall'acquisto). Il cron gira ogni giorno: l'ampiezza
    # 30–45 garantisce copertura anche se un run salta, senza mai ri-inviare.
    base_upsell_min_days: int = 30
    base_upsell_max_days: int = 45

    # App
    base_url: str = "https://nutriscienza.org"
    database_path: str = "./data/orders.db"
    pdf_storage_dir: str = "./data/pdfs"   # In prod su Render: /var/data/pdfs (disco persistente)
    environment: str = "development"

    # Admin
    admin_api_key: str = ""          # Bearer token per /api/admin/* — obbligatorio in prod
    admin_email: str = ""            # Se vuoto usa support_email

    # Monitoring
    sentry_dsn: str = ""             # Lascia vuoto per disabilitare

    @property
    def admin_notify_email(self) -> str:
        return self.admin_email or self.support_email

    @property
    def price_id_for_plan(self) -> dict[str, str]:
        return {
            "base": self.stripe_price_base,
            "completo": self.stripe_price_completo,
            "coach": self.stripe_price_coach,
        }


settings = Settings()
