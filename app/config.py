from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Forager HubSpot Enrichment"
    environment: str = "development"
    log_level: str = "INFO"
    hubspot_access_token: str = ""
    hubspot_client_secret: str = ""
    hubspot_api_base_url: str = "https://api.hubapi.com"
    forager_api_key: str = ""
    forager_account_id: str = ""
    forager_api_base_url: str = "https://api-v2.forager.ai"
    allowed_job_titles: str = "Engineer,CEO,CTO,Founder,Chief Technology Officer,VP Engineering,Head of Engineering,Director of Engineering"
    max_contacts_per_company: int = Field(default=5, ge=1, le=50)
    webhook_max_age_seconds: int = Field(default=300, ge=30, le=3600)
    enable_webhook_signature_validation: bool = True

    @property
    def job_title_terms(self) -> list[str]:
        return [x.strip() for x in self.allowed_job_titles.split(",") if x.strip()]

    @model_validator(mode="after")
    def validate_production_secrets(self):
        if self.environment.lower() == "production":
            if not self.hubspot_access_token:
                raise ValueError("HUBSPOT_ACCESS_TOKEN is required in production")
            if not self.forager_api_key or not self.forager_account_id:
                raise ValueError("FORAGER_API_KEY and FORAGER_ACCOUNT_ID are required in production")
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()
