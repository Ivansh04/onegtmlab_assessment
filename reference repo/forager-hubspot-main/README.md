# Forager × HubSpot Enrichment Automation

A small Flask service that enriches HubSpot CRM records using the
[Forager](https://www.forager.ai/) data API.

It can:

1. **Enrich a company** — look it up in Forager and write back description,
   revenue, headcount, LinkedIn, location, industry, etc.
2. **Enrich a contact** — use a contact's LinkedIn URL to find emails + phone
   numbers and write them back.
3. **Find contacts at a company** — discover current employees, keep only those
   whose **job title is on the TAM buyer-committee list**, create them in HubSpot,
   and associate them to the company.
4. **Score the company (Claude)** — alongside enrichment, compute **ICP fit**
   (is this a good Forager prospect?) and **logo recognizability** (brand
   notability, via web search) and write both score sets back to the company.
5. **Company webhook (Workflow 1)** — on "company created": enrich + score the
   company, then **discover** buyer-committee people and create them as skeleton
   contacts (name/title/LinkedIn/company). This spends **no reveal credits** — it
   does not look up emails/phones.
6. **Contact webhook (Workflow 2)** — on "contact created": reveal that contact's
   email + phone (this is where Forager reveal credits are spent). Each skeleton
   created in Workflow 1 fires this webhook automatically (the "chain reaction"),
   and a manually-added contact enters here directly. A free duplicate-person
   check skips any reveal for a person already enriched on another record.

## Architecture

| File | Responsibility |
|------|----------------|
| `forager.py` | All Forager API calls + response → field mapping |
| `hubspot.py` | All HubSpot CRM calls (+ custom-property bootstrap, resilient writes) |
| `buyer_committee.py` | TAM buyer-committee title list + the title matcher |
| `scoring.py` | LLM-powered ICP fit + logo recognizability scoring (Gemini/Claude) |
| `alerts.py` | Email error alerts over SMTP (off until configured) |
| `enrichment.py` | Business logic tying Forager, the LLM, and HubSpot together |
| `main.py` | Flask server (webhooks + manual triggers + error alerts) |

## Buyer-committee title filtering

When the pipeline auto-discovers people at a company, it only creates + enriches
contacts whose job title matches the **TAM buyer-committee list** (Decision Maker
/ Champion / Influencer — Product, Eng/Platform, Data/AI, Partnerships, Exec, …)
in `buyer_committee.py`. Everyone else is skipped **before** any credit-spending
Forager contact lookup. Matching is normalization-tolerant (abbreviations, comma
variants, seniority prefixes). The optional `job_title_filter` still works as an
extra narrowing on top of the committee list. `max_contacts` defaults to **50**.

> ⚠️ **Credit note:** discovery (Workflow 1) is search-only and ≈free. The reveal
> cost is paid in Workflow 2 — roughly **20–25 Forager credits per contact**, so a
> company filling all 50 slots can cost ~**1,000–1,250 credits**. The duplicate-
> person guard ensures the same person is never revealed (paid for) twice.

## Scoring (LLM — Gemini or Claude)

Two company-level scores are computed via an LLM and written to HubSpot custom
properties:

- **ICP fit** → `icp_match_score`, `icp_decision` (ICP/MAYBE/REMOVE),
  `icp_confidence`, `icp_reasoning`, `icp_positives`, `icp_negatives`,
  `icp_red_flags`, `icp_best_fit_use_case`, `icp_suggested_next_step`.
- **Logo recognizability** (uses web search) → `logo_score`, `logo_tier`
  (T1/T2/T3), `logo_confidence`, `logo_why`, `logo_forager_fit`,
  `logo_forager_fit_reason`, `logo_evidence`.

The provider is **pluggable** (`scoring.py`) so you can test on a free key now and
switch for the client with no code change:

- **Gemini** (`GEMINI_API_KEY`, free tier) — the logo step uses Google Search
  grounding. Recommended for testing.
- **Claude** (`ANTHROPIC_API_KEY`) — the logo step uses Claude's web search.

Selection is automatic from whichever key is present (Gemini wins if both are
set); force it with `SCORING_PROVIDER=gemini|anthropic`. If no key is configured,
enrichment still runs and the scores are simply skipped. LLM + web-search usage is
billed to the LLM account, **not** Forager credits.

## Error alerts (email)

When the pipeline hits a real failure — an unhandled error, a HubSpot/Forager API
error, or Forager out-of-credits — `alerts.py` emails the reason + traceback so the
team finds out without watching logs. It sends over **SMTP** (works with Gmail,
Brevo, SendGrid, Mailgun, …) and stays **OFF until configured**: set `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM`, and `ALERT_EMAIL_TO`
(comma-separated). The alerter never raises, and identical alerts are throttled
(`ALERT_THROTTLE_SECONDS`, default 300s) so one bad batch can't flood your inbox.

Verify your setup with **`GET/POST /debug/alert-test`** — it sends a harmless test
email and returns `{"alerts_configured": ..., "email_sent": ...}`. Soft outcomes
(e.g. "no Forager match") are returned in the response, not emailed.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health check |
| POST | `/webhook` | **HubSpot webhook Target URL** — routes company + contact events |
| POST | `/webhook/company` | Company-created handler (also callable directly) |
| POST | `/webhook/contact` | Contact-created handler (also callable directly) |
| POST | `/enrich/company` | Manual: `{"company_id": "123"}` |
| POST | `/enrich/contact` | Manual: `{"contact_id": "123"}` |
| POST | `/enrich/find-contacts` | Manual: `{"company_id","company_domain","job_title_filter","max_contacts"}` |
| POST | `/enrich/demo` | Full company pipeline: `{"company_id": "123"}` |

Buyer-committee filtering is always applied to auto-discovered contacts. The
optional `job_title_filter` narrows further: on `/webhook/company` and
`/enrich/demo` via a `?job_title_filter=engineer` query parameter, and on
`/enrich/find-contacts` via the `job_title_filter` body field. `/enrich/company`
and `/enrich/demo` responses include the computed `icp` and `logo` scores.

## Setup

```bash
python -m venv .venv
. .venv/Scripts/activate      # Windows;  use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env          # then fill in real values
python main.py                # http://localhost:5000
```

### Environment variables

```
FORAGER_API_KEY       Forager API key
FORAGER_ACCOUNT_ID    Forager account id (integer, part of every API path)
HUBSPOT_TOKEN         HubSpot Private App token

# Scoring LLM (optional — without a key, scoring is skipped):
GEMINI_API_KEY        Google AI Studio key — enables scoring on Gemini (free tier)
GEMINI_MODEL          optional; defaults to gemini-2.5-flash
ANTHROPIC_API_KEY     Claude API key — enables scoring on Claude
ANTHROPIC_MODEL       optional; defaults to claude-opus-4-8
SCORING_PROVIDER      optional; force "gemini" or "anthropic" (else auto-detected)

# Error email alerts (optional — without these, alerts are skipped):
SMTP_HOST             SMTP server, e.g. smtp.gmail.com
SMTP_PORT             587 (STARTTLS) or 465 (SSL); default 587
SMTP_USER             SMTP username
SMTP_PASSWORD         SMTP password / app password
ALERT_EMAIL_FROM      from address (defaults to SMTP_USER)
ALERT_EMAIL_TO        comma-separated recipients
```

The HubSpot Private App needs these scopes:
`crm.objects.companies.read/write`, `crm.objects.contacts.read/write`,
and `crm.schemas.companies.write` + `crm.schemas.contacts.write`
(so the app can create its custom properties).

## Implementation notes (Forager API specifics)

These were verified against the live API and differ from a naive reading of the
docs:

- Forager is fronted by **Cloudflare, which blocks the default Python
  user-agent** — every request sends a browser-like `User-Agent`.
- Search responses are under **`search_results`** (with `total_search_results`),
  not `results`. Company fields: `domain` (string), `employees_amount`,
  `finance_info.revenue`, `linkedin_info.public_profile_url`.
- A domain search returns many low-quality matches; the **best company is chosen
  by `domain_rank` then headcount**, not just the first result.
- Contact-lookup endpoints return a **bare JSON array**, e.g.
  `[{"email": "...", "email_type": "personal"}]`.

## Deployment (Railway)

The `Procfile` runs `gunicorn main:app`. Set the environment variables in
the Railway dashboard (the three Forager/HubSpot ones are required; add
`GEMINI_API_KEY` — or `ANTHROPIC_API_KEY` — to turn on scoring), deploy, then
point your HubSpot Private App's single
webhook **Target URL** at `https://<your-app>.up.railway.app/webhook` — it routes
company and contact events automatically (HubSpot only allows one Target URL
per app).
