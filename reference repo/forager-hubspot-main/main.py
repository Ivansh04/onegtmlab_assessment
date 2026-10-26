"""
main.py
-------
Flask webhook + manual-trigger server for the Forager x HubSpot enrichment automation.

Webhook events are authenticated, ACKed immediately, and processed on a background
worker — so a slow enrichment (the LLM logo web-search can take tens of seconds) can't
trip HubSpot's delivery timeout and trigger retries / double Forager-credit spend. The
manual /enrich/* endpoints run synchronously and return the result to the caller.

Sensitive endpoints (webhooks, /enrich/*, /debug/*) require APP_SECRET when it is set
(send it as header `X-App-Secret` or query `?token=`). `/` and `/health` stay public.

Endpoints:
  GET  /                       Service info / build
  GET  /health                 Health check + queue depth
  GET  /debug/config           Which integrations are configured (no secrets)   [auth]
  GET/POST /debug/alert-test    Send a test error email                          [auth]
  POST /webhook                 HubSpot single Target URL (routes all events)    [auth]
  POST /webhook/company         Company-created handler                          [auth]
  POST /webhook/contact         Contact-created handler                          [auth]
  POST /enrich/company          Manual {"company_id"} (forces a refresh)         [auth]
  POST /enrich/contact          Manual {"contact_id"}                            [auth]
  POST /enrich/find-contacts    Manual {"company_id","company_domain",...}       [auth]
  POST /enrich/demo             Full company pipeline {"company_id"}             [auth]
"""

import logging
import os
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

load_dotenv()

import alerts       # noqa: E402  (import after load_dotenv so env vars are set)
import auth         # noqa: E402
import background   # noqa: E402
import deepline     # noqa: E402
import enrichment   # noqa: E402
import forager      # noqa: E402
import hubspot      # noqa: E402
import scoring      # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

BUILD = "v3.55 (CONTACT OWNER = COMPANY OWNER + 'Contact Source' TAG): (1) auto-discovered contacts now inherit the COMPANY's owner exactly (hubspot.get_company_owner_id) instead of a fixed configured Contact Owner -- if the company owner is 'One GTM Labs' the contact owner is 'One GTM Labs'; if the company has NO owner the contact is left unassigned too. One-time copy at creation, not an ongoing sync. force/manual paths unchanged. (2) New contact property 'Contact Source' (contact_source), stamped 'TAM Automation' ONLY on contacts our discovery creates -- so an automation-dumped contact is distinguishable from a manually-added one (which leaves it blank; e.g. a contact Ahmed adds by hand keeps his chosen owner and a blank Contact Source). Property auto-created via ensure_custom_properties. v3.54 (DOMAIN-DEDUP GUARD, default ON, kill-switch COMPANY_DEDUP_GUARD=off): before a company webhook enriches + discovers contacts, it checks whether ANOTHER company with the SAME domain is already enriched (has forager_org_id) via a free HubSpot search. If so, this record is a DUPLICATE (e.g. an external Airtable/CRM sync created a second copy of a company we already have) and the whole run is SKIPPED before spending any credits. Root cause it fixes: on Jul 4 an Airtable integration created ~44 company records (many duplicates of existing companies); each looked 'new' (blank forager_org_id, no contacts) so our automation re-enriched + re-discovered them, creating 97 contacts and burning ~300 Deepline + ~2400 Forager credits. With this guard the duplicates are recognised (their domain is already enriched under another record) and skipped -> 0 credits. Genuinely new companies are unaffected; manual force=True refreshes bypass the guard on purpose. New hubspot.find_enriched_duplicate_company(domain, exclude_id). v3.53 (DEEPLINE CREDIT REUSE, env-gated DEEPLINE_SEQUENTIAL_REUSE, default OFF): optional sequential email->phone enrichment with a per-contact tool-response cache so a tool called in BOTH waterfalls with the SAME payload is charged ONCE. Headline case: PDL (peopledatalabs_enrich_contact) — one flat-priced enrich (~3.92 credits) already returns work_email AND mobile_phone, but today we call it twice (once per waterfall); with reuse on, the phone waterfall reuses PDL's cached email-time response when its region-ordered sequence reaches PDL (position-aware; waterfall order + validation unchanged). Field-gated tools are untouched by design: ContactOut (include=[work_email] vs [phone]), Prospeo (enrich_mobile flag), Lusha (reveal_phones flag) send DIFFERENT payloads per waterfall so they still make their own calls — we never eagerly pay to reveal a phone we might not need. Correctness guards: cache is per-contact (reset each run), keyed by exact (tool_id,payload), thread-local (concurrent contacts never share), and BYPASSED for async poll reads (_poll cacheable=False) so a running job is never frozen. Default OFF => unchanged parallel behaviour; deploy is a no-op until DEEPLINE_SEQUENTIAL_REUSE=on. Investigation finding: ContactOut's check_work_email/check_phone are VALIDATORS not finders, and Prospeo/Lusha/PDL have no lean single-field endpoint, so the 'cheaper endpoint' idea has no takers among these four — the saving is the reuse of PDL. v3.52 (IMPORT SAFEGUARD): a bulk HubSpot import no longer auto-triggers enrichment / burns credits. Every HubSpot creation event carries a changeSource; a CSV/spreadsheet import stamps it 'IMPORT'. All three webhook routes (/webhook, /webhook/company, /webhook/contact) now ACK import-sourced events 200 but do NOT enqueue them (_is_import_event), so importing thousands of rows costs 0 Forager/Deepline credits. Real user-created records (changeSource CRM_UI/FORM/API/INTEGRATION/...) are unaffected and still enrich normally; a missing changeSource is treated as NOT an import so genuine events are never dropped. Responses now include skipped_import count; skips are logged. Tunable: SKIP_IMPORT_WEBHOOKS (default true), IMPORT_CHANGE_SOURCES (default 'IMPORT'; widen with MIGRATION,BATCH_UPDATE,...). Pre-existing records remain the job of the separate bulk backfill tool, not the live webhook. v3.51 (D) SIZE-BASED PEOPLE DISCOVERY (Shirish/Ahmed 'finding people' logic): the buyer-committee discovery now scales BOTH how many people it targets and WHICH titles, by company headcount. COUNT by band (env-tunable DISCOVERY_COUNT_*): <=50 emp -> 3, <=200 -> 5, <=1000 -> 7, >1000 -> 10 (unknown headcount -> 5). TITLES by size (buyer_committee.titles_for_company): small cos (<=50) are CXO-led so the C-suite is pushed to the FRONT; large cos (>1000) SKIP the (unreachable) C-suite and lead with the VP/Head/Director layer; mid cos keep the full committee's natural Decision-Maker->Champion->Influencer priority order. MAX 1 CONTACT PER TITLE: discover_and_create_contacts now stops scanning a title after the first successful create (duplicates/already-seen people don't count, so it keeps looking within the title for a fresh person). handle_company_webhook computes the size-based target from company employees and passes it as a hard-capped effective_max (never exceeds the caller's max_contacts). Env knobs: DISCOVERY_SMALL_MAX(50)/DISCOVERY_MID_MAX(200)/DISCOVERY_LARGE_MAX(1000)/DISCOVERY_COUNT_SMALL(3)/MID(5)/LARGE(7)/XLARGE(10)/DEFAULT(5). v3.50 (A) PHONE PROVIDER = 'Forager' when Forager (Workflow 2) supplies the phone (Deepline's phone step is skipped when a phone already exists, so it would otherwise leave phone_enrichment_provider blank). (B) WORK-EMAIL NAME+DOMAIN CHECK via Claude: a Deepline email first hits the fast string domain guard; on a MATCH it skips Claude and goes straight to ZeroBounce; on a MISMATCH it escalates to Claude (client's exact Name+Domain prompt: scoring.verify_work_email) which rescues M&A rebrands/parents/subsidiaries (meta.com<->Facebook) and rejects WRONG_COMPANY/NON_WORK_EMAIL/NAME_MISMATCH/WRONG_COMPANY_AND_NAME/UNSURE (accept ONLY VALID). Verdict stored in new custom contact prop email_verification ('Email Verification') — VALID line for a saved email, or the worst rejection reason when blank. Falls back to strict string guard if Claude is unavailable. v3.49 (EMAIL ENRICHMENT PROVIDER: alongside a Deepline WORK email we now write which provider found it (hunter -> 'Hunter', pdl -> 'PDL', ...) into new custom prop email_enrichment_provider ('Email Enrichment Provider'). Only set on a Deepline work-email resolve (never for Forager). v3.48 (PHONE METADATA to HubSpot: alongside the phone number we now write the enrichment PROVIDER (winner key -> 'PDL'/'Upcell'/'Wiza'/... via deepline.provider_display), phone NAME MATCH (true/unknown/false from Trestle, NAMER-only), phone VALIDITY (Trestle phone.is_valid -> valid/invalid, NAMER-only), and finally POPULATE phone_country + phone_calling_code (previously blank) by parsing the resolved E.164 number with phonenumbers + pycountry (e.g. +919581999660 -> 'India','+91'). 3 new custom contact props auto-created: phone_enrichment_provider, phone_name_match, phone_validity (name_match/validity blank for non-NAMER where Trestle doesn't run). validate_phone now also captures phone.is_valid. New deps: phonenumbers, pycountry. v3.47 (FUNDING FIELDS REMAPPED to the client's 4 HubSpot fields: Funding (text) <- most recent funding DATE; Funding Amount (USD) <- most recent ROUND amount (last_funding_round_investment_usd); Total Funding <- TOTAL raised (crunchbase_total_investment_usd); Most Recent Funding Type <- most recent round type. get_company_funding now returns granular {total,last_amount,last_date,last_round_type}. The two Pipedrive-imported fields (Total Funding / Most Recent Funding Type) are resolved by LABEL via hubspot.company_property_name_by_label (overridable with FUNDING_TOTAL_FIELD / FUNDING_TYPE_FIELD). Tier-1 size check now uses TOTAL funding (unchanged intent). v3.46 (DUPLICATE-EMAIL 400 FIX: when the same person is discovered under two titles in one run, the second work-email write hits HubSpot's unique-email rule and HubSpot returns it as a 400 (not a 409) — our handler only caught 409, so the whole contact update aborted and NOTHING saved (Guillermo Rauch on the Vercel run: email was found, hunter=guillermo.rauch@vercel.com [valid], but the 400 wiped the save). hubspot._write_with_retry now also detects a duplicate-email 400 (_is_duplicate_email_error matches 'already exists'/'already has the value'/duplicate+email in the body) and retries WITHOUT `email`, same recovery as the existing 409 path, so the rest of the contact still lands. v3.45 (FUNDING NOW WORKS: switched company funding to Crustdata's company-DB screener crustdata_companydb_search (the Crustdata endpoint that actually carries funding) — reads crunchbase_total_investment_usd/last_funding_round_type/last_funding_date, picks the real company among same-domain namesakes by headcount, writes a clean summary (e.g. 'Total raised: $863M | Last round: Series F | 2025-09-30') + numeric amount for the Tier-1 rule. +/debug/funding?domain= probes a funding source (default Crustdata company-DB screener crustdata_companydb_search, which actually carries funding unlike the enrich tool) and returns raw JSON, to wire funding extraction. Reverted logo-step web-search cap back to 5 (speed handled via the faster ANTHROPIC_MODEL, e.g. claude-sonnet-4-6). Phone extractor now also covers camelCase/cell mobile keys (mobilePhone/mobileNumber/cell/cellphone/phonenumber) so we are prepared for Upcell-style output whose populated shape we have not directly observed. Email extraction now PREFERS the target-company email when a provider returns several — ContactOut returned [jsconf.eu, vercel.com] and we were grabbing jsconf (first) and rejecting it, throwing away the usable vercel.com address; now we pick the domain-matching one. +?raw=1 on the test endpoints surfaces each provider's full JSON, to verify extraction picks the RIGHT value (e.g. the target-domain email when a tool returns several). +/debug/email-tools + /debug/phone-tools: run EACH tool's real adapter through real extraction + validation, no first-success stop, no HubSpot write, ?tools= filter to test only specific tools. Upcell payload fixed to FLAT camelCase top-level fields (the `contact` nesting was wrong; live 422 confirmed it wants linkedinUrl/firstName/lastName/companyDomain/email flat). +/debug/phone-waterfall: runs the full region-aware phone waterfall + Trestle for a person without writing to HubSpot or skipping when a phone exists — lets us confirm the phone tools even when Forager always supplies a phone. Fixed the phone-provider adapters that returned nothing: Upcell now nests identity in a `contact` object (was flat -> empty); Wiza requests enrichment_level=full to actually return phones (DEEPLINE_WIZA_LEVEL); phone extractor now prefers INTERNATIONAL/E.164 format so a bare national number (Datagma's 1718659901) keeps its +country code (+491718659901); +Wiza phone_number1/mobile_phone1 keys. Findymail/Datagma confirmed working (needed the LinkedIn URL). + full per-provider logging + extractor hardening + domain guard + seeded 422 + Trestle/ZeroBounce verdict fixes. Tier 1/2; funding via Crustdata; region-aware phone waterfalls. Deepline dormant unless DEEPLINE_API_KEY)"

_REQUIRED_ENV = ("FORAGER_API_KEY", "FORAGER_ACCOUNT_ID", "HUBSPOT_TOKEN")


def _missing_required_env() -> list:
    return [name for name in _REQUIRED_ENV if not os.environ.get(name)]


# Fail LOUD at startup if the must-have credentials are absent (instead of a silent
# 500 on the first request). The service still boots so /health and /debug/config work.
_missing = _missing_required_env()
if _missing:
    logger.critical("MISSING REQUIRED ENV VARS: %s — enrichment will fail until these are set.",
                    ", ".join(_missing))
if not auth.is_enabled():
    logger.warning("APP_SECRET is not set — sensitive endpoints are OPEN. Set it before going live.")

app = Flask(__name__)

# Create custom HubSpot properties up front, and start the background worker that
# drains the webhook queue.
try:
    hubspot.ensure_custom_properties()
except Exception as exc:  # noqa: BLE001
    logger.warning("ensure_custom_properties() at startup failed: %s", exc)
background.start_worker()


def require_secret(fn):
    """Gate a sensitive endpoint behind the shared secret (enforced only when
    APP_SECRET is configured — see auth.py)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not auth.authorize(request):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


def _int_arg(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# IMPORT SAFEGUARD — stop a bulk HubSpot import from auto-triggering enrichment
# (and silently burning Forager/Deepline credits). Every HubSpot creation event
# carries a `changeSource`; a CSV/spreadsheet import stamps it "IMPORT". When
# SKIP_IMPORT_WEBHOOKS is on (default), we ACK those events 200 but do NOT enqueue
# them — so importing thousands of rows costs 0 credits. Real user-created records
# (changeSource CRM_UI / FORM / API / INTEGRATION / ...) are unaffected and still
# enrich normally. Pre-existing records are meant to be backfilled by the separate
# bulk tool, not by the live webhook. Tunable: SKIP_IMPORT_WEBHOOKS (default true),
# IMPORT_CHANGE_SOURCES (default "IMPORT"; add MIGRATION,BATCH_UPDATE,... to widen).
# ---------------------------------------------------------------------------
_SKIP_IMPORT_WEBHOOKS = _bool_env("SKIP_IMPORT_WEBHOOKS", True)
_IMPORT_CHANGE_SOURCES = {
    s.strip().upper()
    for s in os.environ.get("IMPORT_CHANGE_SOURCES", "IMPORT").split(",")
    if s.strip()
}


def _is_import_event(event: dict) -> bool:
    """True if this webhook event was generated by a bulk import we should not enrich.

    Keyed off HubSpot's `changeSource` (present on every creation/change event). A
    missing changeSource is treated as NOT an import, so genuine records are never
    silently dropped. No-op when SKIP_IMPORT_WEBHOOKS is disabled."""
    if not _SKIP_IMPORT_WEBHOOKS:
        return False
    source = (event.get("changeSource") or "").strip().upper()
    return bool(source) and source in _IMPORT_CHANGE_SOURCES


@app.errorhandler(Exception)
def handle_uncaught(error):
    """Return clean JSON instead of a 500 HTML page for unhandled errors, and surface
    upstream issues clearly — notably Forager returning HTTP 402 (out of credits)."""
    if isinstance(error, HTTPException):
        return error  # let Flask render 404/405/etc. normally
    logger.exception("Unhandled error while handling request")
    response = getattr(error, "response", None)
    upstream_status = getattr(response, "status_code", None)
    summary = ("Forager out of credits" if upstream_status == 402
               else f"Unhandled error on {request.method} {request.path}")
    alerts.send_error_alert(summary, error=error,
                            context={"method": request.method, "path": request.path,
                                     "upstream_status": upstream_status})
    if upstream_status == 402:
        return jsonify({
            "error": "forager_out_of_credits",
            "message": "Forager reports insufficient credits. Add credits in the Forager dashboard, then retry.",
        }), 402
    if upstream_status is not None:
        return jsonify({"error": "upstream_error", "upstream_status": upstream_status,
                        "message": str(error)}), 502
    return jsonify({"error": "internal_error", "message": str(error)}), 500


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Forager x HubSpot Enrichment Automation",
        "status": "running",
        "build": BUILD,
        "auth_enabled": auth.is_enabled(),
        "endpoints": [
            "GET  /health",
            "GET  /debug/config          [auth]",
            "GET/POST /debug/alert-test  [auth]",
            "POST /webhook               [auth]  (HubSpot single Target URL)",
            "POST /webhook/company       [auth]",
            "POST /webhook/contact       [auth]",
            "POST /enrich/company        [auth]",
            "POST /enrich/contact        [auth]",
            "POST /enrich/find-contacts  [auth]",
            "POST /enrich/demo           [auth]",
        ],
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "queue_depth": background.queue_size()}), 200


@app.route("/debug/config", methods=["GET"])
@require_secret
def debug_config():
    """Readiness check — what's configured, without secrets and without spending any
    Forager credits. Useful to confirm you're ready before creating a company."""
    return jsonify({
        "build": BUILD,
        "required_env_ok": not _missing_required_env(),
        "missing_required_env": _missing_required_env(),
        "auth_enabled": auth.is_enabled(),
        "scoring_provider": scoring.provider(),   # "gemini" | "anthropic" | null
        "deepline_enabled": deepline.is_enabled(),
        "alerts_configured": alerts.is_configured(),
        "queue_depth": background.queue_size(),
    }), 200


@app.route("/debug/alert-test", methods=["GET", "POST"])
@require_secret
def alert_test():
    """Send a harmless test alert; returns the ACTUAL SMTP error + non-secret config
    when the send fails, so 'email_sent: false' is self-diagnosing."""
    return jsonify(alerts.test_send()), 200


@app.route("/debug/score-test", methods=["GET", "POST"])
@require_secret
def score_test():
    """Fast Claude key/health check: runs ONLY the ICP prompt (no web search), so it
    returns in a few seconds. A 401 here means a bad key; real JSON means the key
    works. The logo step is skipped on purpose — its live web search is too slow for
    a synchronous request, but runs fine on the background worker during real
    enrichment. No Forager credits (LLM account only)."""
    sample = {
        "name": "Stripe", "domain": "stripe.com",
        "description": "Payments infrastructure and financial APIs for internet businesses.",
        "industry": "Financial Services", "numberofemployees": 8000,
    }
    return jsonify(scoring.icp_only(sample)), 200


@app.route("/debug/owners", methods=["GET"])
@require_secret
def debug_owners():
    """List HubSpot owners (id, email, name) and show what CONTACT_OWNER resolves to —
    use it to find/verify the value for the auto-created Contact Owner."""
    return jsonify({
        "configured_contact_owner": hubspot.CONTACT_OWNER,
        "resolved_owner_id": hubspot.auto_create_owner_id(),
        "owners": hubspot.list_owners(),
    }), 200


@app.route("/debug/person-test", methods=["GET", "POST"])
@require_secret
def person_test():
    """Diagnostic: does Forager return a person's full profile from a LinkedIn handle
    ALONE (no company)? Pass ?slug=<linkedin-handle>. It's a search, not a reveal, so
    ~0 Forager credits. 'found': true with a title/company means the LinkedIn-only
    full-profile path works."""
    slug = request.args.get("slug") or "ankurbansal177"
    return jsonify(forager.probe_person_by_linkedin(slug)), 200


@app.route("/debug/company-test", methods=["GET", "POST"])
@require_secret
def company_test():
    """Diagnostic: for ?company=&first=&last=, report whether the company name resolves
    to a domain and whether the person is findable by name. A search, ~0 credits."""
    company = request.args.get("company") or "Apollo.io"
    first = request.args.get("first") or "Ankur"
    last = request.args.get("last") or "Bansal"
    return jsonify(forager.probe_company_person(company, first, last)), 200


# ---------------------------------------------------------------------------
# Webhooks: authenticate, enqueue, ACK 200 immediately. The slow pipeline runs
# on the background worker (background.py) so HubSpot never waits / retries.
# ---------------------------------------------------------------------------
def _process_company_event(object_id: str, job_title_filter, max_contacts: int) -> None:
    try:
        enrichment.handle_company_webhook(object_id, job_title_filter, max_contacts)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Background company enrichment failed for %s", object_id)
        alerts.send_error_alert(f"Company enrichment failed: {object_id}", error=exc,
                                context={"objectId": object_id, "type": "company"})


def _process_contact_event(object_id: str) -> None:
    try:
        enrichment.handle_contact_webhook(object_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Background contact enrichment failed for %s", object_id)
        alerts.send_error_alert(f"Contact enrichment failed: {object_id}", error=exc,
                                context={"objectId": object_id, "type": "contact"})


@app.route("/webhook", methods=["POST"])
@require_secret
def webhook():
    """Unified HubSpot webhook Target URL — routes company + contact events by their
    subscriptionType. Authenticated, enqueued, ACKed immediately."""
    payload = request.get_json(force=True, silent=True) or []
    logger.info("/webhook received %d event(s)", len(payload))
    job_title_filter = request.args.get("job_title_filter")
    max_contacts = _int_arg(request.args.get("max_contacts"), 10)
    accepted = 0
    skipped_import = 0
    for event in payload:
        object_id = str(event.get("objectId", ""))
        subscription = (event.get("subscriptionType") or "").lower()
        if not object_id:
            continue
        # Import safeguard: never enrich records created by a bulk import (0 credits).
        if _is_import_event(event):
            skipped_import += 1
            continue
        if subscription.startswith("company"):
            background.enqueue(_process_company_event, object_id, job_title_filter, max_contacts)
            accepted += 1
        elif subscription.startswith("contact"):
            background.enqueue(_process_contact_event, object_id)
            accepted += 1
        else:
            logger.info("/webhook ignoring event with subscriptionType=%r", subscription)
    if skipped_import:
        logger.info("/webhook skipped %d import-sourced event(s) (changeSource in %s)",
                    skipped_import, sorted(_IMPORT_CHANGE_SOURCES))
    return jsonify({"accepted": accepted, "skipped_import": skipped_import,
                    "queue_depth": background.queue_size()}), 200


@app.route("/webhook/company", methods=["POST"])
@require_secret
def webhook_company():
    payload = request.get_json(force=True, silent=True) or []
    logger.info("/webhook/company received %d event(s)", len(payload))
    job_title_filter = request.args.get("job_title_filter")
    max_contacts = _int_arg(request.args.get("max_contacts"), 10)
    accepted = 0
    skipped_import = 0
    for event in payload:
        company_id = str(event.get("objectId", ""))
        if not company_id:
            continue
        if _is_import_event(event):  # import safeguard — 0 credits for bulk imports
            skipped_import += 1
            continue
        background.enqueue(_process_company_event, company_id, job_title_filter, max_contacts)
        accepted += 1
    if skipped_import:
        logger.info("/webhook/company skipped %d import-sourced event(s)", skipped_import)
    return jsonify({"accepted": accepted, "skipped_import": skipped_import,
                    "queue_depth": background.queue_size()}), 200


@app.route("/webhook/contact", methods=["POST"])
@require_secret
def webhook_contact():
    payload = request.get_json(force=True, silent=True) or []
    logger.info("/webhook/contact received %d event(s)", len(payload))
    accepted = 0
    skipped_import = 0
    for event in payload:
        contact_id = str(event.get("objectId", ""))
        if not contact_id:
            continue
        if _is_import_event(event):  # import safeguard — 0 credits for bulk imports
            skipped_import += 1
            continue
        background.enqueue(_process_contact_event, contact_id)
        accepted += 1
    if skipped_import:
        logger.info("/webhook/contact skipped %d import-sourced event(s)", skipped_import)
    return jsonify({"accepted": accepted, "skipped_import": skipped_import,
                    "queue_depth": background.queue_size()}), 200


# ---------------------------------------------------------------------------
# Manual triggers: synchronous — the caller wants the result inline.
# ---------------------------------------------------------------------------
@app.route("/enrich/company", methods=["POST"])
@require_secret
def manual_enrich_company():
    body = request.get_json(force=True, silent=True) or {}
    company_id = body.get("company_id")
    if not company_id:
        return jsonify({"error": "company_id required"}), 400
    # Manual trigger forces a refresh even if already enriched.
    return jsonify(enrichment.enrich_company(str(company_id), force=True)), 200


@app.route("/enrich/contact", methods=["POST"])
@require_secret
def manual_enrich_contact():
    body = request.get_json(force=True, silent=True) or {}
    contact_id = body.get("contact_id")
    if not contact_id:
        return jsonify({"error": "contact_id required"}), 400
    return jsonify(enrichment.enrich_contact(str(contact_id))), 200


@app.route("/enrich/deepline", methods=["POST"])
@require_secret
def manual_deepline():
    """Manually run Workflow 3 (Deepline) on one contact — for controlled testing
    once the Deepline key + credits are in place. {"contact_id": "123"}."""
    body = request.get_json(force=True, silent=True) or {}
    contact_id = body.get("contact_id")
    if not contact_id:
        return jsonify({"error": "contact_id required"}), 400
    return jsonify(enrichment.workflow3_deepline(str(contact_id))), 200


@app.route("/debug/deepline", methods=["GET", "POST"])
@require_secret
def debug_deepline():
    """Dormant-safe Deepline check: confirms the key is set and (optionally) runs one
    tool. Pass ?tool=zerobounce_validate&email=test@stripe.com to probe a single tool.
    Spends provider credits only if you pass a tool to run."""
    out = {"deepline_enabled": deepline.is_enabled()}
    tool = request.args.get("tool")
    if tool and deepline.is_enabled():
        payload = {k: v for k, v in request.args.items() if k != "tool" and k != "token"}
        out["tool"] = tool
        out["result"] = deepline.execute_tool(tool, payload)
    return jsonify(out), 200


@app.route("/debug/phone-waterfall", methods=["GET", "POST"])
@require_secret
def debug_phone_waterfall():
    """Run the FULL region-aware phone waterfall for a person — every provider in order
    + Trestle name-match — WITHOUT writing to HubSpot and WITHOUT the 'already has a
    phone' skip. Lets us confirm the phone tools end-to-end even when Forager always
    supplies a phone. The full per-provider trail prints to the logs.
    Example: ?first_name=Malte&last_name=Ubl&domain=vercel.com&country=United States
             &linkedin_url=https://www.linkedin.com/in/malteubl&email=malte@vercel.com
    Spends provider credits (it actually runs the waterfall)."""
    inp = _person_inp({**(request.get_json(silent=True) or {}), **request.args.to_dict()})
    result = deepline.run_phone_waterfall(inp) if deepline.is_enabled() else {}
    return jsonify({"deepline_enabled": deepline.is_enabled(), "input": inp, "result": result}), 200


def _person_inp(a: dict) -> dict:
    """Build the enrichment input dict from request args (shared by the debug endpoints)."""
    first, last = a.get("first_name", ""), a.get("last_name", "")
    return {
        "first_name": first, "last_name": last,
        "full_name": a.get("full_name") or f"{first} {last}".strip(),
        "domain": forager.normalize_domain(a.get("domain", "")),
        "company_name": a.get("company_name", ""),
        "linkedin_url": a.get("linkedin_url", ""),
        "email": a.get("email", ""),
        "country": a.get("country", ""),
    }


def _only_tools(a: dict):
    """Optional ?tools=a,b,c filter — limits which tools run (to save credits)."""
    raw = (a.get("tools") or "").strip()
    return [t.strip() for t in raw.split(",") if t.strip()] or None


@app.route("/debug/email-tools", methods=["GET", "POST"])
@require_secret
def debug_email_tools():
    """Run EACH email tool's real adapter (with its correct reveal flags) through the
    real extraction + ZeroBounce + domain check — without stopping at the first success
    and without writing to HubSpot. Confirms every tool individually. Use ?tools=a,b to
    test only specific tools (saves credits). Spends provider credits.
    Example: ?first_name=Malte&last_name=Ubl&domain=vercel.com
             &linkedin_url=https://www.linkedin.com/in/malteubl&tools=contactout,crustdata"""
    a = {**(request.get_json(silent=True) or {}), **request.args.to_dict()}
    inp = _person_inp(a)
    raw = str(a.get("raw", "")).lower() in ("1", "true", "yes", "on")
    return jsonify({"deepline_enabled": deepline.is_enabled(), "input": inp,
                    "tools": deepline.test_email_tools(inp, _only_tools(a), include_raw=raw)}), 200


@app.route("/debug/phone-tools", methods=["GET", "POST"])
@require_secret
def debug_phone_tools():
    """Run EACH phone tool's real adapter (with its correct reveal flags) through real
    extraction + Trestle (region-based) — without stopping at the first success and
    without writing to HubSpot. Use ?tools=a,b to test only specific tools (saves
    credits). Spends provider credits.
    Example: ?first_name=Malte&last_name=Ubl&domain=vercel.com&country=United States
             &linkedin_url=https://www.linkedin.com/in/malteubl&email=malte@vercel.com
             &tools=wiza,prospeo,contactout,upcell,findymail"""
    a = {**(request.get_json(silent=True) or {}), **request.args.to_dict()}
    inp = _person_inp(a)
    raw = str(a.get("raw", "")).lower() in ("1", "true", "yes", "on")
    return jsonify({"deepline_enabled": deepline.is_enabled(), "input": inp,
                    "phone": deepline.test_phone_tools(inp, _only_tools(a), include_raw=raw)}), 200


@app.route("/debug/funding", methods=["GET", "POST"])
@require_secret
def debug_funding():
    """Probe a company-funding source by domain and return the RAW response (no HubSpot
    write) so we can see which funding fields it carries. Defaults to Crustdata's
    company-DB screener (the Crustdata endpoint that actually holds funding), which
    needs a filter payload a plain /debug/deepline call can't send. Override the source
    with ?tool= (e.g. contactout_enrich_person, lusha_enrich_company).
    Example: ?domain=vercel.com . Spends ~0.4 provider credits."""
    a = {**(request.get_json(silent=True) or {}), **request.args.to_dict()}
    domain = forager.normalize_domain(a.get("domain", ""))
    name = (a.get("name") or "").strip()
    tool = a.get("tool") or "crustdata_companydb_search"
    if "companydb_search" in tool or "company_db_search" in tool:
        if domain:
            payload = {"filters": [{"filter_type": "company_website_domain", "type": "=", "value": domain}], "limit": 3}
        elif name:
            payload = {"filters": [{"filter_type": "company_name", "type": "(.)", "value": name}], "limit": 3}
        else:
            return jsonify({"error": "pass ?domain= or ?name="}), 400
    else:
        payload = {k: v for k, v in {"domain": domain, "companyDomain": domain,
                                     "company_domain": domain, "company_name": name}.items() if v}
    result = deepline.execute_tool(tool, payload) if deepline.is_enabled() else None
    return jsonify({"deepline_enabled": deepline.is_enabled(), "tool": tool,
                    "payload": payload, "result": result}), 200


@app.route("/enrich/find-contacts", methods=["POST"])
@require_secret
def manual_find_contacts():
    body = request.get_json(force=True, silent=True) or {}
    company_id = body.get("company_id")
    company_domain = body.get("company_domain")
    job_title = body.get("job_title_filter")
    max_contacts = _int_arg(body.get("max_contacts"), 10)
    if not company_id or not company_domain:
        return jsonify({"error": "company_id and company_domain required"}), 400
    results = enrichment.discover_and_create_contacts(
        hubspot_company_id=str(company_id),
        company_domain=company_domain,
        job_title_filter=job_title,
        max_contacts=max_contacts,
    )
    return jsonify(results), 200


@app.route("/enrich/demo", methods=["POST"])
@require_secret
def demo():
    body = request.get_json(force=True, silent=True) or {}
    company_id = body.get("company_id")
    if not company_id:
        return jsonify({"error": "company_id required"}), 400
    job_title_filter = request.args.get("job_title_filter")
    max_contacts = _int_arg(request.args.get("max_contacts", body.get("max_contacts")), 10)
    # Demo runs the full pipeline synchronously and forces a refresh so you always see results.
    return jsonify(enrichment.handle_company_webhook(
        str(company_id), job_title_filter, max_contacts, force=True)), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
