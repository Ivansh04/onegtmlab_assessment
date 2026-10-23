import logging
import re
from typing import Any
from app.clients.forager import ForagerClient, ForagerError
from app.clients.hubspot import HubSpotClient, HubSpotError
from app.config import Settings
from app.schemas import EnrichmentSummary


logger = logging.getLogger(__name__)

def _domain(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^https?://", "", value)
    return value.split("/")[0].removeprefix("www.") or None

def _linkedin_identifier(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(
        r"(?:https?://)?(?:www\.)?linkedin\.com/in/([^/?#]+)",
        url.strip(),
        re.IGNORECASE,
    )
    return match.group(1) if match else None

def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None

class EnrichmentService:
    def __init__(
        self,
        settings: Settings,
        forager: ForagerClient,
        hubspot: HubSpotClient,
    ):
        self.settings = settings
        self.forager = forager
        self.hubspot = hubspot

    async def enrich_company(self, company_id: str) -> EnrichmentSummary:
        company = await self.hubspot.get_company(company_id)
        props = company.get("properties", {})
        domain = _domain(props.get("domain"))
        name = props.get("name")
        if not domain and not name:
            return EnrichmentSummary(
                company_id=company_id,
                warnings=[
                    "Company has neither domain nor name"
                ],
            )
        candidates = await self.forager.search_companies(
            domain=domain,
            name=name,
        )
        match = self._match_company(candidates, domain, name)
        warnings: list[str] = []
        if match:
            org = match.get("organization", match)
            if not isinstance(org, dict):
                warnings.append(
                    "Forager returned an unexpected organization format; "
                    "company fields were not updated"
                )
            else:
                linkedin_info = org.get("linkedin_info") or {}
                if not isinstance(linkedin_info, dict):
                    linkedin_info = {}
                finance_info = org.get("finance_info") or {}
                if not isinstance(finance_info, dict):
                    finance_info = {}
                addresses = org.get("addresses") or []
                if not isinstance(addresses, list):
                    addresses = []
                primary_address = (
                    addresses[0]
                    if addresses and isinstance(addresses[0], dict)
                    else {}
                )
                mapped = {
                    "description": _first(
                        org,
                        "description",
                        "organization_description",
                    ),
                    "numberofemployees": _first(
                        org,
                        "employees_amount",
                        "employees",
                        "employee_count",
                        "headcount",
                        "number_of_employees",
                    ),
                    "annualrevenue": finance_info.get("revenue"),
                    "linkedin_company_page": (
                        _first(
                            linkedin_info,
                            "public_profile_url",
                        )
                        or _first(
                            org,
                            "linkedin_url",
                            "linkedin_company_url",
                        )
                    ),
                    "city": primary_address.get("city"),
                    "state": primary_address.get("state"),
                    "country": primary_address.get("country"),
                }
                # Keep only fields with actual values.
                # Do not overwrite existing CRM data with empty values.
                mapped = {
                    key: (
                        str(value)
                        if key in {
                            "numberofemployees",
                            "annualrevenue",
                        }
                        else value
                    )
                    for key, value in mapped.items()
                    if value not in (None, "")
                }
                if mapped:
                    await self.hubspot.update_company(
                        company_id,
                        mapped,
                    )
        else:
            warnings.append(
                "Forager returned no confident company match; "
                "no company fields were overwritten"
            )
        contacts = await self.hubspot.contacts_for_company(company_id)
        enriched = 0
        discovered = 0
        for contact in contacts:
            try:
                await self.enrich_contact(str(contact["id"]))
                enriched += 1
            except (
                ForagerError,
                HubSpotError,
                KeyError,
                ValueError,
            ) as exc:
                logger.warning(
                    "Contact enrichment failed contact_id=%s error=%s",
                    contact.get("id"),
                    type(exc).__name__,
                )
                warnings.append(
                    f"Contact {contact.get('id')}: enrichment failed"
                )
        if len(contacts) < self.settings.max_contacts_per_company:
            discovered = await self._discover_and_create_contacts(
                company_name=name,
                company_domain=domain,
                company_id=company_id,
            )
            enriched += discovered
            if discovered == 0:
                warnings.append(
                    "No new contacts were discovered. Check the Forager "
                    "account, company domain, and ALLOWED_JOB_TITLES."
                )
        return EnrichmentSummary(
            company_id=company_id,
            contacts_found=len(contacts) + discovered,
            contacts_enriched=enriched,
            warnings=warnings,
        )

    async def _discover_and_create_contacts(
        self,
        company_name: str | None,
        company_domain: str | None,
        company_id: str,
    ) -> int:
        search_value = company_name or company_domain
        if not search_value:
            return 0
        created = 0
        seen: set[str] = set()
        for title in self.settings.job_title_terms:
            try:
                results = await self.forager.search_people(
                    search_value,
                    title,
                    company_domain=company_domain,
                )
            except ForagerError:
                logger.warning(
                    "Skipping unsupported Forager job-title filter title=%s",
                    title,
                )
                continue
            for result in results:
                person = result.get("person", result)
                organization = result.get("organization", {})
                if not isinstance(person, dict):
                    continue
                if not isinstance(organization, dict):
                    organization = {}
                employer = _first(organization, "name", "organization_name")
                if company_name and employer and (
                    str(employer).casefold() != company_name.casefold()
                ):
                    continue
                key = str(
                    person.get("id")
                    or person.get("linkedin_url")
                    or person.get("full_name")
                    or ""
                )
                if not key or key in seen:
                    continue
                seen.add(key)
                linkedin_identifier = _linkedin_identifier(
                    _first(person, "linkedin_url", "profile_url")
                )
                person_id = person.get("id")
                if not person_id and not linkedin_identifier:
                    continue
                details = await self.forager.person_details(
                    person_id=int(person_id) if person_id else None,
                    linkedin_identifier=linkedin_identifier,
                )
                emails = await self.forager.work_emails(
                    int(person_id) if person_id else None,
                    linkedin_identifier,
                )
                phones = await self.forager.phone_numbers(
                    int(person_id) if person_id else None,
                    linkedin_identifier,
                )
                properties = self._discovered_contact_properties(
                    details, person, organization, title, emails, phones
                )
                if properties:
                    await self.hubspot.create_contact(properties, company_id)
                    created += 1
                if created >= self.settings.max_contacts_per_company:
                    return created
        return created

    @staticmethod
    def _discovered_contact_properties(
        details: dict[str, Any],
        person: dict[str, Any],
        organization: dict[str, Any],
        fallback_title: str,
        emails: list[dict[str, Any]],
        phones: list[dict[str, Any]],
    ) -> dict[str, Any]:
        role = details.get("current_role")
        if not isinstance(role, dict):
            role = {}
        values = {
            "firstname": _first(details, "first_name", "firstname"),
            "lastname": _first(details, "last_name", "lastname"),
            "jobtitle": (
                _first(details, "job_title", "headline")
                or _first(role, "title", "role_title")
                or fallback_title
            ),
            "hs_linkedin_url": (
                _first(
                    details,
                    "linkedin_url",
                    "linkedin_profile_url",
                    "profile_url",
                )
                or _first(person, "linkedin_url", "profile_url")
            ),
            "email": _first(emails[0], "email") if emails else None,
            "phone": (
                _first(phones[0], "phone_number", "phone")
                if phones else None
            ),
            "company": _first(organization, "name", "organization_name"),
            "city": _first(details, "city"),
            "state": _first(details, "state", "region"),
            "country": _first(details, "country"),
        }
        return {
            key: value for key, value in values.items()
            if value not in (None, "")
        }


    @staticmethod
    def _match_company(
        candidates: list[dict[str, Any]],
        domain: str | None,
        name: str | None,
    ) -> dict[str, Any] | None:
        normalized_domain = _domain(domain)
        normalized_name = name.strip().casefold() if name else None

        matches: list[tuple[int, dict[str, Any]]] = []

        for item in candidates:
            org = item.get("organization", item)
            if not isinstance(org, dict):
                continue

            candidate_name = _first(
                org, "name", "legal_name", "organization_name"
            )
            candidate_name = (
                candidate_name.strip().casefold()
                if isinstance(candidate_name, str)
                else None
            )

            candidate_domain = _domain(
                _first(org, "domain", "primary_domain")
            )
            website_domain = _domain(org.get("website"))

            # If the CRM has a domain, require an exact domain match.
            if normalized_domain and normalized_domain not in {
                candidate_domain,
                website_domain,
            }:
                continue

            # Read the LinkedIn company identifier when available.
            linkedin_info = org.get("linkedin_info") or {}
            linkedin_identifier = (
                linkedin_info.get("public_identifier")
                if isinstance(linkedin_info, dict)
                else None
            )
            linkedin_identifier = (
                linkedin_identifier.strip().casefold()
                if isinstance(linkedin_identifier, str)
                else None
            )

            score = 0

            if normalized_name and candidate_name == normalized_name:
                score += 4

            if normalized_domain and candidate_domain == normalized_domain:
                score += 3

            if normalized_domain and website_domain == normalized_domain:
                score += 2

            # Prefer the official OpenAI LinkedIn company identifier.
            if (
                normalized_name == "openai"
                and linkedin_identifier == "openai"
            ):
                return item

            # Without a domain, require an exact company-name match.
            if not normalized_domain and (
                not normalized_name or candidate_name != normalized_name
            ):
                continue

            if score:
                matches.append((score, item))

        if not matches:
            return None

        matches.sort(key=lambda match: match[0], reverse=True)

        exact_name_domain = [
            item
            for score, item in matches
            if normalized_name
            and normalized_domain
            and _domain(
                _first(
                    item.get("organization", item),
                    "domain",
                    "primary_domain",
                )
            ) == normalized_domain
            and (
                _first(
                    item.get("organization", item),
                    "name",
                    "legal_name",
                    "organization_name",
                )
                or ""
            ).strip().casefold() == normalized_name
        ]
        if len(exact_name_domain) == 1:
            return exact_name_domain[0]

        # Reject tied top results rather than selecting arbitrarily.
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            logger.warning(
                "Ambiguous Forager company match: domain=%s name=%s",
                normalized_domain,
                normalized_name,
            )
            return None

        return matches[0][1]


    async def enrich_contact(
        self,
        contact_id: str,
    ) -> dict[str, Any]:
        contact = await self.hubspot.get_contact(contact_id)
        props = contact.get("properties", {})
        identifier = _linkedin_identifier(
            props.get("hs_linkedin_url")
        )
        email = props.get("email")
        first = props.get("firstname")
        last = props.get("lastname")
        company_name = props.get("company")
        if not identifier and not (
            first and last and company_name
        ):
            raise ValueError(
                "Contact needs LinkedIn URL or first name, "
                "last name, and company"
            )
        person_id = None
        details: dict[str, Any] = {}
        if identifier:
            details = await self.forager.person_details(
                linkedin_identifier=identifier
            )
        else:
            # Search by title, then validate the person's name
            # and employer before accepting a result.
            for title in (
                props.get("jobtitle"),
                *self.settings.job_title_terms,
            ):
                if not title:
                    continue
                try:
                    results = await self.forager.search_people(
                        company_name,
                        title,
                    )
                except ForagerError:
                    logger.warning(
                        "Skipping unsupported Forager job-title filter title=%s",
                        title,
                    )
                    continue
                for result in results:
                    person = result.get("person", result)
                    if not isinstance(person, dict):
                        continue
                    found_name = (
                        _first(person, "full_name", "name") or ""
                    ).casefold()
                    found_org = result.get("organization", {})
                    if not isinstance(found_org, dict):
                        found_org = {}
                    found_company = (
                        _first(found_org, "name") or ""
                    ).casefold()
                    wanted_name = (
                        f"{first} {last}".strip().casefold()
                    )
                    if (
                        found_name == wanted_name
                        and (
                            not found_company
                            or found_company == company_name.casefold()
                        )
                    ):
                        person_id = (
                            person.get("id")
                            or result.get("person_id")
                        )
                        break
                if person_id:
                    break
            if person_id:
                details = await self.forager.person_details(
                    person_id=int(person_id)
                )
        if not details:
            raise ForagerError(
                "Forager returned no person detail match"
            )
        person_id = person_id or details.get("id")
        linkedin = _first(
            details,
            "linkedin_url",
            "linkedin_profile_url",
            "profile_url",
        )
        linkedin = linkedin or props.get("hs_linkedin_url")
        identifier = _linkedin_identifier(linkedin) or identifier
        emails: list[dict[str, Any]] = []
        phones: list[dict[str, Any]] = []
        if person_id or identifier:
            try:
                emails = await self.forager.work_emails(
                    int(person_id) if person_id else None,
                    identifier,
                )
            except ForagerError:
                logger.info(
                    "Work email lookup unavailable for contact_id=%s",
                    contact_id,
                )
            try:
                phones = await self.forager.phone_numbers(
                    int(person_id) if person_id else None,
                    identifier,
                )
            except ForagerError:
                logger.info(
                    "Phone lookup unavailable for contact_id=%s",
                    contact_id,
                )
        role = details.get("current_role")
        if not isinstance(role, dict):
            role = {}
        org = details.get("organization")
        if not isinstance(org, dict):
            org = {}
        location = _first(details, "location", "city")
        city = _first(details, "city") or (
            location.split(",")[0].strip()
            if isinstance(location, str)
            else None
        )
        state = _first(details, "state", "region")
        country = _first(details, "country")
        if isinstance(location, str) and "," in location:
            parts = [
                part.strip()
                for part in location.split(",")
            ]
            if not state and len(parts) >= 2:
                state = parts[1]
            if not country and len(parts) >= 3:
                country = parts[-1]
        found_email = (
            _first(emails[0], "email")
            if emails
            else None
        )
        found_phone = (
            _first(phones[0], "phone_number", "phone")
            if phones
            else None
        )
        enriched = {
            "firstname": (
                _first(details, "first_name", "firstname")
                or first
            ),
            "lastname": (
                _first(details, "last_name", "lastname")
                or last
            ),
            "jobtitle": (
                _first(details, "job_title", "headline")
                or _first(role, "title", "role_title")
                or props.get("jobtitle")
            ),
            "hs_linkedin_url": linkedin,
            "email": email or found_email,
            "phone": found_phone or props.get("phone"),
            "company": (
                _first(org, "name", "organization_name")
                or company_name
            ),
            "city": city or props.get("city"),
            "state": state or props.get("state"),
            "country": country or props.get("country"),
        }
        # Avoid overwriting existing CRM values with empty data.
        enriched = {
            key: value
            for key, value in enriched.items()
            if value not in (None, "")
        }
        return await self.hubspot.update_contact(
            contact_id,
            enriched,
        )
