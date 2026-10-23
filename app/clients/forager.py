import logging
from typing import Any
import httpx
from app.config import Settings

logger = logging.getLogger(__name__)

class ForagerError(RuntimeError):
    pass

class ForagerClient:
    """Thin, typed wrapper around Forager's documented v2 endpoints."""
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.forager_api_base_url.rstrip("/")
        self.account_id = settings.forager_account_id
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=8.0),
            headers={"X-API-KEY": settings.forager_api_key, "Accept": "application/json"},
        )

    async def close(self):
        await self.client.aclose()

    def _url(self, path: str) -> str:
        if not self.account_id:
            raise ForagerError("FORAGER_ACCOUNT_ID is not configured")

        normalized_path = path.strip("/")
        return (
            f"{self.base}/api/{self.account_id}"
            f"/datastorage/{normalized_path}/"
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        if not self.settings.forager_api_key:
            raise ForagerError("FORAGER_API_KEY is not configured")
        try:
            response = await self.client.post(
                self._url(path),
                json=payload,
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Forager API returned HTTP %s for endpoint %s response=%s",
                exc.response.status_code,
                path,
                exc.response.text[:500],
            )
            raise ForagerError(
                f"Forager API error ({exc.response.status_code})"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ForagerError(f"Forager request failed: {type(exc).__name__}") from exc

    async def search_companies(
        self,
        domain: str | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"page": 1}

        if domain:
            payload["domains"] = [domain]

        if name:
            payload["name"] = name

        result = await self._post("organization_search/", payload)

        if not isinstance(result, dict):
            return []

        return result.get("search_results", result.get("results", []))


    async def search_people(
        self,
        company_name: str,
        job_title: str,
        page: int = 1,
        company_domain: str | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "page": page,
            "role_title": job_title,
            "role_is_current": True,
        }
        if company_domain:
            payload["organization_domains"] = [company_domain]
        result = await self._post("person_role_search/", payload)
        return result.get("search_results", result.get("results", [])) if isinstance(result, dict) else []

    async def person_details(self, person_id: int | None = None, linkedin_identifier: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reveal_work_emails": True,
            "reveal_personal_emails": False,
            "reveal_phone_numbers": False,
            "do_contacts_enrichment": False,
        }
        if person_id is not None:
            payload["person_id"] = person_id
        if linkedin_identifier:
            payload["linkedin_public_identifier"] = linkedin_identifier
        if person_id is None and not linkedin_identifier:
            raise ValueError("person_id or linkedin_identifier is required")
        result = await self._post("person_detail_lookup/", payload)
        return result if isinstance(result, dict) else {}

    async def work_emails(self, person_id: int | None, linkedin_identifier: str | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"do_contacts_enrichment": True}
        if person_id is not None: payload["person_id"] = person_id
        if linkedin_identifier: payload["linkedin_public_identifier"] = linkedin_identifier
        result = await self._post("person_contacts_lookup/work_emails/", payload)
        return result if isinstance(result, list) else []

    async def phone_numbers(self, person_id: int | None, linkedin_identifier: str | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {}
        if person_id is not None: payload["person_id"] = person_id
        if linkedin_identifier: payload["linkedin_public_identifier"] = linkedin_identifier
        result = await self._post("person_contacts_lookup/phone_numbers/", payload)
        return result if isinstance(result, list) else []
