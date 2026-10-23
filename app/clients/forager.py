import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class ForagerError(RuntimeError):
    """Raised when Forager cannot fulfill a request."""


class ForagerCreditsExhausted(ForagerError):
    """Raised when the Forager account has no credits remaining."""


class ForagerClient:
    """Small async client for the documented Forager v2 API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.forager_api_base_url.rstrip("/")
        self.account_id = settings.forager_account_id
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=8.0),
            headers={
                "X-API-KEY": settings.forager_api_key,
                "Accept": "application/json",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _url(self, path: str) -> str:
        if not self.account_id:
            raise ForagerError("FORAGER_ACCOUNT_ID is not configured")
        return f"{self.base}/api/{self.account_id}/datastorage/{path.strip('/')}/"

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        if not self.settings.forager_api_key:
            raise ForagerError("FORAGER_API_KEY is not configured")

        try:
            response = await self.client.post(
                self._url(path), json=payload, follow_redirects=True
            )
            if response.status_code == 402:
                # Do not retry a depleted account; retries cannot restore credits.
                logger.error(
                    "Forager credits exhausted endpoint=%s status=402", path
                )
                raise ForagerCreditsExhausted(
                    "Forager account has insufficient credits; request was not retried"
                )
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                raise ForagerError("Forager returned invalid JSON") from exc
        except ForagerCreditsExhausted:
            raise

        except httpx.HTTPStatusError as exc:
         # Extract validation errors for debugging without logging the full response.
            detail = ""
            try:
                body = exc.response.json()
                if isinstance(body, dict):
                    detail = str(body.get("message") or body.get("detail") or "")
                    if not detail:
                        errors = {
                            key: value
                            for key, value in body.items()
                            if key in {"role_title", "person_name", "organization_domains"}
                        }
                        detail = str(errors) if errors else ""
            except ValueError:
                pass

            logger.warning(
                "Forager API returned HTTP %s endpoint=%s detail=%s",
                exc.response.status_code,
                path,
                detail[:500],
            )
            raise ForagerError(
                f"Forager API error ({exc.response.status_code})"
                + (f": {detail[:500]}" if detail else "")
            ) from exc

        except httpx.HTTPError as exc:
            raise ForagerError(
                f"Forager request failed: {type(exc).__name__}"
            ) from exc

    async def search_companies(
        self,
        domain: str | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search by exact domain. The documented endpoint has no name filter."""
        del name  # Company-name search is not a documented organization_search filter.
        if not domain:
            return []
        result = await self._post(
            "organization_search/", {"page": 1, "domains": [domain]}
        )
        if not isinstance(result, dict):
            return []
        rows = result.get("search_results", result.get("results", []))
        return rows if isinstance(rows, list) else []

    async def search_people(
        self,
        company_name: str,
        job_title: str | None,
        page: int = 1,
        company_domain: str | None = None,
        person_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search current roles once using documented filters."""
        del company_name  # Forager filters by domain, not a free-text company name.
        payload: dict[str, Any] = {
            "page": page,
            "role_is_current": True,
        }
        if job_title and job_title.strip():
            payload["role_title"] = job_title.strip()
        if company_domain:
            payload["organization_domains"] = [company_domain]
        if person_name:
            payload["person_name"] = person_name.strip()
        result = await self._post("person_role_search/", payload)
        if not isinstance(result, dict):
            return []
        rows = result.get("search_results", result.get("results", []))
        return rows if isinstance(rows, list) else []

    async def person_details(
        self,
        person_id: int | None = None,
        linkedin_identifier: str | None = None,
    ) -> dict[str, Any]:
        if person_id is None and not linkedin_identifier:
            raise ValueError("person_id or linkedin_identifier is required")
        payload: dict[str, Any] = {
            "reveal_work_emails": False,
            "reveal_personal_emails": False,
            "reveal_phone_numbers": False,
            "do_contacts_enrichment": False,
        }
        if person_id is not None:
            payload["person_id"] = person_id
        if linkedin_identifier:
            payload["linkedin_public_identifier"] = linkedin_identifier
        result = await self._post("person_detail_lookup/", payload)
        return result if isinstance(result, dict) else {}

    async def work_emails(
        self, person_id: int | None, linkedin_identifier: str | None
    ) -> list[dict[str, Any]]:
        """Explicit paid lookup; deliberately not called by automatic enrichment."""
        if person_id is None and not linkedin_identifier:
            return []
        payload: dict[str, Any] = {"do_contacts_enrichment": True}
        if person_id is not None:
            payload["person_id"] = person_id
        if linkedin_identifier:
            payload["linkedin_public_identifier"] = linkedin_identifier
        result = await self._post("person_contacts_lookup/work_emails/", payload)
        return result if isinstance(result, list) else []

    async def phone_numbers(
        self, person_id: int | None, linkedin_identifier: str | None
    ) -> list[dict[str, Any]]:
        """Explicit expensive lookup; deliberately not called automatically."""
        if person_id is None and not linkedin_identifier:
            return []
        payload: dict[str, Any] = {}
        if person_id is not None:
            payload["person_id"] = person_id
        if linkedin_identifier:
            payload["linkedin_public_identifier"] = linkedin_identifier
        result = await self._post("person_contacts_lookup/phone_numbers/", payload)
        return result if isinstance(result, list) else []
