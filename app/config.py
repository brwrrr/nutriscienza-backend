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

    # App
    base_url: str = "https://nutriscienza.org"
    database_path: str = "./data/orders.db"
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
