from typing import Any, Literal
from pydantic import BaseModel, Field, HttpUrl

class EnrichmentRequest(BaseModel):
    company_domain: str | None = None
    company_name: str | None = None
    job_titles: list[str] = Field(default_factory=list)
    max_contacts: int = Field(default=5, ge=1, le=50)

class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"

class EnrichmentSummary(BaseModel):
    company_id: str | None = None
    contacts_found: int = 0
    contacts_enriched: int = 0
    warnings: list[str] = Field(default_factory=list)

class WebhookEvent(BaseModel):
    objectId: str | int | None = None
    subscriptionType: str | None = None
    objectTypeId: str | None = None
    propertyName: str | None = None
    propertyValue: Any | None = None
    eventId: int | str | None = None
    portalId: int | None = None
    appId: int | None = None
    occurredAt: int | None = None
    attemptNumber: int | None = None
