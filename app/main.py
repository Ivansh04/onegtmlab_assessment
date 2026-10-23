import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Query
from app.config import get_settings
from app.logging_config import configure_logging
from app.clients.forager import ForagerClient, ForagerError, ForagerPersonNotFound
from app.clients.hubspot import HubSpotClient, HubSpotError
from app.schemas import HealthResponse, EnrichmentRequest
from app.security import verify_hubspot_signature
from app.services.enrichment import EnrichmentService

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.forager = ForagerClient(settings)
    app.state.hubspot = HubSpotClient(settings)
    app.state.enrichment = EnrichmentService(settings, app.state.forager, app.state.hubspot)
    yield
    await app.state.forager.close()
    await app.state.hubspot.close()

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

@app.get("/health", response_model=HealthResponse, tags=["operations"])
async def health():
    return HealthResponse()

@app.get("/", tags=["operations"])
async def root():
    return {"service": settings.app_name, "status": "running", "docs": "/docs"}

@app.post("/api/enrich/company/{company_id}", tags=["enrichment"])
async def enrich_company(company_id: str):
    try:
        return await app.state.enrichment.enrich_company(company_id)
    except (ForagerError, HubSpotError) as exc:
        logger.warning("Company enrichment failed company_id=%s error=%s", company_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Upstream enrichment failed") from exc

@app.post("/api/enrich/contact/{contact_id}", tags=["enrichment"])
async def enrich_contact(contact_id: str):
    try:
        await app.state.enrichment.enrich_contact(contact_id)
        return {"contact_id": contact_id, "status": "enriched"}
    except ForagerPersonNotFound as exc:
        logger.info("Contact profile not found contact_id=%s", contact_id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ForagerError, HubSpotError) as exc:
        logger.warning("Contact enrichment failed contact_id=%s error=%s", contact_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Upstream enrichment failed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.post("/api/find-contacts", tags=["enrichment"])
async def find_contacts(payload: EnrichmentRequest):
    if not payload.company_name and not payload.company_domain:
        raise HTTPException(status_code=422, detail="company_name or company_domain is required")
    titles = payload.job_titles or settings.job_title_terms
    found: list[dict] = []
    seen: set[str] = set()
    for title in titles:
        results = await app.state.forager.search_people(
            payload.company_name or payload.company_domain or "",
            title,
            company_domain=payload.company_domain,
        )
        for item in results:
            person = item.get("person", item)
            org = item.get("organization", {})
            employer = (org.get("name") or "").casefold()
            if payload.company_name and employer and employer != payload.company_name.casefold():
                continue
            identifier = str(person.get("id") or person.get("linkedin_url") or person.get("full_name") or "")
            if not identifier or identifier in seen: continue
            seen.add(identifier)
            found.append({
                "name": person.get("full_name") or person.get("name"),
                "job_title": item.get("role_title") or item.get("title") or title,
                "company": org.get("name") or payload.company_name,
                "linkedin_url": person.get("linkedin_url") or person.get("profile_url"),
                "forager_person_id": person.get("id"),
            })
            if len(found) >= min(payload.max_contacts, settings.max_contacts_per_company): break
        if len(found) >= min(payload.max_contacts, settings.max_contacts_per_company): break
    return {"count": len(found), "contacts": found, "job_titles_used": titles}

@app.post("/webhooks/hubspot", tags=["webhooks"])
async def hubspot_webhook(request: Request):
    raw = await request.body()
    verify_hubspot_signature(request, raw, settings)
    try:
        events = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(events, list):
        events = [events]
    # Acknowledge quickly. For this starter, process inline; move to durable queue before high-volume use.
    results = []
    for event in events:
        object_id = event.get("objectId")
        event_type = (event.get("subscriptionType") or "").lower()
        object_type = (event.get("objectTypeId") or "").lower()
        try:
            if object_id is None:
                results.append({"status": "ignored", "reason": "missing objectId"})
            elif "company" in event_type or object_type == "0-2":
                summary = await app.state.enrichment.enrich_company(str(object_id))
                results.append({"object_id": str(object_id), "status": "processed", "summary": summary.model_dump()})
            elif "contact" in event_type or object_type == "0-1":
                await app.state.enrichment.enrich_contact(str(object_id))
                results.append({"object_id": str(object_id), "status": "processed"})
            else:
                results.append({"object_id": str(object_id), "status": "ignored", "reason": "unsupported event"})
        except Exception as exc:
            logger.warning("Webhook event processing failed object_id=%s error=%s", object_id, type(exc).__name__)
            results.append({"object_id": str(object_id), "status": "failed", "reason": "processing error"})
    return {"received": len(events), "results": results}
