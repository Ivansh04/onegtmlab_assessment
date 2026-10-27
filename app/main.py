import logging
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, HTTPException, Request, Query
from app.config import get_settings
from app.logging_config import configure_logging
from app.clients.forager import (
    ForagerClient,
    ForagerCreditsExhausted,
    ForagerError,
    ForagerPersonNotFound,
)
from app.clients.hubspot import HubSpotClient, HubSpotError
from app.schemas import HealthResponse, EnrichmentRequest
from app.security import verify_hubspot_signature
from app.security import verify_app_secret
from app.background import enqueue
from app.services.enrichment import EnrichmentService, _linkedin_url

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.background_tasks = set()
    app.state.forager = ForagerClient(settings)
    app.state.hubspot = HubSpotClient(settings)
    app.state.enrichment = EnrichmentService(settings, app.state.forager, app.state.hubspot)
    yield
    await app.state.forager.close()
    await app.state.hubspot.close()
    for task in app.state.background_tasks:
        task.cancel()

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

@app.get("/health", response_model=HealthResponse, tags=["operations"])
async def health():
    return HealthResponse()

@app.get("/", tags=["operations"])
async def root():
    return {"service": settings.app_name, "status": "running", "docs": "/docs"}

@app.post("/api/enrich/company/{company_id}", tags=["enrichment"])
async def enrich_company(company_id: str, request: Request):
    verify_app_secret(request, settings)
    try:
        return await app.state.enrichment.enrich_company(company_id)
    except (ForagerError, HubSpotError) as exc:
        logger.warning("Company enrichment failed company_id=%s error=%s", company_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Upstream enrichment failed") from exc

@app.post("/api/enrich/contact/{contact_id}", tags=["enrichment"])
async def enrich_contact(contact_id: str, request: Request):
    verify_app_secret(request, settings)
    try:
        reveal_contacts = request.query_params.get("reveal_contacts", "").lower() == "true"
        await app.state.enrichment.enrich_contact(
            contact_id,
            reveal_contacts=reveal_contacts,
        )
        return {
            "contact_id": contact_id,
            "status": "enriched",
            "paid_lookups": reveal_contacts,
        }
    except ForagerPersonNotFound as exc:
        logger.info("Contact profile not found contact_id=%s", contact_id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ForagerError, HubSpotError) as exc:
        logger.warning("Contact enrichment failed contact_id=%s error=%s", contact_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Upstream enrichment failed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.post("/api/find-contacts", tags=["enrichment"])
async def find_contacts(payload: EnrichmentRequest, request: Request):
    verify_app_secret(request, settings)
    if not payload.company_name and not payload.company_domain:
        raise HTTPException(status_code=422, detail="company_name or company_domain is required")
    titles = payload.job_titles or settings.job_title_terms
    found: list[dict] = []
    seen: set[str] = set()
    try:
        for title in titles:
            results = await app.state.forager.search_people(
                payload.company_name or payload.company_domain or "",
                title,
                company_domain=payload.company_domain,
            )
            for item in results:
                person = item.get("person", item)
                if not isinstance(person, dict):
                    continue
                org = person.get("organization") or item.get("organization", {})
                if not isinstance(org, dict):
                    org = {}
                employer = (org.get("name") or "").casefold()
                if payload.company_name and employer and employer != payload.company_name.casefold():
                    continue
                linkedin_url = _linkedin_url(item, person)
                identifier = str(
                    person.get("id")
                    or person.get("person_id")
                    or linkedin_url
                    or person.get("full_name")
                    or person.get("name")
                    or ""
                )
                if not identifier or identifier in seen: continue
                seen.add(identifier)
                found.append({
                    "name": person.get("full_name") or person.get("name"),
                    "job_title": (
                        item.get("role_title")
                        or item.get("title")
                        or person.get("role_title")
                        or person.get("title")
                        or title
                    ),
                    "company": org.get("name") or payload.company_name,
                    "linkedin_url": linkedin_url,
                    "forager_person_id": person.get("id") or person.get("person_id") or item.get("person_id"),
                })
                if len(found) >= min(payload.max_contacts, settings.max_contacts_per_company): break
            if len(found) >= min(payload.max_contacts, settings.max_contacts_per_company): break
    except ForagerCreditsExhausted as exc:
        logger.info("Contact discovery unavailable because Forager credits are exhausted")
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except ForagerError as exc:
        logger.warning("Contact discovery failed error=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Contact discovery failed upstream") from exc
    return {"count": len(found), "contacts": found, "job_titles_used": titles}

@app.post("/webhooks/hubspot", tags=["webhooks"])
async def hubspot_webhook(request: Request):
    raw = await request.body()
    verify_app_secret(request, settings)
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
            if _is_import_event(event):
                results.append({"object_id": str(object_id), "status": "ignored", "reason": "import event"})
                continue
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


def _is_import_event(event: dict) -> bool:
    if not settings.skip_import_webhooks:
        return False
    source = str(event.get("changeSource") or "").upper()
    return source in settings.import_sources


async def _process_event(event: dict[str, Any]) -> None:
    object_id = event.get("objectId")
    subscription = (event.get("subscriptionType") or "").lower()
    object_type = (event.get("objectTypeId") or "").lower()
    if object_id is None:
        return
    if "company" in subscription or object_type == "0-2":
        await app.state.enrichment.enrich_company(str(object_id))
    elif "contact" in subscription or object_type == "0-1":
        await app.state.enrichment.enrich_contact(str(object_id))


async def _queued_webhook(request: Request, route: str):
    raw = await request.body()
    verify_app_secret(request, settings)
    verify_hubspot_signature(request, raw, settings)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    events = payload if isinstance(payload, list) else [payload]
    accepted = 0
    skipped_import = 0
    for event in events:
        if not isinstance(event, dict) or _is_import_event(event):
            skipped_import += int(isinstance(event, dict))
            continue
        subscription = (event.get("subscriptionType") or "").lower()
        object_type = (event.get("objectTypeId") or "").lower()
        matches_route = (
            route == "all"
            or (route == "company" and ("company" in subscription or object_type == "0-2"))
            or (route == "contact" and ("contact" in subscription or object_type == "0-1"))
        )
        if matches_route and event.get("objectId") is not None:
            enqueue(app.state.background_tasks, _process_event, event)
            accepted += 1
    return {
        "accepted": accepted,
        "skipped_import": skipped_import,
        "queue_depth": len(app.state.background_tasks),
    }


# @app.post("/webhook", tags=["webhooks"])
# async def webhook(request: Request):
#     return await _queued_webhook(request, "all")


# @app.post("/webhook/company", tags=["webhooks"])
# async def company_webhook(request: Request):
#     return await _queued_webhook(request, "company")


# @app.post("/webhook/contact", tags=["webhooks"])
# async def contact_webhook(request: Request):
#     return await _queued_webhook(request, "contact")


# @app.post("/enrich/company", tags=["enrichment"])
# async def manual_company(payload: dict[str, Any], request: Request):
#     verify_app_secret(request, settings)
#     company_id = payload.get("company_id")
#     if not company_id:
#         raise HTTPException(status_code=422, detail="company_id is required")
#     return (await app.state.enrichment.enrich_company(str(company_id))).model_dump()


# @app.post("/enrich/contact", tags=["enrichment"])
# async def manual_contact(payload: dict[str, Any], request: Request):
#     verify_app_secret(request, settings)
#     contact_id = payload.get("contact_id")
#     if not contact_id:
#         raise HTTPException(status_code=422, detail="contact_id is required")
#     await app.state.enrichment.enrich_contact(str(contact_id))
#     return {"contact_id": str(contact_id), "status": "enriched"}
