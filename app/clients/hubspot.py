import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class HubSpotError(RuntimeError):
    pass


class HubSpotClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.hubspot_api_base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=8.0),
            headers={
                "Authorization": "Bearer " + settings.hubspot_access_token,
                "Content-Type": "application/json",
            },
        )

    async def close(self):
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.settings.hubspot_access_token:
            raise HubSpotError("HUBSPOT_ACCESS_TOKEN is not configured")
        try:
            response = await self.client.request(
                method, f"{self.base}{path}", **kwargs
            )
            response.raise_for_status()
            return response.json() if response.content else {}
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HubSpot API returned HTTP %s for %s: %s",
                exc.response.status_code,
                path,
                exc.response.text,
            )
            raise HubSpotError(
                f"HubSpot API error ({exc.response.status_code})"
            ) from exc
        except httpx.HTTPError as exc:
            raise HubSpotError(
                f"HubSpot request failed: {type(exc).__name__}"
            ) from exc

    async def get_company(self, company_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/crm/v3/objects/companies/{company_id}",
            params={
                "properties": (
                    "name,domain,description,linkedin_company_page,"
                    "numberofemployees,annualrevenue,city,state,country"
                )
            },
        )

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/crm/v3/objects/contacts/{contact_id}",
            params={
                "properties": (
                    "firstname,lastname,email,phone,jobtitle,hs_linkedin_url,"
                    "city,state,country,company,associatedcompanyid"
                )
            },
        )

    async def search_company_by_domain(
        self, domain: str
    ) -> dict[str, Any] | None:
        result = await self._request(
            "POST",
            "/crm/v3/objects/companies/search",
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "domain",
                                "operator": "EQ",
                                "value": domain,
                            }
                        ]
                    }
                ],
                "properties": [
                    "name",
                    "domain",
                    "description",
                    "linkedin_company_page",
                    "numberofemployees",
                    "annualrevenue",
                    "city",
                    "state",
                    "country",
                ],
                "limit": 1,
            },
        )
        return (result.get("results") or [None])[0]

    async def contacts_for_company(
        self, company_id: str
    ) -> list[dict[str, Any]]:
        result = await self._request(
            "GET",
            f"/crm/v4/objects/companies/{company_id}/associations/contacts",
            params={"limit": 100},
        )
        ids = [
            str(item.get("toObjectId"))
            for item in result.get("results", [])
            if item.get("toObjectId")
        ]
        contacts = []
        for contact_id in ids:
            try:
                contacts.append(await self.get_contact(contact_id))
            except HubSpotError:
                logger.warning(
                    "Unable to read associated contact id=%s", contact_id
                )
        return contacts

    async def update_company(
        self, company_id: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        clean = {
            key: value
            for key, value in properties.items()
            if value is not None and value != ""
        }
        if not clean:
            return {}
        return await self._request(
            "PATCH",
            f"/crm/v3/objects/companies/{company_id}",
            json={"properties": clean},
        )

    async def update_contact(
        self, contact_id: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        clean = {
            key: value
            for key, value in properties.items()
            if value is not None and value != ""
        }
        if not clean:
            return {}
        return await self._request(
            "PATCH",
            f"/crm/v3/objects/contacts/{contact_id}",
            json={"properties": clean},
        )

    async def create_contact(
        self,
        properties: dict[str, Any],
        company_id: str | None = None,
    ) -> dict[str, Any]:
        clean = {
            key: value
            for key, value in properties.items()
            if value is not None and value != ""
        }
        body: dict[str, Any] = {"properties": clean}
        if company_id:
            body["associations"] = [
                {
                    "to": {"id": str(company_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 279,
                        }
                    ],
                }
            ]
        return await self._request(
            "POST", "/crm/v3/objects/contacts", json=body
        )
