"""
enrichment.py
-------------
Business logic: orchestrates Forager lookups and HubSpot writes.

Two separate workflows, chained via HubSpot's contact-created webhook:
  Workflow 1 (company-created):  enrich_company + discover_and_create_contacts
                                 (discover skeletons only — NO reveal credits)
  Workflow 2 (contact-created):  enrich_contact (reveal email/phone — credits)

Creating a skeleton in Workflow 1 fires the contact-created webhook, which runs
Workflow 2 on it. A manually-added contact enters at Workflow 2 directly.

Capabilities:
  enrich_company             - fill a HubSpot company from Forager
  discover_and_create_contacts - discover people at a company, create skeletons
  enrich_contact             - reveal a contact's email/phone from Forager
  handle_company_webhook     - Workflow 1 pipeline
  handle_contact_webhook     - Workflow 2 pipeline
"""

import logging
import os

import buyer_committee
import deepline
import forager
import hubspot
import scoring

logger = logging.getLogger(__name__)

# Rough Forager credit cost to reveal ONE contact's email + phone. Used only for
# the log estimate below — Forager bills the real amount (~20-25 in practice).
EST_CREDITS_PER_REVEAL = 25

# Tier-1 thresholds (Shirish's rule).
_TIER1_MIN_LOGO = 7
_TIER1_MIN_EMPLOYEES = 100
_TIER1_MIN_FUNDING = 5_000_000  # USD


def _to_float(value) -> float | None:
    """Coerce a HubSpot/string value to float, or None."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _classify_tier(icp_decision, logo_score, employees, funding_amount) -> str:
    """Tier 1 = ICP-FIT AND logo >= 7 AND (employees >= 100 OR funding >= $5M).
    Everything else = Tier 2."""
    icp_fit = str(icp_decision or "").strip().upper() == "ICP"
    logo_ok = (logo_score or 0) >= _TIER1_MIN_LOGO
    size_ok = (employees or 0) >= _TIER1_MIN_EMPLOYEES or (funding_amount or 0) >= _TIER1_MIN_FUNDING
    return "Tier 1" if (icp_fit and logo_ok and size_ok) else "Tier 2"


def _linkedin_slug(url: str) -> str | None:
    """Extract the slug from a LinkedIn personal URL (.../in/<slug>)."""
    if url and "linkedin.com/in/" in url:
        return url.rstrip("/").split("/in/")[-1].split("?")[0]
    return None


# A contact's LinkedIn URL can live in several HubSpot fields depending on how it
# was entered: HubSpot's built-in "LinkedIn URL" (hs_linkedin_url) when typed into
# the standard field, our custom "LinkedIn Profile URL" (linkedin_url) when we write
# it back, or the built-in "LinkedIn Profile" (linkedin_profile). Check them all.
_CONTACT_LINKEDIN_FIELDS = ("linkedin_url", "hs_linkedin_url", "linkedin_profile")


def _contact_linkedin_slug(props: dict) -> str | None:
    for field in _CONTACT_LINKEDIN_FIELDS:
        slug = _linkedin_slug(props.get(field) or "")
        if slug:
            return slug
    return None


def _company_linkedin_slug(url: str) -> str | None:
    """Extract the company slug from a LinkedIn company URL (.../company/<slug>)."""
    if url and "linkedin.com/company/" in url:
        slug = url.rstrip("/").split("/company/")[-1].split("?")[0].split("/")[0]
        return slug or None
    return None


def enrich_company(hubspot_company_id: str, force: bool = False) -> dict:
    """Fetch a company from HubSpot, find it in Forager, write enriched fields back.

    Idempotent by default: if the company already carries a forager_org_id (we have
    enriched it before), it is skipped unless force=True. This stops a duplicate
    webhook delivery from re-running the Forager search + LLM scoring and re-spending."""
    hubspot.ensure_custom_properties()
    company = hubspot.get_company(hubspot_company_id)
    if not company:
        return {"error": f"Company {hubspot_company_id} not found in HubSpot"}

    props = company.get("properties", {})
    domain = (props.get("domain") or "").strip()
    name = (props.get("name") or "").strip()
    # Also accept a company LinkedIn URL as a resolver: when no domain is set,
    # resolve the company by its LinkedIn handle (exact). When a domain IS set,
    # the domain flow runs first and the LinkedIn handle is the FALLBACK for
    # when the domain search comes up empty (a company's real domain often
    # differs from the one on its LinkedIn page).
    li_slug = _company_linkedin_slug(props.get("linkedin_company_page") or "")

    # Skip only if the company is BOTH enriched (forager_org_id) AND scored
    # (icp_match_score) — so a company that missed scoring (e.g. a transient LLM
    # 503/429 blip) is completed on the next run instead of being skipped forever.
    # If scoring isn't configured at all, there's nothing to backfill, so skip too.
    already_enriched = bool((props.get("forager_org_id") or "").strip())
    already_scored = bool((props.get("icp_match_score") or "").strip())
    if not force and already_enriched and (already_scored or scoring.provider() is None):
        return {"hubspot_company_id": hubspot_company_id, "status": "skipped",
                "reason": "already enriched + scored (or scoring disabled); use /enrich/company to force a refresh",
                "domain": domain}

    org = forager.search_organization(
        domain=domain or None, name=name or None,
        linkedin_identifier=(li_slug if not domain else None),
    )
    if not org and domain and li_slug:
        # Domain search found nothing but a LinkedIn company page is on file —
        # retry by the LinkedIn handle (Ahmed, 2026-08-18). Exact-slug
        # validation inside search_organization rejects wrong companies.
        org = forager.search_organization(linkedin_identifier=li_slug)
    elif org and domain and li_slug:
        # Wrong-company guard: Forager sometimes misattributes a domain (e.g.
        # companies whose careers page lives on job-boards.greenhouse.io can
        # carry domain=greenhouse.io — a search for that domain returns Medium).
        # When the user supplied a LinkedIn page and the domain winner's own
        # handle disagrees with it, the user's LinkedIn URL outranks the domain
        # guess: prefer the exact slug match when one exists.
        found_slug = forager._norm_slug(forager._org_linkedin_slug(org))
        if found_slug and found_slug != forager._norm_slug(li_slug):
            better = forager.search_organization(linkedin_identifier=li_slug)
            if better:
                org = better
    if not org:
        return {"error": f"No Forager match for domain='{domain}' name='{name}' "
                         f"linkedin='{li_slug or ''}' (domain and LinkedIn lookups tried)"}

    fields = forager.parse_company_fields(org)

    # Never overwrite values the user typed in by hand: Company Name, Website,
    # Domain, and the LinkedIn company page are only filled when the HubSpot
    # field is empty. Domain matters for the LinkedIn fallback: Forager's
    # record may carry a different domain than the one entered in HubSpot —
    # the mismatch is why the domain search failed — and it must not clobber it.
    # (Everything else — revenue, headcount, location, etc. — still updates.)
    for key in ("name", "website", "domain", "linkedin_company_page"):
        if (props.get(key) or "").strip():
            fields.pop(key, None)

    # Score the company in parallel to enrichment (diagram: ICP fit + logo
    # recognizability via Claude). Skips gracefully if scoring isn't configured,
    # and never blocks the Forager write-back on a scoring failure.
    scores = scoring.score_company(fields.get("name") or name, fields)
    score_fields = scores.get("hubspot_fields", {}) or {}

    # Company Funding via Deepline (Crustdata). Only when enabled and the Funding field
    # is still empty. The client's 4 funding fields map as follows:
    #   Funding (text)              <- most recent funding DATE
    #   Funding Amount (USD) (num)  <- most recent ROUND amount
    #   Total Funding (num)         <- TOTAL funding raised
    #   Most Recent Funding Type    <- most recent round type
    # The last two are existing (Pipedrive) fields; resolve their internal names by label
    # (overridable via env). The Tier-1 size check uses the TOTAL funding. No-op when off.
    tier_funding = _to_float(props.get("funding_amount"))  # fallback if the lookup is skipped
    total_field = os.environ.get("FUNDING_TOTAL_FIELD") or hubspot.company_property_name_by_label("Total Funding")
    type_field = os.environ.get("FUNDING_TYPE_FIELD") or hubspot.company_property_name_by_label("Most Recent Funding Type")
    if deepline.is_enabled() and not (props.get("funding") or "").strip():
        try:
            fr = deepline.get_company_funding(fields.get("domain") or domain, fields.get("name") or name)
            if fr.get("last_date"):
                fields["funding"] = fr["last_date"]                 # Funding (text) <- last funding date
            if fr.get("last_amount") is not None:
                fields["funding_amount"] = fr["last_amount"]        # Funding Amount (USD) <- last round amount
            if total_field and fr.get("total") is not None:
                fields[total_field] = fr["total"]                   # Total Funding <- total raised
            if type_field and fr.get("last_round_type"):
                fields[type_field] = fr["last_round_type"]          # Most Recent Funding Type <- last round type
            if fr.get("total") is not None:
                tier_funding = fr["total"]                          # Tier-1 size check uses TOTAL funding
            if not total_field:
                logger.warning("Could not resolve the 'Total Funding' field by label; skipped it.")
            if not type_field:
                logger.warning("Could not resolve the 'Most Recent Funding Type' field by label; skipped it.")
        except Exception as exc:  # noqa: BLE001 - funding must never break enrichment
            logger.warning("Deepline funding lookup failed for %s: %s", hubspot_company_id, exc)

    # Tier 1 / Tier 2 classification (after Claude scoring). Tier 1 = ICP-FIT AND
    # logo score >= 7 AND (employees >= 100 OR total funding >= $5M); else Tier 2.
    # Output field is configurable (TIER_FIELD, default "tier") — pending the client's
    # confirmation of the exact HubSpot field.
    tier = _classify_tier(
        icp_decision=score_fields.get("icp_decision"),
        logo_score=_to_float(score_fields.get("logo_score")),
        employees=_to_float(fields.get("numberofemployees")),
        funding_amount=tier_funding,
    )
    fields[os.environ.get("TIER_FIELD", "tier")] = tier

    hubspot.update_company(hubspot_company_id, {**fields, **score_fields})
    return {
        "hubspot_company_id": hubspot_company_id,
        "forager_org_id": fields.get("forager_org_id"),
        "name": fields.get("name"),
        "domain": fields.get("domain"),
        "employees": fields.get("numberofemployees"),
        "revenue": fields.get("annualrevenue"),
        "icp": scores.get("icp"),
        "logo": scores.get("logo"),
        "tier": tier,
        "scoring_status": scores.get("status"),
        "status": "enriched",
    }


def enrich_contact(hubspot_contact_id: str) -> dict:
    """Workflow 2 — enrich ONE contact (the credit-spending step).

    Runs for every contact-created event: contacts the company workflow just
    DISCOVERED (skeletons that already carry a forager_person_id + LinkedIn URL but
    no revealed email/phone) and contacts a user adds by hand (LinkedIn URL only).

    Credit safety:
      * `forager_enriched` guard — a record we've already revealed is skipped, so a
        re-delivered webhook (or a contact.propertyChange ping) never re-spends.
      * duplicate-person guard — before any reveal we do a FREE HubSpot search; if
        the same Forager person is already enriched on another record, we skip the
        reveal entirely (no credits) instead of paying for the same person twice.
    """
    hubspot.ensure_custom_properties()
    contact = hubspot.get_contact(hubspot_contact_id)
    if not contact:
        return {"error": f"Contact {hubspot_contact_id} not found in HubSpot"}

    props = contact.get("properties", {})

    # (1) Already revealed? Nothing to do, no credits.
    if (props.get("forager_enriched") or "").strip().lower() == "true":
        return {
            "hubspot_contact_id": hubspot_contact_id,
            "status": "skipped",
            "reason": "already enriched (forager_enriched=true)",
        }

    person_id = (props.get("forager_person_id") or "").strip() or None
    slug = _contact_linkedin_slug(props)

    # (2) Resolve the person. Discovered skeletons already carry the person_id (set
    # for free at discovery), so no lookup is needed — we only need to reveal their
    # email/phone. A manual contact has only a LinkedIn URL, so resolve the full
    # profile from it first (a search — ~0 Forager credits).
    role = None
    if not person_id:
        if not slug:
            return {
                "hubspot_contact_id": hubspot_contact_id,
                "status": "skipped",
                "reason": "need a LinkedIn URL (or a Forager person id) to enrich the contact",
            }
        role = forager.find_person_by_linkedin(slug)
        if role:
            person = role.get("person") or {}
            person_id = str(person.get("id")) if person.get("id") else None
            slug = slug or (person.get("linkedin_info") or {}).get("public_identifier")

    # (3) Duplicate-person guard (FREE HubSpot search — no Forager credits). If this
    # exact person is already enriched on a different record, don't pay again.
    if person_id:
        dup = hubspot.find_enriched_contact_by_person_id(person_id, exclude_id=hubspot_contact_id)
        if dup:
            hubspot.update_contact(hubspot_contact_id, {"forager_enriched": "true"})
            return {
                "hubspot_contact_id": hubspot_contact_id,
                "status": "skipped",
                "reason": f"duplicate of already-enriched contact {dup.get('id')}; no credits spent",
                "duplicate_of": dup.get("id"),
            }

    # (4) Reveal email + phone (THIS spends Forager credits). Work and personal emails
    # are pulled separately so we can route work -> standard `email`, personal -> `migrated_emails_home`.
    personal_emails, work_emails = forager.get_person_emails_split(person_id=person_id, linkedin_identifier=slug)
    phones = forager.get_person_phones(person_id=person_id, linkedin_identifier=slug)

    # (5) Build the write. A manual contact gets its full profile from `role`; a
    # discovered skeleton already has its profile, so we only add the revealed
    # contact details. Either way, stamp forager_enriched so we never re-spend.
    if role:
        fields = forager.parse_person_fields(role, personal_emails, work_emails, phones)
    else:
        fields = {}
        union = list(dict.fromkeys(work_emails + personal_emails))
        # WORK email is NOT taken from Forager anymore (client: rely on Deepline only for
        # work email). The built-in `email` field is left for Workflow 3 / Deepline.
        if personal_emails:                   # PERSONAL email -> "Email (home)"
            fields["migrated_emails_home"] = personal_emails[0]
        if union:
            fields["all_emails"] = ", ".join(union)
        if phones:
            fields["phone"] = phones[0]
            fields["all_phones"] = ", ".join(phones)
    # When Forager itself supplied the phone (Workflow 2), stamp the provider as "Forager"
    # so HubSpot shows where the number came from. (Deepline's phone step is skipped when a
    # phone already exists, so it would otherwise leave this blank.) Applies to both the
    # manual-contact (role) and discovered-skeleton paths.
    if phones:
        fields["phone_enrichment_provider"] = "Forager"
    fields["forager_enriched"] = "true"
    if person_id and not (props.get("forager_person_id") or "").strip():
        fields["forager_person_id"] = person_id

    hubspot.update_contact(hubspot_contact_id, fields)
    logger.info(
        "Enriched contact %s (%s): work_email=%d personal_email=%d phone=%d, est ~%d Forager credits",
        hubspot_contact_id, fields.get("company") or props.get("company") or "?",
        len(work_emails), len(personal_emails), len(phones), EST_CREDITS_PER_REVEAL,
    )
    return {
        "hubspot_contact_id": hubspot_contact_id,
        "status": "enriched",
        "matched_by": "forager_person_id" if not role else "linkedin",
        "email": fields.get("email"),
        "migrated_emails_home": fields.get("migrated_emails_home"),
        "phone": fields.get("phone"),
        "title": fields.get("jobtitle") or props.get("jobtitle"),
        "est_forager_credits": EST_CREDITS_PER_REVEAL,
    }


# Forager's regular person_role_search windows results at ~500 per query (10 rows/page),
# so a single title can be paged at most this far before it runs dry.
_MAX_PAGES_PER_TITLE = 50


def _match_existing_contact(fields: dict, existing_index: dict | None) -> str | None:
    """Match a to-be-created skeleton against the company's existing contacts by
    IDENTITY (LinkedIn slug, then email) — catches manually-added contacts that carry
    no forager_person_id. Exact identifiers only; deliberately NO name matching (two
    different people can share a name). Returns the existing contact id or None."""
    if not existing_index:
        return None
    slug = _linkedin_slug(fields.get("hs_linkedin_url") or "")
    if slug:
        match = existing_index.get(f"li:{slug.lower()}")
        if match:
            return match
    for email_field in ("email", "migrated_emails_home", "all_emails"):
        for addr in (fields.get(email_field) or "").split(","):
            addr = addr.strip().lower()
            if addr and "@" in addr:
                match = existing_index.get(f"em:{addr}")
                if match:
                    return match
    return None


def _create_skeleton_from_role(role: dict, hubspot_company_id: str, owner_id,
                               seen_person_ids: set,
                               existing_index: dict | None = None) -> dict | None:
    """Create (or link) a HubSpot skeleton contact from a Forager role record and
    associate it to the company. Spends NO reveal credits — creating the skeleton fires
    the contact-created webhook, which runs Workflow 2 to reveal email/phone. Returns a
    result dict, or None if this person was already created/seen this run."""
    person = role.get("person") or {}
    pid = str(person.get("id")) if person.get("id") else ""
    if pid and pid in seen_person_ids:
        return None
    if pid:
        seen_person_ids.add(pid)

    # Skeleton fields only — empty email/phone are dropped by HubSpot's clean step;
    # the contact carries forager_person_id (for dedup) but NOT forager_enriched.
    fields = forager.parse_person_fields(role, [], [], [])  # skeleton only — no email/phone
    if owner_id:
        fields["hubspot_owner_id"] = owner_id
    # Tag it as automation-created so it's distinguishable from a manually-added
    # contact (which leaves Contact Source blank). Applied only to NEW creations
    # below — an already-existing contact we merely link is never re-tagged.
    fields["contact_source"] = "TAM Automation"

    # Cross-company dedup (free HubSpot search): if this person already exists as a
    # contact, just associate them — don't create a duplicate or re-trigger a reveal.
    existing = hubspot.find_contact_by_person_id(pid) if pid else None
    if existing:
        contact_id = existing["id"]
        action = "exists"
    else:
        # Identity dedup against the company's OWN contacts: a manually-added contact
        # (no forager_person_id) is matched by LinkedIn slug or email. Link + stamp it
        # instead of creating a duplicate; Workflow 2 then enriches the existing record.
        matched_id = _match_existing_contact(fields, existing_index)
        if matched_id:
            contact_id = matched_id
            action = "linked_existing"
            if pid:  # stamp the id so every future run recognises this person instantly
                try:
                    hubspot.update_contact(contact_id, {"forager_person_id": pid})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not stamp person id on contact %s: %s", contact_id, exc)
        else:
            created = hubspot.create_contact(fields)
            contact_id = created["id"]
            action = "discovered"

    try:
        hubspot.associate_contact_to_company(contact_id, hubspot_company_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Association failed for contact %s: %s", contact_id, exc)

    return {
        "contact_id": contact_id,
        "name": f"{fields.get('firstname', '')} {fields.get('lastname', '')}".strip(),
        "title": fields.get("jobtitle"),
        "action": action,
    }


def discover_and_create_contacts(
    hubspot_company_id: str,
    company_domain: str,
    forager_org_id: str | None = None,
    job_title_filter: str | None = None,
    max_contacts: int = 10,
    employees=None,
) -> list[dict]:
    """Workflow 1's contact step — DISCOVER buyer-committee people at a company by their
    EXACT title and create them as skeleton contacts. Each skeleton fires Workflow 2
    (``enrich_contact``) to reveal email/phone, so discovery itself spends no reveal
    credits; the reveal happens once per created contact.

    Method (title-bucketed, parent-org-isolated — the new default):
      * Isolate to the MAIN company via ``organizations:[forager_org_id]`` so subsidiary
        member-firms that merely share the domain (e.g. all ~92 'kpmg.com' entities) are
        EXCLUDED. The org id comes from enrich_company; if absent it is resolved from the
        domain (~1 credit) so the manual /enrich/find-contacts path works too.
      * For each EXACT buyer-committee title, ask Forager's FREE ``/totals/`` endpoint how
        many people hold it (0 credits), then page the PAID ``person_role_search`` only on
        titles that actually have people (server-side ``role_title`` keyword).
      * Re-confirm every hit locally with ``matches_buyer_committee`` (role_title is fuzzy)
        and create a skeleton, up to ``max_contacts`` (default 10).

    Falls back to the old domain-wide pagination only when the org id cannot be resolved.
    Idempotent: people already on the company (by Forager person id) are not duplicated
    and count toward ``max_contacts``.
    """
    hubspot.ensure_custom_properties()

    # What does the company already have? Count stamped contacts (skeleton or enriched)
    # and remember their Forager person ids so we don't re-create them. Also index the
    # company's contacts by LinkedIn slug and email: a manually-added contact carries
    # no forager_person_id, so identity matching is the only way to recognise that a
    # discovered person is already in the CRM (link + enrich instead of duplicating).
    seen_person_ids: set = set()
    existing_index: dict = {}   # "li:<slug>" / "em:<email>" -> contact id
    already = 0
    for existing_contact in hubspot.get_contacts_for_company(hubspot_company_id):
        cp = existing_contact.get("properties", {})
        pid = (cp.get("forager_person_id") or "").strip()
        if pid:
            seen_person_ids.add(pid)
            already += 1
        contact_id = str(existing_contact.get("id") or "")
        if not contact_id:
            continue
        slug = _contact_linkedin_slug(cp)
        if slug:
            existing_index.setdefault(f"li:{slug.lower()}", contact_id)
        for email_field in ("email", "migrated_emails_home", "all_emails"):
            for addr in (cp.get(email_field) or "").split(","):
                addr = addr.strip().lower()
                if addr and "@" in addr:
                    existing_index.setdefault(f"em:{addr}", contact_id)

    needed = max_contacts - already
    results: list = []
    if needed <= 0:
        return [{"status": "skipped",
                 "reason": f"company already has {already} contact(s); max is {max_contacts}"}]

    # Contact Owner = Company Owner: auto-created contacts inherit the company's owner
    # exactly — including "no owner" (None -> the contact is left unassigned, matching
    # the company). This is a one-time copy at creation, not an ongoing sync.
    owner_id = hubspot.get_company_owner_id(hubspot_company_id)

    # Resolve the PARENT org id (the only thing that excludes same-domain subsidiaries).
    if not forager_org_id and company_domain:
        org = forager.search_organization(domain=company_domain)
        if not org:
            # Same LinkedIn-handle fallback as enrich_company: the company's
            # real domain may differ from its LinkedIn page's domain.
            comp = hubspot.get_company(hubspot_company_id) or {}
            li_slug = _company_linkedin_slug(
                (comp.get("properties", {}) or {}).get("linkedin_company_page") or "")
            if li_slug:
                org = forager.search_organization(linkedin_identifier=li_slug)
        forager_org_id = str((org or {}).get("id") or "") or None

    if not forager_org_id:
        logger.warning("No Forager org id for domain=%r — falling back to domain pagination", company_domain)
        return _discover_by_domain_pagination(
            hubspot_company_id, company_domain, job_title_filter,
            max_contacts, needed, seen_person_ids, owner_id, results, existing_index,
        )

    # --- Title-bucketed, parent-org-isolated discovery ---
    # Size-based title strategy: small cos are CXO-led, large cos skip the
    # (unreachable) C-suite and target the VP/Head/Director layer. Category
    # priority (Decision Maker -> Champion -> Influencer) stays intact.
    titles = buyer_committee.titles_for_company(employees)
    if job_title_filter:
        titles = [t for t in titles if job_title_filter.lower() in t.lower()]

    # Walk titles in committee PRIORITY order (decision makers -> champions ->
    # influencers, the order in committee_titles()) and INTERLEAVE the free totals
    # pre-check with the paid search, breaking the instant the cap fills. This avoids
    # the ~138-call upfront totals sweep: for a real ICP the first few senior titles
    # fill the cap, so we make only a handful of calls. (A company with few committee
    # people still falls through all 138 free totals — correct, just slower.)
    search_credits = 0
    totals_calls = 0
    titles_with_people = 0
    people_scanned = 0
    matches_found = 0
    stop_reason = "reached max_contacts (cap filled)"

    for title in titles:
        if needed <= 0:
            break
        totals_calls += 1
        if forager.role_title_totals(forager_org_id, title) <= 0:  # FREE skip of empty titles
            continue
        titles_with_people += 1
        page = 0
        created_for_title = False  # max 1 contact per title (Shirish's rule)
        while needed > 0 and not created_for_title and page < _MAX_PAGES_PER_TITLE:
            roles, _total = forager.find_contacts_by_role_title(forager_org_id, title, page=page)
            search_credits += 1  # person_role_search = 1 credit/page (totals checks are free)
            page += 1
            if not roles:
                break
            for role in roles:
                if needed <= 0:
                    break
                people_scanned += 1
                # role_title is a fuzzy phrase filter (matches any title CONTAINING the
                # phrase) — re-confirm with STRICT exact-title equality so we only create
                # people who genuinely hold a buyer-committee title.
                if not buyer_committee.matches_buyer_committee_exact(role.get("role_title") or ""):
                    continue
                res = _create_skeleton_from_role(role, hubspot_company_id, owner_id,
                                                 seen_person_ids, existing_index)
                if res is None:
                    # Already-seen / duplicate person — keep scanning THIS title for a
                    # different, not-yet-created person before moving on.
                    continue
                res["matched_title"] = title
                results.append(res)
                matches_found += 1
                needed -= 1
                created_for_title = True  # take at most one person per title
                break

    if needed > 0:
        stop_reason = "exhausted buyer-committee titles with matches (fewer than cap available)"

    created_count = sum(1 for r in results if r.get("contact_id"))
    new_count = sum(1 for r in results if r.get("action") == "discovered")
    est_reveal_credits = new_count * EST_CREDITS_PER_REVEAL
    diagnostic = {
        "method": "title_bucketed_org_isolated",
        "domain": company_domain,
        "forager_org_id": forager_org_id,
        "titles_checked": totals_calls,
        "titles_with_people": titles_with_people,
        "people_scanned": people_scanned,
        "buyer_committee_matches": matches_found,
        "contacts_created_or_linked": created_count,
        "new_contacts_to_reveal": new_count,
        "discovery_search_credits": search_credits,  # title totals were free; only paged searches cost
        "estimated_reveal_credits": est_reveal_credits,
        "max_contacts": max_contacts,
        "stop_reason": stop_reason,
    }
    logger.info(
        "Discovery(title) %s org=%s: titles_checked=%d titles_with_people=%d scanned=%d matched=%d "
        "created=%d search_credits=%d est_reveal_credits=~%d (%d new x ~%d) cap=%d stop=%r",
        company_domain, forager_org_id, totals_calls, titles_with_people, people_scanned, matches_found,
        created_count, search_credits, est_reveal_credits, new_count, EST_CREDITS_PER_REVEAL,
        max_contacts, stop_reason,
    )
    results.append({"diagnostic": diagnostic})
    return results


def _discover_by_domain_pagination(
    hubspot_company_id: str,
    company_domain: str,
    job_title_filter: str | None,
    max_contacts: int,
    needed: int,
    seen_person_ids: set,
    owner_id,
    results: list,
    existing_index: dict | None = None,
) -> list:
    """Fallback: the original domain-wide pagination (scan everyone, filter titles
    locally). Used only when the parent org id can't be resolved. Note this sweeps in
    same-domain subsidiaries and is windowed by Forager — the org-isolated title method
    above is preferred."""
    page = 0
    MAX_PAGES = 300
    people_scanned = 0
    matches_found = 0
    total_available = None
    stop_reason = "reached max_contacts (cap filled)"
    while needed > 0 and page < MAX_PAGES:
        roles, total = forager.find_contacts_at_company_with_total(company_domain, page=page)
        if total is not None:
            total_available = total
        page += 1
        if not roles:
            stop_reason = "ran out of people (Forager returned no more)"
            break
        for role in roles:
            if needed <= 0:
                break
            people_scanned += 1
            role_title = role.get("role_title") or ""
            if not buyer_committee.matches_buyer_committee_exact(role_title):
                continue
            matches_found += 1
            if job_title_filter and job_title_filter.lower() not in role_title.lower():
                continue
            res = _create_skeleton_from_role(role, hubspot_company_id, owner_id,
                                             seen_person_ids, existing_index)
            if res is None:
                continue
            results.append(res)
            needed -= 1

    if needed > 0 and page >= MAX_PAGES:
        stop_reason = f"hit page cap ({MAX_PAGES}) — more people likely remain; raise MAX_PAGES"

    created_count = sum(1 for r in results if r.get("contact_id"))
    new_count = sum(1 for r in results if r.get("action") == "discovered")
    est_reveal_credits = new_count * EST_CREDITS_PER_REVEAL
    diagnostic = {
        "method": "domain_pagination_fallback",
        "domain": company_domain,
        "forager_total_people": total_available,
        "people_scanned": people_scanned,
        "pages_scanned": page,
        "buyer_committee_matches": matches_found,
        "contacts_created_or_linked": created_count,
        "new_contacts_to_reveal": new_count,
        "discovery_credits_spent": 0,
        "estimated_reveal_credits": est_reveal_credits,
        "max_contacts": max_contacts,
        "stop_reason": stop_reason,
    }
    logger.info(
        "Discovery(domain-fallback) %s: total_people=%s scanned=%d pages=%d matched=%d "
        "created=%d est_reveal_credits=~%d cap=%d stop=%r",
        company_domain, total_available, people_scanned, page, matches_found,
        created_count, est_reveal_credits, max_contacts, stop_reason,
    )
    results.append({"diagnostic": diagnostic})
    return results


def _dedup_guard_enabled() -> bool:
    """Domain-dedup guard on by default; disable with COMPANY_DEDUP_GUARD=off."""
    return (os.environ.get("COMPANY_DEDUP_GUARD") or "on").strip().lower() not in ("off", "0", "false", "no")


def handle_company_webhook(hubspot_company_id: str, job_title_filter: str | None = None,
                           max_contacts: int = 10, force: bool = False) -> dict:
    """Workflow 1 (company-created): enrich the company, then DISCOVER buyer-committee
    contacts as skeletons. It does NOT enrich them here — creating each skeleton fires
    the contact-created webhook, which runs Workflow 2 to reveal email/phone. This is
    the chain reaction: company workflow -> discovered contacts -> contact workflow."""
    # DOMAIN-DEDUP GUARD: if a DIFFERENT company with the SAME domain is already enriched,
    # this record is a duplicate (e.g. an external Airtable/CRM sync created a second copy
    # of a company we already have). A duplicate looks 'new' — blank forager_org_id, no
    # contacts — so without this it would be fully re-enriched + re-discovered, wasting
    # ~25 credits per contact. Skip it entirely BEFORE spending anything. Free HubSpot
    # search (0 credits). Manual force=True refreshes bypass the guard on purpose.
    if not force and _dedup_guard_enabled():
        company = hubspot.get_company(hubspot_company_id)
        domain = ((company or {}).get("properties", {}).get("domain") or "").strip()
        if domain:
            dup = hubspot.find_enriched_duplicate_company(domain, exclude_id=hubspot_company_id)
            if dup:
                logger.info(
                    "Dedup guard: company %s (domain=%s) is a DUPLICATE of already-enriched "
                    "company %s — skipping enrichment + discovery (0 credits)",
                    hubspot_company_id, domain, dup.get("id"),
                )
                return {"hubspot_company_id": hubspot_company_id, "status": "skipped",
                        "reason": f"duplicate of already-enriched company {dup.get('id')} "
                                  f"(domain {domain}); skipped to avoid re-enriching",
                        "domain": domain,
                        "duplicate_of": dup.get("id")}

    company_result = enrich_company(hubspot_company_id, force=force)
    if "error" in company_result:
        return company_result

    company_domain = company_result.get("domain", "") or ""
    # The parent org id resolved during enrichment is what isolates the MAIN company
    # from its same-domain subsidiaries in title-bucketed discovery.
    forager_org_id = company_result.get("forager_org_id") or None
    # Size-based target count (Shirish's 3/5/7/10 by headcount). max_contacts stays a
    # hard upper safety cap: the effective target never exceeds the caller's cap, so an
    # explicit lower cap (e.g. testing) still wins while defaults get the size-based count.
    employees = company_result.get("employees")
    effective_max = min(max_contacts, buyer_committee.target_contact_count(employees))
    discovered_contacts = []
    if company_domain or forager_org_id:
        discovered_contacts = discover_and_create_contacts(
            hubspot_company_id=hubspot_company_id,
            company_domain=company_domain,
            forager_org_id=forager_org_id,
            job_title_filter=job_title_filter,
            max_contacts=effective_max,
            employees=employees,
        )

    return {
        "company": company_result,
        "discovered_contacts": discovered_contacts,
    }


def workflow3_deepline(hubspot_contact_id: str) -> dict:
    """Workflow 3 — Deepline work-email + phone enrichment for one contact.

    Runs AFTER Workflow 2 has finished for the contact. Additive + dormant: returns
    immediately unless DEEPLINE_API_KEY is set, so it changes nothing when Deepline
    is off. Idempotent via the `deepline_enriched` flag, and duplicate-safe via the
    same Forager-person-id check Workflow 2 uses.

    The work-email and phone waterfalls run IN PARALLEL. The phone waterfall is
    skipped when the contact already has a phone (Deepline is the fallback after
    Forager, per spec). Work email -> built-in Email field; phone -> Phone field.
    """
    if not deepline.is_enabled():
        return {"status": "skipped", "reason": "deepline disabled"}

    contact = hubspot.get_contact(hubspot_contact_id)
    if not contact:
        return {"error": f"Contact {hubspot_contact_id} not found"}
    props = contact.get("properties", {})

    # Idempotency: never re-run Deepline on a contact we've already done.
    if (props.get("deepline_enriched") or "").strip().lower() == "true":
        return {"hubspot_contact_id": hubspot_contact_id, "status": "skipped",
                "reason": "already deepline-enriched"}

    # Duplicate-person guard (free HubSpot search): if this person is already
    # Deepline-enriched on another record, don't pay to enrich them again.
    person_id = (props.get("forager_person_id") or "").strip() or None
    if person_id:
        dup = hubspot.find_deepline_contact_by_person_id(person_id, exclude_id=hubspot_contact_id)
        if dup:
            hubspot.update_contact(hubspot_contact_id, {"deepline_enriched": "true"})
            return {"hubspot_contact_id": hubspot_contact_id, "status": "skipped",
                    "reason": f"duplicate of deepline-enriched contact {dup.get('id')}"}

    first = (props.get("firstname") or "").strip()
    last = (props.get("lastname") or "").strip()
    inp = {
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}".strip(),
        "domain": forager.normalize_domain(props.get("company_domain") or ""),
        "company_name": (props.get("company") or "").strip(),
        "linkedin_url": (props.get("linkedin_url") or props.get("hs_linkedin_url") or "").strip(),
        "email": (props.get("migrated_emails_home") or "").strip(),  # personal email helps phone lookups
        "country": (props.get("country") or "").strip(),  # drives the region-specific phone waterfall
    }
    # Deepline is the FALLBACK after Forager: if Forager already put a work email in the
    # built-in `email` field (or a phone in `phone`), skip that waterfall ENTIRELY — don't
    # even look it up (saves provider credits). Per Shirish's call.
    has_work_email = bool((props.get("email") or "").strip())
    has_phone = bool((props.get("phone") or "").strip())

    email_res, phone_res = {}, {}
    if deepline.sequential_reuse_enabled():
        # SEQUENTIAL + REUSE (credit optimisation, per Shirish's call): run the email
        # waterfall first, THEN the phone waterfall, sharing a per-contact tool-response
        # cache. A tool called in both with the SAME payload (e.g. PDL, whose one
        # flat-priced enrich carries email AND phone) is charged ONCE — the phone
        # waterfall reuses the cached response when its region-ordered sequence reaches
        # that tool. Field-gated tools (ContactOut/Prospeo/Lusha) send different
        # payloads per waterfall, so they still make their own calls (no eager phone
        # reveal). Waterfall ORDER and validation are unchanged.
        deepline.begin_reuse_cache()
        try:
            if not has_work_email:
                email_res = deepline.run_email_waterfall(inp) or {}
            if not has_phone:
                phone_res = deepline.run_phone_waterfall(inp) or {}
        finally:
            saved = deepline.end_reuse_cache()
            if saved:
                logger.info("Deepline reuse cache saved %d duplicate tool call(s) for contact %s",
                            saved, hubspot_contact_id)
    else:
        # Default: run the (needed) waterfalls in parallel (each is independent, I/O-bound).
        import concurrent.futures as _f
        with _f.ThreadPoolExecutor(max_workers=2) as pool:
            email_future = pool.submit(deepline.run_email_waterfall, inp) if not has_work_email else None
            phone_future = pool.submit(deepline.run_phone_waterfall, inp) if not has_phone else None
            email_res = (email_future.result() or {}) if email_future else {}
            phone_res = (phone_future.result() or {}) if phone_future else {}

    update: dict = {"deepline_enriched": "true"}
    if email_res.get("value"):
        update["email"] = email_res["value"]  # work email -> built-in Email field
        # Which provider found the work email (hunter -> "Hunter", pdl -> "PDL", ...).
        # Only set on a Deepline work-email resolve (this block); never for Forager.
        winner = email_res.get("winner")
        if winner:
            update["email_enrichment_provider"] = deepline.provider_display(winner)
        if (email_res.get("meta") or {}).get("smtp_provider"):
            update["email_smtp_provider"] = email_res["meta"]["smtp_provider"]
    # Claude Name+Domain verdict (VALID for a written email, or the rejection reason when
    # no email passed) -> "Email Verification" so reps see WHY an email is present/blank.
    if email_res.get("email_verification"):
        update["email_verification"] = email_res["email_verification"]
    if phone_res.get("value"):
        meta = phone_res.get("meta") or {}
        phone_val = phone_res["value"]
        update["phone"] = phone_val
        # Which provider resolved it (pdl -> "PDL", upcell -> "Upcell", ...).
        winner = phone_res.get("winner")
        if winner:
            update["phone_enrichment_provider"] = deepline.provider_display(winner)
        # Country + calling code, derived from the NUMBER itself — works for ALL regions
        # (not just NAMER), which is why these were blank before.
        country, calling_code = deepline.phone_geo(phone_val)
        if country:
            update["phone_country"] = country
        if calling_code:
            update["phone_calling_code"] = calling_code
        # Trestle name-match + validity — only present when validation actually ran
        # (NAMER); left blank for other regions where Trestle isn't called.
        if meta.get("validated"):
            nm = meta.get("name_match")
            update["phone_name_match"] = "true" if nm is True else ("false" if nm is False else "unknown")
            iv = meta.get("is_valid")
            if iv is not None:
                update["phone_validity"] = "valid" if iv in (True, "true", "True", "valid") else "invalid"
        if meta.get("activity_score") is not None:
            update["phone_activity_score"] = meta["activity_score"]
        if meta.get("line_type"):
            update["phone_line_type"] = meta["line_type"]

    hubspot.update_contact(hubspot_contact_id, update)
    return {
        "hubspot_contact_id": hubspot_contact_id,
        "status": "deepline_enriched",
        "work_email": email_res.get("value"),
        "email_provider": (email_res.get("meta") or {}).get("smtp_provider"),
        "email_skipped_existing": has_work_email,
        "phone": phone_res.get("value"),
        "phone_skipped_existing": has_phone,
    }


def handle_contact_webhook(hubspot_contact_id: str) -> dict:
    """Workflow 2 (contact-created): enrich the contact (reveal email/phone), then —
    once that has finished — trigger Workflow 3 (Deepline). Workflow 3 is dormant
    unless DEEPLINE_API_KEY is set, so this is identical to before when Deepline is off."""
    result = enrich_contact(hubspot_contact_id)
    if deepline.is_enabled():
        try:
            result = {"workflow2": result, "workflow3": workflow3_deepline(hubspot_contact_id)}
        except Exception as exc:  # noqa: BLE001 - WF3 must never break WF2 / the webhook
            logger.exception("Workflow 3 (Deepline) failed for %s", hubspot_contact_id)
            result = {"workflow2": result, "workflow3": {"error": str(exc)}}
    return result
