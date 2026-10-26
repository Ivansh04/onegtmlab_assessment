"""
buyer_committee.py
------------------
The TAM buyer-committee title list (Decision Maker / Champion / Influencer) plus
a matcher used to decide which discovered employees are worth creating in HubSpot.

When the company pipeline auto-discovers people at a company, we only create +
enrich contacts whose job title matches this list — Forager credits are only
spent on people who are actually part of the buying committee for a data-driven
SaaS platform.

Matching is normalization-tolerant:
  * lower-cased, punctuation/commas/slashes/hyphens stripped, filler words
    ("of", "the", "and", ...) removed, a few abbreviations expanded.
  * multi-word committee titles match when they appear as a contiguous run of
    words inside the candidate title ("Senior Product Manager, Data" matches the
    committee title "Senior Product Manager" and "Product Manager").
  * single-word executive titles (CEO/CTO/CPO/COO/CDO/President/Founder) match
    when present as a standalone word, with a guard so "Vice President <X>" never
    matches the bare "President".
"""

import os
import re

# ---------------------------------------------------------------------------
# Exhaustive title list (from "TAM Buyer Committee List"). Both abbreviation and
# full-title variants are included on purpose; normalization collapses the rest.
# ---------------------------------------------------------------------------
_TITLES = [
    # --- Decision Maker: Product leadership ---
    "Chief Product Officer", "CPO",
    "VP Product", "Vice President Product", "Vice President, Product",
    "Head of Product",
    "Director of Product", "Director, Product",
    # --- Decision Maker: Technology leadership ---
    "Chief Technology Officer", "CTO",
    "VP Engineering", "Vice President Engineering", "Vice President, Engineering",
    "Head of Engineering",
    "Director of Engineering", "Director, Engineering",
    # --- Decision Maker: Platform leadership ---
    "VP Platform", "Vice President Platform", "Vice President, Platform",
    "Head of Platform",
    "Director of Platform",
    # --- Decision Maker: Data leadership (Peter, Aug 2026) ---
    # A CDO owns the data budget, so they are a buyer rather than an influencer.
    # Position matters: committee_titles() is searched in declared order, so
    # moving this up means a CDO is found before the Champion titles fill the
    # per-company contact quota.
    "Chief Data Officer", "CDO",
    # --- Decision Maker: Executive (smaller companies) ---
    "Chief Executive Officer", "CEO",
    "Founder", "Co-Founder",
    "President",
    "Chief Operating Officer", "COO",
    # --- Decision Maker: Partnerships / Vendor management (Peter, Aug 2026) ---
    # These roles own vendor selection and the commercial relationship for a
    # data purchase, so they buy rather than influence. Moved up from the
    # Influencer block for the same search-order reason as the CDO above, but
    # placed AFTER the executive titles so a procurement manager is never
    # searched ahead of a founder or CEO.
    "Head of Partnerships", "VP Partnerships", "Vice President Partnerships",
    "Director of Partnerships",
    "Strategic Partnerships Manager", "Technology Partnerships Manager",
    "Ecosystem Partnerships Manager",
    "Head of Data Partnerships", "Director of Data Partnerships",
    "Data Partnerships Manager",
    "Partner Manager", "Partner Manager, Data Partnerships",
    "Head of Business Development", "Director of Business Development",
    "Vendor Manager, Data", "Procurement Manager, Data",
    "Strategic Sourcing Manager, Data",

    # --- Champion: Core product managers ---
    "Product Manager", "Senior Product Manager", "Lead Product Manager",
    "Staff Product Manager", "Principal Product Manager", "Group Product Manager",
    # --- Champion: Specialized product managers ---
    "Product Manager, Data", "Product Manager Data",
    "Product Manager, Data Products",
    "Product Manager, Enrichment",
    "Product Manager, Integrations",
    "Product Manager, Platform", "Product Manager Platform",
    "Product Manager, Search",
    "Product Manager, Identity",
    "Product Manager, Graph",
    "Product Manager, Intelligence",
    "Senior Product Manager, Data",
    "Senior Product Manager, Platform",
    # --- Champion: Product directors / leads ---
    "Director of Product, Data", "Director of Product, Platform",
    "Head of Platform Product",
    "Head of Product, Data",
    "Product Lead, Data",

    # --- Influencer: Engineering / Platform ---
    "Engineering Manager", "Senior Engineering Manager",
    "Engineering Manager, Platform", "Engineering Manager, Data",
    "Backend Engineering Manager",
    "Technical Lead", "Technical Lead, Platform", "Technical Lead, Data",
    "Lead Engineer",
    "Principal Engineer", "Principal Engineer, Data", "Principal Engineer, Platform",
    "Staff Engineer", "Staff Engineer, Data", "Staff Engineer, Platform",
    "Solutions Architect", "Solutions Architect, Data", "Solutions Architect, Platform",
    "Platform Architect",
    # --- Influencer: Data Engineering / Data Platform ---
    "Head of Data Engineering", "Director of Data Engineering",
    "VP Data Engineering", "Vice President Data Engineering",
    "Head of Data Platform", "Director of Data Platform",
    "Data Platform Lead",
    "Head of Data Infrastructure", "Director of Data Infrastructure",
    "Data Infrastructure Lead",
    "Data Architect",
    "Lead Data Engineer", "Senior Data Engineer", "Principal Data Engineer",
    "Staff Data Engineer",
    # --- Influencer: Data leadership ---
    # (Chief Data Officer / CDO moved up to Decision Maker, Peter Aug 2026)
    "VP Data", "Vice President Data",
    "Head of Data", "Director of Data",
    "Head of Data Products", "Director of Data Products",
    "Head of Data Supply",
    # --- Influencer: AI / Machine Learning leadership ---
    "Head of AI", "VP AI", "Vice President AI", "Director of AI",
    "Director of Machine Learning", "Head of Machine Learning",
    "Head of Applied AI", "Director of Applied AI",
    "Machine Learning Engineering Manager",
    "Lead Machine Learning Engineer", "Principal Machine Learning Engineer",
    # (Partnerships / Vendor / Procurement moved up to Decision Maker, Peter Aug 2026)

    # --- Governance / Compliance (mapped, not primary outreach) ---
    "Head of Trust & Safety", "Head of Risk",
    "Director of Data Protection", "Data Governance Lead",
    "Compliance Manager", "Compliance Manager, Data Privacy",
    "Privacy Counsel", "Data Compliance Lead",
    "Chief Legal Officer", "General Counsel",

    # --- Strategy / Corporate development (occasional influencers) ---
    "VP Strategy", "Vice President Strategy",
    "Head of Strategy", "Director of Strategic Initiatives",
    "Corporate Development",
]

_STOPWORDS = {"of", "the", "for", "a", "an", "to", "at", "in", "on", "and", "&"}
_ABBREV = {
    "sr": "senior", "jr": "junior", "mgr": "manager",
    "svp": "vp", "evp": "vp", "cofounder": "founder",
}


def _normalize_words(title: str | None) -> list[str]:
    """Lower-case, strip punctuation, expand a few abbreviations, drop filler words."""
    if not title:
        return []
    text = title.lower().replace("&", " and ")
    text = text.replace(".", "")              # collapse dotted acronyms: "v.p." -> "vp"
    text = re.sub(r"[^a-z0-9 ]", " ", text)   # commas, slashes, hyphens -> space
    words: list[str] = []
    for word in text.split():
        word = _ABBREV.get(word, word)
        if word in _STOPWORDS:
            continue
        words.append(word)
    return words


# Pre-compute normalized committee titles once, split into single-word and
# multi-word buckets (single-word execs need stricter matching).
_COMMITTEE: list[tuple[str, ...]] = list(
    dict.fromkeys(tuple(w) for w in (_normalize_words(t) for t in _TITLES) if w)
)
_SINGLE_WORD = {t[0] for t in _COMMITTEE if len(t) == 1}
_MULTI_WORD = [t for t in _COMMITTEE if len(t) > 1]


def _contains_run(needle: tuple[str, ...], haystack: list[str]) -> bool:
    """True if `needle` appears as a contiguous run of words inside `haystack`."""
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    for i in range(len(haystack) - n + 1):
        if tuple(haystack[i:i + n]) == needle:
            return True
    return False


def matches_buyer_committee(title: str | None) -> bool:
    """Is this job title part of the TAM buyer committee?"""
    words = _normalize_words(title)
    if not words:
        return False
    word_set = set(words)

    # Single-word exec titles: match as a standalone word. Guard "president" so
    # "Vice President <X>" / "VP <X>" doesn't masquerade as the CEO-tier "President".
    for token in _SINGLE_WORD:
        if token == "president":
            if "president" in word_set and "vice" not in word_set and "vp" not in word_set:
                return True
        elif token in word_set:
            return True

    # Multi-word titles: contiguous-run match.
    for committee_title in _MULTI_WORD:
        if _contains_run(committee_title, words):
            return True

    return False


def committee_titles() -> list[str]:
    """The exact buyer-committee title strings, deduped, in their declared (priority)
    order. Used as server-side Forager ``role_title`` keywords for title-bucketed
    people search — each title becomes one 'find people with exactly this title' query."""
    return list(dict.fromkeys(_TITLES))


# ---------------------------------------------------------------------------
# STRICT EXACT-TITLE buyer-committee matcher.
#
# A person matches a buyer-committee title ONLY if their job title IS that title
# exactly (after the existing _normalize_words), NEVER merely "contains" the
# committee token. This fixes the fuzzy-role_title bug where, under role_title="COO"
# at KPMG, Forager returned and the OLD matches_buyer_committee accepted noise like
# "Manager - COO's Office", "Global People COO", "CFO/COO Management Consulting",
# "Senior Executive Assistant - Global COO Team", "Director - Chairman & COO Office".
#
# Reuses the existing _normalize_words (so CPO==CPO, "Vice President, Product"==
# "Vice President Product", sr->senior, svp/evp->vp, "of"/"the"/"and"/"&" dropped)
# and the existing _COMMITTEE list of normalized title tuples. Because _TITLES
# already enumerates BOTH the abbreviation and the spelled-out form of every
# committee title, exact-equality against the SET of normalized committee tuples
# covers every legitimate spelling — there is no substring / contiguous-run path,
# which is exactly what let the noise through before.
#
# DESIGN DECISION (compound exec titles): YES — a clean exact SEGMENT counts.
# Compound exec titles joined by a co-equal-role connector (&, /, +, |, ;, "and",
# "or") — e.g. "CEO & Founder", "CTO/COO", "Co-Founder & CEO", "EVP & CTO",
# "President/CEO" — match when ANY WHOLE connector-split segment is itself exactly a
# committee title. Strictness holds because the match is WHOLE-SEGMENT, not
# substring: role-noise stays glued to its own segment, so "Global People COO"
# (no connector -> the single tuple ('global','people','coo')) and "CFO/COO
# Management Consulting" (-> ('cfo',) + ('coo','management','consulting')) both fail.
# We split the RAW title, NOT the normalized words, because _normalize_words turns
# '&'/'and' into a dropped stopword and '/' into a space, which would dissolve the
# segment boundary and reintroduce the substring bug. '-' and ',' are deliberately
# NOT connectors: they separate a title from an org/department descriptor
# ("Manager - COO's Office", "Vice President, Product") rather than joining two
# co-equal roles, so they are handled only by the whole-title equality test (which
# correctly rejects the former and accepts the latter).
# ---------------------------------------------------------------------------

# Set of normalized committee title tuples — O(1) exact membership is the whole
# strictness guarantee. Built from the already-computed _COMMITTEE.
_COMMITTEE_SET: frozenset[tuple[str, ...]] = frozenset(_COMMITTEE)

# Connectors that join INDEPENDENT, co-equal role titles in a compound exec title.
# We split the RAW title on these and test each side as its own exact title.
_COMPOUND_CONNECTOR_RE = re.compile(r"\s*(?:&|/|\+|\||;|\band\b|\bor\b)\s*", re.IGNORECASE)


def matches_buyer_committee_exact(title: str | None) -> bool:
    """STRICT: is this job title EXACTLY a buyer-committee title (after normalization)?

    Accepts only when the WHOLE normalized title equals a committee title, or — for
    compound exec titles joined by a co-equal-role connector (&, /, +, |, ;, "and",
    "or") — when a WHOLE connector-split segment equals a committee title exactly. A
    segment carrying any extra word (e.g. "Global People COO") never matches the bare
    "COO". This is the strict replacement for the fuzzy ``matches_buyer_committee``:
    use it to re-confirm fuzzy ``role_title`` hits so Forager reveal credits are spent
    only on people who genuinely hold a buyer-committee title.
    """
    if not title:
        return False

    # (1) Whole-title exact equality — the clean single-role case ("COO",
    #     "Vice President, Product"). Runs FIRST, which also preserves committee
    #     titles that themselves contain a connector char, e.g. "Head of Trust &
    #     Safety" (-> ('head','trust','safety')), before the split would shatter them.
    if tuple(_normalize_words(title)) in _COMMITTEE_SET:
        return True

    # (2) Compound exec titles: split the RAW title on connectors and require a
    #     WHOLE segment to be exactly a committee title. Only attempted when a real
    #     connector is present, so single-segment noise titles ("Interim COO",
    #     "Manager - COO's Office") are never given a second chance.
    segments = _COMPOUND_CONNECTOR_RE.split(title)
    if len(segments) > 1:
        for seg in segments:
            seg_words = tuple(_normalize_words(seg))
            if seg_words and seg_words in _COMMITTEE_SET:
                return True

    return False


# ---------------------------------------------------------------------------
# SIZE-BASED DISCOVERY STRATEGY (per Shirish/Ahmed's "finding people" logic).
#
# The email anchors two rules and leaves the middle to sensible defaults:
#   * How MANY people to target scales with headcount. Shirish gave the count
#     options 3 / 5 / 7 / 10; we map them to headcount bands (all env-tunable).
#   * WHICH titles to target depends on company size:
#       - Small companies (~10-50) are CXO-led — the founder/CPO/CTO/COO IS the
#         buyer, so we lead with the C-suite.
#       - Large companies — the C-suite is unreachable, so we SKIP it and target
#         the VP / Head / Director layer (and the rest of the committee).
#       - Mid companies keep the full committee in its natural priority order.
#   * Category priority (Decision Maker -> Champion -> Influencer) and "max 1
#     contact per title" are enforced by the caller (discover_and_create_contacts).
#
# Every threshold/count is overridable via env so the client can retune without
# a code change. Unknown headcount falls back to the mid-size defaults.
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# Headcount band edges (inclusive upper bounds).
_SMALL_MAX = _env_int("DISCOVERY_SMALL_MAX", 50)      # <= this => small (CXO-led)
_MID_MAX = _env_int("DISCOVERY_MID_MAX", 200)         # <= this => mid
_LARGE_MAX = _env_int("DISCOVERY_LARGE_MAX", 1000)    # <= this => large; above => x-large

# How many contacts to target per band (Shirish's 3 / 5 / 7 / 10).
_COUNT_SMALL = _env_int("DISCOVERY_COUNT_SMALL", 3)
_COUNT_MID = _env_int("DISCOVERY_COUNT_MID", 5)
_COUNT_LARGE = _env_int("DISCOVERY_COUNT_LARGE", 7)
_COUNT_XLARGE = _env_int("DISCOVERY_COUNT_XLARGE", 10)
_COUNT_DEFAULT = _env_int("DISCOVERY_COUNT_DEFAULT", _COUNT_MID)  # unknown headcount

# C-suite titles skipped for large companies (unreachable there) and boosted to
# the front for small companies (they ARE the buyer). Matched by exact string
# against the _TITLES entries.
_CSUITE_TITLES = frozenset({
    "Chief Product Officer", "CPO",
    "Chief Technology Officer", "CTO",
    "Chief Executive Officer", "CEO",
    "Founder", "Co-Founder",
    "President",
    "Chief Operating Officer", "COO",
    "Chief Data Officer", "CDO",
    "Chief Legal Officer",
})


def _emp_to_int(employees) -> int | None:
    """Coerce a HubSpot employee-count value (str/float/int/None) to an int, or None."""
    if employees is None:
        return None
    try:
        return int(float(str(employees).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def is_csuite_title(title: str | None) -> bool:
    """True if `title` is exactly a C-suite / founder title (env: _CSUITE_TITLES)."""
    return (title or "").strip() in _CSUITE_TITLES


def target_contact_count(employees) -> int:
    """How many buyer-committee contacts to discover for a company of this size.

    Bands (env-tunable): <=50 -> 3, <=200 -> 5, <=1000 -> 7, >1000 -> 10.
    Unknown headcount falls back to the mid-size default (5)."""
    n = _emp_to_int(employees)
    if n is None:
        return _COUNT_DEFAULT
    if n <= _SMALL_MAX:
        return _COUNT_SMALL
    if n <= _MID_MAX:
        return _COUNT_MID
    if n <= _LARGE_MAX:
        return _COUNT_LARGE
    return _COUNT_XLARGE


def titles_for_company(employees) -> list[str]:
    """The buyer-committee titles to search, ORDERED by priority for this company size.

    * Small (<=50): C-suite first (the founder/CPO/CTO is the buyer), then the rest.
    * Large (>1000): C-suite dropped (unreachable) — lead with VP/Head/Director.
    * Mid / unknown: the full committee in its natural declared priority order.

    Category priority (Decision Maker -> Champion -> Influencer) is already baked into
    the declared order of committee_titles(); this only reshuffles the C-suite layer."""
    titles = committee_titles()
    n = _emp_to_int(employees)
    if n is not None and n <= _SMALL_MAX:
        execs = [t for t in titles if is_csuite_title(t)]
        rest = [t for t in titles if not is_csuite_title(t)]
        return execs + rest
    if n is not None and n > _LARGE_MAX:
        return [t for t in titles if not is_csuite_title(t)]
    return titles
