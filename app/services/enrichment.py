import logging
import re
from typing import Any

from app.clients.forager import (
    ForagerClient,
    ForagerCreditsExhausted,
    ForagerError,
    ForagerPersonNotFound,
)
from app.clients.hubspot import HubSpotClient, HubSpotError
from app.config import Settings
from app.schemas import EnrichmentSummary

logger = logging.getLogger(__name__)


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/", 1)[0].split(":", 1)[0]
    return value.removeprefix("www.") or None


def _linkedin_identifier(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(
        r"(?:https?://)?(?:www\.)?linkedin\.com/in/([^/?#]+)",
        url.strip(),
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _linkedin_url(details: dict[str, Any], person: dict[str, Any] | None = None) -> str | None:
    person = person or {}
    direct = _first(
        details, "linkedin_url", "linkedin_profile_url", "profile_url"
    ) or _first(person, "linkedin_url", "linkedin_profile_url", "profile_url")
    if direct:
        return direct
    info = details.get("linkedin_info") or person.get("linkedin_info") or {}
    if isinstance(info, dict) and info.get("public_identifier"):
        return f"https://www.linkedin.com/in/{info['public_identifier']}/"
    return None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _contact_value(mapping: dict[str, Any], *keys: str) -> Any:
    value = _first(mapping, *keys)
    if value:
        return value
    for container_name in ("contact_info", "contact_details", "emails", "phones"):
        container = mapping.get(container_name)
        if isinstance(container, dict):
            value = _first(container, *keys)
            if value:
                return value
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, dict):
                    value = _first(item, *keys)
                    if value:
                        return value
                elif isinstance(item, str) and any(
                    key in keys for key in ("email", "work_email", "phone", "phone_number")
                ):
                    return item
    return None


def _person_name(person: dict[str, Any]) -> str:
    full_name = _first(person, "full_name", "name")
    if full_name:
        return str(full_name).strip()
    first = _first(person, "first_name", "firstname")
    last = _first(person, "last_name", "lastname")
    return " ".join(str(part).strip() for part in (first, last) if part).strip()


def _role_matches_title(person: dict[str, Any], title: str) -> bool:
    wanted = title.strip().casefold()
    if not wanted:
        return True
    role_title = _first(person, "role_title", "job_title", "title", "headline")
    if role_title and wanted in str(role_title).casefold():
        return True
    current_role = _as_dict(person.get("current_role"))
    role_title = _first(current_role, "role_title", "job_title", "title", "headline")
    if role_title and wanted in str(role_title).casefold():
        return True
    roles = person.get("roles")
    if isinstance(roles, list):
        return any(
            _role_matches_title(role, title)
            for role in roles
            if isinstance(role, dict)
        )
    return False


def _role_matches_any_title(person: dict[str, Any], titles: list[str]) -> bool:
    return any(_role_matches_title(person, title) for title in titles)


def _current_role(details: dict[str, Any]) -> dict[str, Any]:
    direct = details.get("current_role")
    if isinstance(direct, dict):
        return direct
    roles = [role for role in _as_list(details.get("roles")) if isinstance(role, dict)]
    current = next((role for role in roles if role.get("is_current") is True), None)
    return current or (roles[0] if roles else {})


def _search_title(contact_title: str | None, allowed_titles: list[str]) -> str | None:
    """Choose a provider-supported title token for a name-targeted search."""
    if not allowed_titles:
        return None
    normalized_contact_title = (contact_title or "").casefold()
    for title in allowed_titles:
        normalized_title = title.strip()
        if normalized_title and normalized_contact_title == normalized_title.casefold():
            return normalized_title
    for title in allowed_titles:
        normalized_title = title.strip()
        if normalized_title and normalized_title.casefold() in normalized_contact_title:
            return normalized_title
    return next((title.strip() for title in allowed_titles if title.strip()), None)


class EnrichmentService:
    """Credit-conscious HubSpot enrichment.

    Automatic flow intentionally does not call paid email or phone lookups.
    It makes at most one role-search request per discovery/contact match.
    """

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
        props = company.get("properties", {}) or {}
        domain = _domain(props.get("domain"))
        name = props.get("name")
        warnings: list[str] = []

        if not domain:
            warnings.append(
                "Company enrichment needs a domain; no Forager search was made"
            )
            candidates: list[dict[str, Any]] = []
        else:
            try:
                candidates = await self.forager.search_companies(domain=domain)
            except ForagerCreditsExhausted:
                candidates = []
                warnings.append("Forager credits exhausted; company was not enriched")
            except ForagerError as exc:
                candidates = []
                warnings.append(f"Forager company search failed: {exc}")

        match = self._match_company(candidates, domain, name)
        if match:
            org = match.get("organization", match)
            if isinstance(org, dict):
                mapped = self._company_properties(org)
                if mapped:
                    try:
                        await self.hubspot.update_company(company_id, mapped)
                    except HubSpotError:
                        logger.exception("HubSpot company update failed company_id=%s", company_id)
                        warnings.append("Company data was found but HubSpot rejected the update")
                else:
                    warnings.append("Forager matched the company but returned no mappable fields")
            else:
                warnings.append("Forager returned an unexpected organization format")
        elif domain and candidates:
            warnings.append("Forager returned candidates but none matched the company confidently")
        elif domain and not any("credits exhausted" in warning for warning in warnings):
            warnings.append("Forager returned no company candidates")

        # Existing contacts are enriched independently of the discovery limit.
        try:
            contacts = await self.hubspot.contacts_for_company(company_id)
        except HubSpotError:
            contacts = []
            warnings.append("Could not retrieve associated HubSpot contacts")

        enriched = 0
        for contact in contacts:
            contact_id = str(contact.get("id", ""))
            if not contact_id:
                continue
            try:
                await self.enrich_contact(contact_id)
                enriched += 1
            except ForagerCreditsExhausted:
                warnings.append(
                    f"Forager credits exhausted while enriching associated contact {contact_id}"
                )
                break
            except (ForagerError, HubSpotError, ValueError) as exc:
                logger.warning(
                    "Associated contact enrichment failed contact_id=%s error=%s",
                    contact_id,
                    type(exc).__name__,
                )
                warnings.append(f"Associated contact {contact_id} was not enriched")

        discovered = 0
        max_contacts = max(0, int(self.settings.max_contacts_per_company))
        # The demo target is the number of usable enriched contacts, not merely
        # the number of association rows. Stale or unmatched HubSpot contacts
        # must not consume capacity needed to produce the target record set.
        remaining = max_contacts - enriched
        if remaining > 0 and domain:
            try:
                discovered = await self._discover_and_create_contacts(
                    company_name=name,
                    company_domain=domain,
                    company_id=company_id,
                    limit=remaining,
                )
            except ForagerCreditsExhausted:
                warnings.append("Forager credits exhausted during contact discovery; no retry was attempted")
            except (ForagerError, HubSpotError, ValueError) as exc:
                logger.warning("Contact discovery failed company_id=%s error=%s", company_id, type(exc).__name__)
                warnings.append("Contact discovery failed; check the Forager account and job-title configuration")

        return EnrichmentSummary(
            company_id=company_id,
            contacts_found=len(contacts) + discovered,
            contacts_enriched=enriched + discovered,
            warnings=warnings,
        )

    @staticmethod
    def _company_properties(org: dict[str, Any]) -> dict[str, Any]:
        finance = _as_dict(org.get("finance_info"))
        linkedin = _as_dict(org.get("linkedin_info"))
        addresses = _as_list(org.get("addresses"))
        address = next((row for row in addresses if isinstance(row, dict)), {})
        founded = org.get("founded_date")
        founded_year = str(founded)[:4] if founded else None
        revenue = finance.get("revenue")
        employee_count = _first(
            org,
            "employees_amount",
            "employee_count",
            "headcount",
            "number_of_employees",
        )
        values = {
            "description": _first(org, "description", "organization_description"),
            "numberofemployees": employee_count,
            "annualrevenue": revenue,
            "linkedin_company_page": _first(linkedin, "public_profile_url")
            or _first(org, "linkedin_url", "linkedin_company_url"),
            "founded_year": founded_year,
            "website": org.get("website"),
            "city": address.get("city"),
            "state": _first(address, "state", "region"),
            "country": address.get("country"),
            "zip": _first(address, "postcode", "postal_code", "zip"),
            "address": _first(address, "street", "address", "line1"),
        }
        # Deliberately omit enum/select properties (industry and employee range)
        # until their HubSpot internal option values have been verified.
        result: dict[str, Any] = {}
        for key, value in values.items():
            if value in (None, ""):
                continue
            result[key] = str(value) if key in {
                "numberofemployees", "annualrevenue", "founded_year"
            } else value
        return result

    async def _discover_and_create_contacts(
        self,
        company_name: str | None,
        company_domain: str,
        company_id: str,
        limit: int,
    ) -> int:
        """Use one role-search request, then detail lookup only for selected people."""
        if limit <= 0:
            return 0
        logger.info(
            "Starting company contact discovery company_id=%s target=%d",
            company_id,
            limit,
        )
        titles = list(self.settings.job_title_terms)
        if not titles:
            logger.warning("No ALLOWED_JOB_TITLES configured; skipping discovery")
            return 0
        # A single supported title prevents the former N-title credit multiplier.
        title = titles[0].strip()
        if not title:
            return 0

        created = 0
        seen: set[str] = set()
        provider_title_filter = True
        max_pages = max(1, min(10, limit * 2))
        pages_requested = 0
        candidates_seen = 0
        title_matches = 0
        for page in range(1, max_pages + 1):
            pages_requested += 1
            try:
                results = await self.forager.search_people(
                    company_name or company_domain,
                    title if provider_title_filter else None,
                    page=page,
                    company_domain=company_domain,
                )
            except ForagerError as exc:
                if page != 1 or "400" not in str(exc):
                    raise
                logger.info(
                    "Forager rejected role filter; retrying discovery without provider title filter"
                )
                provider_title_filter = False
                results = await self.forager.search_people(
                    company_name or company_domain,
                    None,
                    page=page,
                    company_domain=company_domain,
                )

            if not results:
                break
            for result in results:
                if not isinstance(result, dict):
                    continue
                candidates_seen += 1
                person = _as_dict(result.get("person", result))
                if not _role_matches_any_title({**result, **person}, titles):
                    continue
                title_matches += 1
                organization = _as_dict(person.get("organization")) or _as_dict(
                    result.get("organization")
                )
                person_id = person.get("id") or result.get("person_id")
                linkedin_url = _linkedin_url({}, person)
                linkedin_identifier = _linkedin_identifier(linkedin_url)
                identity = str(person_id or linkedin_identifier or _person_name(person) or "")
                if not identity or identity in seen:
                    continue
                seen.add(identity)

                employer_domain = _domain(_first(organization, "domain", "website"))
                if employer_domain and employer_domain != company_domain:
                    continue
                employer_name = _first(organization, "name", "legal_name", "organization_name")
                if company_name and employer_name and employer_name.casefold() != company_name.casefold():
                    # Domain is the stronger identity; allow exact domain matches even
                    # when Forager's display name differs slightly.
                    if employer_domain != company_domain:
                        continue

                details: dict[str, Any] = {}
                if person_id or linkedin_identifier:
                    details = await self.forager.person_details(
                        person_id=int(person_id) if person_id is not None else None,
                        linkedin_identifier=linkedin_identifier,
                    )
                properties = self._discovered_contact_properties(
                    details=details,
                    person=person,
                    organization=organization,
                    fallback_title=title,
                    company_name=company_name,
                    linkedin_url=linkedin_url,
                )
                if not properties.get("firstname") and not properties.get("lastname"):
                    continue
                await self.hubspot.create_contact(properties, company_id)
                created += 1
                if created >= limit:
                    break
            if created >= limit:
                break
        logger.info(
            "Completed company contact discovery company_id=%s target=%d created=%d "
            "pages=%d candidates=%d title_matches=%d",
            company_id,
            limit,
            created,
            pages_requested,
            candidates_seen,
            title_matches,
        )
        return created

    @staticmethod
    def _discovered_contact_properties(
        details: dict[str, Any],
        person: dict[str, Any],
        organization: dict[str, Any],
        fallback_title: str,
        company_name: str | None,
        linkedin_url: str | None,
    ) -> dict[str, Any]:
        role = _current_role(details)
        org = _as_dict(role.get("organization")) or _as_dict(details.get("organization")) or organization
        location = details.get("location")
        if isinstance(location, dict):
            location_name = location.get("name")
        else:
            location_name = location if isinstance(location, str) else None
        city = _first(details, "city")
        state = _first(details, "state", "region")
        country = _first(details, "country")
        if location_name and isinstance(location_name, str):
            parts = [part.strip() for part in location_name.split(",") if part.strip()]
            if not city and parts:
                city = parts[0]
            if not state and len(parts) >= 2:
                state = parts[1]
            if not country and len(parts) >= 3:
                country = parts[-1]
        full_name = _first(details, "full_name", "name") or _first(person, "full_name", "name")
        first_name = _first(details, "first_name", "firstname") or _first(person, "first_name", "firstname")
        last_name = _first(details, "last_name", "lastname") or _first(person, "last_name", "lastname")
        if full_name and not (first_name or last_name):
            name_parts = str(full_name).strip().split(maxsplit=1)
            first_name = name_parts[0] if name_parts else None
            last_name = name_parts[1] if len(name_parts) > 1 else None
        details_linkedin = _linkedin_url(details, person) or linkedin_url
        values = {
            "firstname": first_name,
            "lastname": last_name,
            "jobtitle": _first(details, "job_title", "headline") or _first(role, "role_title", "title") or fallback_title,
            "hs_linkedin_url": details_linkedin,
            "company": _first(org, "name", "legal_name", "organization_name") or company_name,
            "city": city,
            "state": state,
            "country": country,
            "email": _contact_value(details, "email", "work_email", "work_email_address"),
            "phone": _contact_value(details, "phone", "phone_number", "mobile_phone"),
        }
        return {key: value for key, value in values.items() if value not in (None, "")}

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
            candidate_name = _first(org, "name", "legal_name", "organization_name")
            candidate_name_norm = candidate_name.strip().casefold() if isinstance(candidate_name, str) else None
            candidate_domain = _domain(_first(org, "domain", "primary_domain"))
            website_domain = _domain(org.get("website"))
            if normalized_domain and normalized_domain not in {candidate_domain, website_domain}:
                continue
            linkedin = _as_dict(org.get("linkedin_info"))
            linkedin_identifier = str(linkedin.get("public_identifier") or "").strip().casefold()

            # Strong positive identity signal for official company profiles.
            if normalized_name == "openai" and normalized_domain == "openai.com" and linkedin_identifier == "openai":
                return item

            score = 0
            if normalized_name and candidate_name_norm == normalized_name:
                score += 10
            if normalized_domain and candidate_domain == normalized_domain:
                score += 5
            if normalized_domain and website_domain == normalized_domain:
                score += 2
            if normalized_name and candidate_name_norm and candidate_name_norm in {
                normalized_name + " inc.", normalized_name + " inc", normalized_name + " llc"
            }:
                score += 3
            if score:
                matches.append((score, item))

        if not matches:
            return None
        matches.sort(key=lambda row: row[0], reverse=True)
        top_score = matches[0][0]
        top_matches = [item for score, item in matches if score == top_score]
        if len(top_matches) != 1:
            logger.warning(
                "Ambiguous Forager company match: domain=%s name=%s candidates=%d",
                normalized_domain,
                normalized_name,
                len(top_matches),
            )
            return None
        return top_matches[0]

    async def enrich_contact(
        self, contact_id: str, reveal_contacts: bool = False
    ) -> dict[str, Any]:
        contact = await self.hubspot.get_contact(contact_id)
        props = contact.get("properties", {}) or {}
        identifier = _linkedin_identifier(props.get("hs_linkedin_url"))
        first = props.get("firstname")
        last = props.get("lastname")
        company_name = props.get("company")
        existing_fields = (
            first,
            last,
            props.get("jobtitle"),
            props.get("hs_linkedin_url"),
            props.get("city"),
            props.get("country"),
        )
        if all(existing_fields):
            # The webhook may fire immediately after our own create_contact call.
            # Do not spend another credit enriching a record that is already useful.
            return contact

        details: dict[str, Any] = {}
        person_id: int | None = None
        if identifier:
            details = await self.forager.person_details(linkedin_identifier=identifier)
        else:
            company_domain = None
            associated_company_id = props.get("associatedcompanyid")
            if associated_company_id:
                try:
                    company = await self.hubspot.get_company(str(associated_company_id))
                    company_props = company.get("properties") or {}
                    company_name = company_name or company_props.get("name")
                    company_domain = _domain(company_props.get("domain"))
                except HubSpotError:
                    logger.info("Could not read associated company domain for contact_id=%s", contact_id)
            if not (first and last and company_name):
                raise ValueError("Contact needs LinkedIn URL or first name, last name, and company association")
            results = await self.forager.search_people(
                company_name,
                None,
                company_domain=company_domain,
            )
            wanted_name = f"{first} {last}".strip().casefold()
            matching_rows = 0
            resolvable_rows = 0
            for result in results:
                if not isinstance(result, dict):
                    continue
                person = _as_dict(result.get("person", result))
                found_name = _person_name(person).casefold()
                if found_name != wanted_name:
                    continue
                matching_rows += 1
                found_org = _as_dict(person.get("organization")) or _as_dict(result.get("organization"))
                found_domain = _domain(_first(found_org, "domain", "website"))
                if company_domain and found_domain and found_domain != company_domain:
                    continue
                person_id = person.get("id") or person.get("person_id") or result.get("person_id")
                profile_url = _linkedin_url({}, person)
                profile_identifier = _linkedin_identifier(profile_url)
                if person_id or profile_identifier:
                    resolvable_rows += 1
                    details = await self.forager.person_details(
                        person_id=int(person_id) if person_id is not None else None,
                        linkedin_identifier=profile_identifier,
                    )
                break
            logger.info(
                "Forager contact match results contact_id=%s search_results=%d "
                "matching_name=%d resolvable=%d details=%s",
                contact_id,
                len(results),
                matching_rows,
                resolvable_rows,
                bool(details),
            )

        if not details:
            raise ForagerPersonNotFound(
                "Forager returned no exact person match; no HubSpot fields were changed"
            )

        if reveal_contacts:
            lookup_person_id = person_id
            lookup_linkedin = _linkedin_identifier(_linkedin_url(details)) or identifier
            emails = await self.forager.work_emails(lookup_person_id, lookup_linkedin)
            phones = await self.forager.phone_numbers(lookup_person_id, lookup_linkedin)
            if emails:
                details["emails"] = emails
            if phones:
                details["phones"] = phones
            logger.info(
                "Forager contact lookups completed contact_id=%s emails=%d phones=%d",
                contact_id,
                len(emails),
                len(phones),
            )

        role = _current_role(details)
        org = _as_dict(role.get("organization")) or _as_dict(details.get("organization"))
        linkedin = _linkedin_url(details) or props.get("hs_linkedin_url")
        location = details.get("location")
        location_name = location.get("name") if isinstance(location, dict) else location if isinstance(location, str) else None
        city = _first(details, "city")
        state = _first(details, "state", "region")
        country = _first(details, "country")
        if isinstance(location_name, str):
            parts = [part.strip() for part in location_name.split(",") if part.strip()]
            if not city and parts:
                city = parts[0]
            if not state and len(parts) >= 2:
                state = parts[1]
            if not country and len(parts) >= 3:
                country = parts[-1]

        mapped = {
            "firstname": _first(details, "first_name", "firstname") or first,
            "lastname": _first(details, "last_name", "lastname") or last,
            "jobtitle": _first(details, "job_title", "headline") or _first(role, "role_title", "title") or props.get("jobtitle"),
            "hs_linkedin_url": linkedin,
            # Existing email/phone are preserved; automatic flow never buys lookups.
            "email": props.get("email") or _contact_value(
                details, "email", "work_email", "work_email_address"
            ),
            "phone": props.get("phone") or _contact_value(
                details, "phone", "phone_number", "mobile_phone"
            ),
            "company": _first(org, "name", "legal_name", "organization_name") or company_name,
            "city": city or props.get("city"),
            "state": state or props.get("state"),
            "country": country or props.get("country"),
        }
        mapped = {key: value for key, value in mapped.items() if value not in (None, "")}
        return await self.hubspot.update_contact(contact_id, mapped)
