from app.services.enrichment import _domain, _linkedin_identifier, EnrichmentService, _search_title
from app.clients.hubspot import HubSpotClient
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
