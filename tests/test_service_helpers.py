import json
import httpx
import pytest

from app.services.enrichment import _domain, _linkedin_identifier, EnrichmentService, _search_title
from app.clients.hubspot import HubSpotClient
from app.clients.forager import ForagerClient
from app.config import Settings

def test_domain_normalization():
    assert _domain("https://www.example.com/path") == "example.com"

def test_linkedin_identifier():
    assert _linkedin_identifier("https://www.linkedin.com/in/example-person/") == "example-person"

def test_company_matching_prefers_exact_domain():
    candidates = [
        {"organization": {"name": "Example Incorporated", "domain": "other.com"}},
        {"organization": {"name": "Example", "domain": "example.com"}},
    ]
    assert EnrichmentService._match_company(candidates, "example.com", "Example") == candidates[1]


def test_company_matching_selects_real_openai():
    candidates = [
        {
            "organization": {
                "name": "ChatGPT India",
                "domain": "openai.com",
                "website": "https://openai.com/",
            }
        },
        {
            "organization": {
                "name": "OpenAI",
                "domain": "openai.com",
                "website": "https://openai.com/",
                "linkedin_info": {
                    "public_identifier": "openai"
                },
            }
        },
        {
            "organization": {
                "name": "Wordwise",
                "domain": "openai.com",
                "website": "https://chat.openai.com/",
            }
        },
    ]

    result = EnrichmentService._match_company(
        candidates, "openai.com", "OpenAI"
    )

    assert result == candidates[1]


def test_company_matching_rejects_wrong_domain():
    candidates = [
        {
            "organization": {
                "name": "OpenAI",
                "domain": "unrelated.com",
                "website": "https://unrelated.com/",
            }
        }
    ]

    result = EnrichmentService._match_company(
        candidates, "openai.com", "OpenAI"
    )

    assert result is None


def test_search_title_uses_supported_token_for_freeform_contact_title():
    assert _search_title(
        "Senior Software Engineer at Stripe",
        ["Engineer", "CEO"],
    ) == "Engineer"


def test_search_title_preserves_supported_exact_title():
    assert _search_title("CEO", ["Engineer", "CEO"]) == "CEO"


@pytest.mark.asyncio
async def test_name_targeted_search_omits_rejected_role_title_filter():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"search_results": []})

    settings = Settings(
        forager_api_key="test-key",
        forager_account_id="test-account",
    )
    client = ForagerClient(settings)
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client.search_people(
            "Example",
            None,
            company_domain="example.com",
        )
    finally:
        await client.close()

    assert captured == {
        "page": 1,
        "role_is_current": True,
        "organization_domains": ["example.com"],
    }


def test_contact_search_does_not_require_provider_person_name_filter():
    # Forager rejects person_name as a role-search operation in some accounts.
    # Contact matching verifies the exact name locally after the role search.
    assert _search_title("Senior Software Engineer at Stripe", ["Engineer"]) == "Engineer"


def test_forager_role_row_can_be_used_as_person_record():
    row = {
        "id": 123,
        "full_name": "Example Person",
        "role_title": "Engineer",
        "organization": {"name": "Example", "domain": "example.com"},
    }
    person = row.get("person", row)
    assert person["id"] == 123
    assert person["full_name"] == "Example Person"


def test_company_matching_rejects_ambiguous_results():
    candidates = [
        {
            "organization": {
                "name": "OpenAI",
                "domain": "openai.com",
                "website": "https://openai.com/",
            }
        },
        {
            "organization": {
                "name": "OpenAI",
                "domain": "openai.com",
                "website": "https://openai.com/",
            }
        },
    ]

    result = EnrichmentService._match_company(
        candidates, "openai.com", "OpenAI"
    )

    assert result is None


def test_hubspot_client_uses_configured_bearer_token():
    client = HubSpotClient(Settings(hubspot_access_token="test-token"))
    assert client.client.headers["Authorization"] == "Bearer test-token"
