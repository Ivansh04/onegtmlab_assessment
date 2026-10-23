# Forager × HubSpot Enrichment

A Python/FastAPI service for enriching HubSpot company and contact records with Forager data, discovering people by job title, and processing HubSpot webhook events.

> Important: this repository is a starter implementation. Before using it against live CRM data, validate the exact Forager response shapes against your account and configure HubSpot webhook subscriptions/signature verification. It intentionally does not fabricate missing contact information.

## Capabilities

- Enrich an existing HubSpot company and discover/create matching contacts when fewer than the configured contact limit are associated.
- Enrich an existing HubSpot contact.
- Discover people using configurable job-title terms.
- Receive company/contact webhook events.
- Keep secrets in environment variables.
- Structured logs that avoid logging CRM payloads and contact data.
- Small unit-test suite and Railway deployment configuration.

The contact LinkedIn field used by the service is HubSpot's `hs_linkedin_url`
property. Confirm that this standard property is available in the portal before
the demo. Company writes use standard HubSpot fields only; this avoids portal
specific dropdown and custom-property validation failures.

## Requirements

- Python 3.11+
- Forager API key and account ID
- HubSpot private app token with company/contact read and write scopes
- For public-app webhooks, HubSpot app secret and webhook subscriptions

## Run locally

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and add credentials. Do not share or commit `.env`.

```powershell
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for interactive API docs and `http://127.0.0.1:8000/health` for the health check.

### Linux/macOS

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

## API endpoints

- `GET /health`
- `POST /api/enrich/company/{hubspot_company_id}`
- `POST /api/enrich/contact/{hubspot_contact_id}`
- `POST /api/find-contacts`

Example contact search body:

```json
{
  "company_name": "OpenAI",
  "company_domain": "openai.com",
  "job_titles": ["CEO", "CTO", "VP Engineering"],
  "max_contacts": 5
}
```

- `POST /webhooks/hubspot`

## HubSpot setup notes

1. Create a private app (or public app if using app-level webhooks).
2. Grant only the required CRM scopes: company read/write and contact read/write. Exact scope labels can change in HubSpot's UI.
3. For app-level webhook subscriptions, subscribe to company creation and contact creation events and set the callback URL to `https://YOUR-DOMAIN/webhooks/hubspot`.
4. Set `HUBSPOT_CLIENT_SECRET` and keep signature validation enabled for production.
5. Test signatures with actual HubSpot deliveries before going live.

A private app token alone does not automatically configure app-level webhook subscriptions. If your HubSpot setup uses workflow webhook actions instead, configure the action's authentication and adapt the verification scheme accordingly.

For the primary company demo, call `POST /api/enrich/company/{hubspot_company_id}`.
The service enriches existing associated contacts and searches Forager with
each title in `ALLOWED_JOB_TITLES` until it has created up to
`MAX_CONTACTS_PER_COMPANY` matching contacts. Only values returned by Forager
are written to HubSpot.

## Forager endpoints used

The client uses documented Forager v2 endpoints under `/api/{account_id}/datastorage/`, including organization search, person role search, person details, work email lookup, and phone number lookup. Request and response fields can differ by endpoint or account access; confirm response payloads with your Forager API reference and test with one known record before processing the demo dataset.

## Production-readiness checklist before demo

- [ ] Verify response schemas for organization search and person detail against live Forager responses.
- [ ] Confirm the HubSpot `hs_linkedin_url` contact property exists in your portal; create a custom property if necessary.
- [ ] Test HubSpot association type IDs in your portal before creating contacts.
- [ ] Add durable queue/background processing and persistent idempotency storage before high-volume production use. This starter processes webhook events inline.
- [ ] Configure rate limits, retries/backoff, metrics, and alerting.
- [ ] Verify whether your HubSpot webhook delivery signature version and URL construction match the deployed ingress/proxy.
- [ ] Confirm Forager credits and privacy/compliance requirements before running enrichment.

## Run tests

```bash
pytest -q
```

## Deploy on Railway

1. Push this project to a GitHub repository.
2. Create a Railway project from the repository.
3. Add environment variables from `.env.example` in Railway's Variables panel.
4. Deploy and verify `/health`.
5. Configure the HubSpot webhook callback URL.
6. Trigger one test company/contact event and inspect Railway logs.

## Demo plan

1. Show `/health` and API docs.
2. Enrich the OpenAI test company.
3. Find contacts with title filters and show the results.
4. Trigger company/contact webhook events and show CRM fields changing.
5. Review missing data and any additional profile details surfaced by Forager.

Do not claim a record is fully enriched unless the data was returned by Forager and successfully written to HubSpot.
