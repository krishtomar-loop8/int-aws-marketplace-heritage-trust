"""Orchestration for the monthly AML name export (D6).

Produces **one row per party** (person/entity) across every screening-eligible
matter — the structure Aphrodite requires for sanctions screening. Each
party carries its matter number, the internal source record id (kept separate so
a database object id never masquerades as a matter number), matter type, role,
full legal name, date of birth, and provenance.

Sources:
- **Estateably** is the primary party source: deceased/settlor (client intake),
  beneficiaries, executors/trustees, substitute decision makers (attorney /
  committee, POA matters), and role-tagged contacts (settlor / protector /
  committee). It exposes DOB and person/entity type on these records.
- **Clio** contributes the human-readable ``display_number`` (vs its numeric
  ``id``) and, for matters that exist only in Clio, the client + related
  contacts (with roles from the relationship description) as parties.

Data quality — exclusions, normalization, and the exception report — lives in
:mod:`quality`. This module never calls Bedrock and never logs PII (counts only).
Rows may contain DOBs; SIN is **not** exported at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from backend.lambdas.aml_export import quality
from services.http import HttpError
from services.logging import get_logger

logger = get_logger(__name__)

# HTTP statuses that mean "this case's data can't be retrieved right now" (a
# per-case Estateably billing hold / not-found), as opposed to a real error.
_UNAVAILABLE_STATUSES = {402, 404}

_MATTER_TYPE_LABEL = {"estate": "Estate", "trust": "Trust", "poa": "POA"}

# Human matter-number tokens in Aphrodite's naming: 6-digit (230009), sub-matter
# (230010-01), internal (2100-11 / 999999).
_MATTER_NUM_RE = re.compile(r"\b(\d{6}(?:-\d{2})?|2100-\d{2}|999999)\b")

CLIO_MATTER_FIELDS = "id,display_number,description,status,client{name}"
# Clio v4 rejects a nested ``relationships{...}`` field on a matter ("InvalidFields"),
# so related contacts are fetched from the separate GET /relationships endpoint (each
# relationship links a contact to the matter with a role description).
CLIO_PARTY_FIELDS = (
    "id,display_number,description,status,"
    "client{id,name,type,first_name,middle_name,last_name,date_of_birth}"
)
CLIO_RELATIONSHIP_FIELDS = (
    "id,description,contact{id,name,type,first_name,middle_name,last_name,date_of_birth}"
)
# A Clio contact's address can't ride the matter->client expansion (Clio 400s on the
# nested field), so jurisdiction for a Clio-sourced party is fetched per-contact via
# GET /contacts/{id}. Clio uses ``province`` (not ``state``); ``country`` may be absent.
CLIO_ADDRESS_FIELDS = "id,primary_address{province,country},addresses{province,country}"


# -- small accessors ---------------------------------------------------------


def _attrs(resource: dict[str, Any] | None) -> dict[str, Any]:
    return (resource or {}).get("attributes") or {}


def _first(attrs: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def _full_name(attrs: dict[str, Any]) -> str | None:
    first = attrs.get("firstName") or attrs.get("first_name")
    middle = attrs.get("middleName") or attrs.get("middle_name")
    last = attrs.get("lastName") or attrs.get("last_name")
    if first or last:
        return " ".join(part for part in (first, middle, last) if part)
    return _first(attrs, "name", "willname", "title")


def _description(estate: dict[str, Any]) -> str | None:
    return _first(_attrs(estate), "title", "name", "description")


# -- matter type / number ----------------------------------------------------


def classify_case_from_text(text: str) -> str:
    haystack = text.lower()
    if "trust" in haystack:
        return "trust"
    if (
        "committeeship" in haystack
        or "committee of" in haystack
        or "poa" in haystack
        or "power of attorney" in haystack
    ):
        return "poa"
    return "estate"


def classify_case(estate: dict[str, Any]) -> str:
    attrs = _attrs(estate)
    haystack = " ".join(
        str(_first(attrs, key) or "") for key in ("caseType", "matterType", "type", "title", "name")
    )
    return classify_case_from_text(haystack)


def _matter_number_from_text(*texts: str | None) -> str | None:
    """Extract the human matter number embedded in a case title/description.

    Aphrodite's titles read e.g. "Aziz-e-dean Tejpar - 230009" or "230010-01";
    the number is the trailing token, so we take the *last* match.
    """
    for text in texts:
        matches = _MATTER_NUM_RE.findall(text or "")
        if matches:
            return matches[-1]
    return None


def _plausible_matter_number(value: str) -> bool:
    """A real matter number contains a digit and is short — not a case title.

    Rejects free text that lands in Estateably's number field (e.g. the title
    "Estate of Nicola DeBenedetto"), which must never become a matter number.
    """
    return bool(re.search(r"\d", value)) and len(value) <= 20


def resolve_matter_number(
    attrs: dict[str, Any], clio_matter: dict[str, Any] | None, *texts: str | None
) -> tuple[str | None, str]:
    """Return ``(matter_number, provenance)`` — never the internal object id.

    Order: a structured Estateably number → a matched Clio ``display_number``.
    We do NOT infer a number from the title (Aphrodite decision B): when neither
    source has one, the field is left blank and flagged for source correction.
    """
    del texts  # title inference intentionally disabled (decision B)
    structured = _first(
        attrs, "estateNumber", "matterNumber", "displayNumber", "number", "fileNumber"
    )
    structured = quality.normalize_text(structured) if structured else None
    if (
        structured
        and not quality.looks_like_object_id(structured)
        and _plausible_matter_number(structured)
    ):
        return structured, "structured"
    if clio_matter and clio_matter.get("display_number"):
        return str(clio_matter["display_number"]), "clio"
    return None, "none"


# -- party-type mappers ------------------------------------------------------


def _beneficiary_party_type(attrs: dict[str, Any]) -> str:
    t = str(attrs.get("typeOfBeneficiary") or "").strip().lower()
    return {"legalperson": "Corporation", "trust": "Trust"}.get(
        t, "Individual" if t in ("familymember", "nonfamilymember") else ""
    )


def _executor_party_type(attrs: dict[str, Any]) -> str:
    t = str(attrs.get("executorType") or "").strip().lower()
    return {"corporate": "Corporation", "individual": "Individual"}.get(t, "")


def _clio_party_type(contact: dict[str, Any]) -> str:
    t = str(contact.get("type") or "").strip().lower()
    return {"company": "Corporation", "person": "Individual"}.get(t, "")


# Jurisdiction is read from Estateably's native (non-custom) fields — a party's own
# address first, then the client-intake province/country for the principal, then the
# matter's location as a fallback (Aphrodite has no custom fields; Nicole 2026-08-05).
# Values are normalized because the source mixes "BC"/"British Columbia" and "CA"/"Canada".
_CA_PROVINCES = {
    "bc": "British Columbia",
    "ab": "Alberta",
    "sk": "Saskatchewan",
    "mb": "Manitoba",
    "on": "Ontario",
    "qc": "Quebec",
    "nb": "New Brunswick",
    "ns": "Nova Scotia",
    "pe": "Prince Edward Island",
    "nl": "Newfoundland and Labrador",
    "yt": "Yukon",
    "nt": "Northwest Territories",
    "nu": "Nunavut",
}
_COUNTRY_ALIASES = {
    "ca": "Canada",
    "can": "Canada",
    "us": "United States",
    "usa": "United States",
    "uk": "United Kingdom",
}


def _titlecase_if_uniform(value: str) -> str:
    """Title-case a value only if it is all-lower or all-upper (source-entry noise);
    leave already-mixed-case values (e.g. 'Nova Scotia', 'Hyogo') untouched."""
    return value.title() if value and (value.islower() or value.isupper()) else value


def _norm_region(value: Any) -> str:
    v = quality.normalize_text(value)
    if not v:
        return ""
    return _CA_PROVINCES.get(v.lower(), _titlecase_if_uniform(v))


def _norm_country(value: Any) -> str:
    v = quality.normalize_text(value)
    if not v:
        return ""
    return _COUNTRY_ALIASES.get(v.lower(), _titlecase_if_uniform(v))


def _format_jurisdiction(region: Any, country: Any) -> str:
    parts = [_norm_region(region), _norm_country(country)]
    return ", ".join(p for p in parts if p)


def _jurisdiction(attrs: dict[str, Any]) -> str:
    """Matter-level jurisdiction from the estate's location (fallback for parties)."""
    loc = attrs.get("location") or {}
    region = loc.get("province") or attrs.get("province") or loc.get("state")
    return _format_jurisdiction(region, loc.get("country") or attrs.get("country"))


def _address_jurisdiction(address: Any) -> str:
    """Jurisdiction from a single native address object ({state/province, country})."""
    if not isinstance(address, dict):
        return ""
    return _format_jurisdiction(
        address.get("state") or address.get("province"), address.get("country")
    )


def _addresses_jurisdiction(addresses: Any) -> str:
    """Jurisdiction from a native addresses[] list — prefer the tagged primary/
    tax-residency address, else the first entry."""
    if not isinstance(addresses, list):
        return ""
    dicts = [a for a in addresses if isinstance(a, dict)]
    primary = next((a for a in dicts if "primary" in str(a.get("addressType") or "").lower()), None)
    return _address_jurisdiction(primary or (dicts[0] if dicts else None))


def _intake_jurisdiction(intake_attrs: dict[str, Any]) -> str:
    """Jurisdiction for the principal, from the client intake's native province/country."""
    return _format_jurisdiction(
        intake_attrs.get("province") or intake_attrs.get("state"), intake_attrs.get("country")
    )


# -- party assembly ----------------------------------------------------------


def _party(
    ctx: dict[str, Any],
    *,
    role: str,
    party_type: str,
    name: Any,
    dob: Any,
    source: str,
    jurisdiction: str | None = None,
    alias: Any = None,
) -> dict[str, Any]:
    clean, issues = quality._clean_name(name)
    dob_norm = quality.normalize_dob(dob)
    source_unavailable = ctx.get("source_unavailable", False)

    # Alias / AKA: Estateably has no alias field, but exposes "name as it appears in
    # the will" (willname); surface it as a secondary screening name when it differs
    # from the legal name (Nicole 2026-08-05). Genuine AKAs stay manual per decision G.
    alias_clean = quality.normalize_text(alias)
    if alias_clean and alias_clean.lower() == clean.lower():
        alias_clean = ""

    # Entity vs natural person: a trust/estate/company name must never be stamped
    # as an Individual or carry a person's DOB (Aphrodite item 1). Any remaining
    # blank type defaults to Individual (flagged) so it isn't silently exempt from
    # the missing-DOB check (item 8a).
    is_entity = bool(clean and quality.looks_like_entity(clean))
    ptype = party_type or ""
    if is_entity:
        ptype = quality.entity_party_type(clean)
        dob_norm = None
    elif not ptype and not source_unavailable:
        ptype = "Individual"
        issues.append("Party type unverified (defaulted to Individual)")

    canonical = quality.map_role(role)
    raw_role = quality.normalize_text(role)

    # Role routing (Aphrodite decision D as amended by item 3):
    #  • Aphrodite's own company              → Excluded Parties
    #  • a clearly non-principal role        → Excluded Parties (advisor/service/social) —
    #                                          takes precedence over a taxonomy term the
    #                                          free text merely name-drops, e.g. "Counsel
    #                                          for the Beneficiary" is counsel, not a
    #                                          beneficiary (Nicole 2026-07-28)
    #  • a canonical taxonomy role           → screened (incl. corporate trustees ≠ HTC)
    #  • an entity with no screenable role   → Excluded Parties (a trust name is not a
    #                                          screenable person — item 1)
    #  • anything else (generic Client, POA Principal, ambiguous person, blank)
    #                                        → screened as "Role to be Confirmed" (a
    #                                          principal is never dropped for a thin role)
    non_principal = quality.is_non_principal_role(role)
    role_out = "" if non_principal else (canonical or "")
    excluded_reason: str | None = None
    if not source_unavailable:
        excluded_reason = quality.is_excluded_party_name(clean)
        if excluded_reason is None and non_principal:
            excluded_reason = f"Non-principal role — not screened ({raw_role or '—'})"
        elif excluded_reason is None and canonical is None:
            if is_entity:
                excluded_reason = (
                    f"Entity (trust/company) — not a screenable person ({raw_role or '—'})"
                )
            else:
                role_out = "Role to be Confirmed"
                issues.append("Role to be confirmed — assign a standard role at source")

    return {
        "matter_number": ctx["matter_number"],
        "source_record_id": ctx["source_record_id"],
        "matter_type": ctx["matter_type"],
        "active": ctx["active"],
        "screening_required": ctx["screening_required"],
        "party_type": ptype,
        "party_role": role_out,
        "source_role": raw_role,
        "full_legal_name": clean,
        "alias": alias_clean,
        "date_of_birth": dob_norm,
        "jurisdiction": jurisdiction or ctx.get("jurisdiction") or "",
        "source": source,
        "exception_status": "",
        # helper keys (ignored by the workbook writer, used downstream)
        "description": ctx.get("description") or "",
        "matter_number_provenance": ctx.get("matter_number_provenance"),
        "source_unavailable": source_unavailable,
        "excluded_reason": excluded_reason,
        "cross_system": ctx.get("cross_system"),
        "not_on_designation_list": ctx.get("not_on_designation_list", False),
        "issues": issues,
    }


def _dedup_parties(parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (name, role) within a matter; a nameless row is never merged."""
    seen: set[tuple[str, str]] = set()
    out = []
    for p in parties:
        name = (p.get("full_legal_name") or "").lower()
        if not name:
            out.append(p)
            continue
        key = (name, p.get("party_role") or p.get("source_role") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _estateably_parties(
    estateably: Any, estate_id: str, matter_type: str, ctx: dict[str, Any]
) -> list[dict[str, Any]]:
    parties: list[dict[str, Any]] = []

    for b in estateably.list_beneficiaries(estate_id):
        a = _attrs(b)
        parties.append(
            _party(
                ctx,
                role="Beneficiary",
                party_type=_beneficiary_party_type(a),
                name=_full_name(a),
                dob=_first(a, "birth"),
                source="Estateably: beneficiaries",
                jurisdiction=_address_jurisdiction(a.get("address")),
                alias=_first(a, "willname"),
            )
        )

    trustee_role = "Trustee" if matter_type == "Trust" else "Executor"
    for e in estateably.list_executors(estate_id):
        a = _attrs(e)
        parties.append(
            _party(
                ctx,
                role=trustee_role,
                party_type=_executor_party_type(a),
                name=_full_name(a),
                dob=_first(a, "birth"),
                source="Estateably: executors",
                jurisdiction=_address_jurisdiction(a.get("address")),
                alias=_first(a, "willname"),
            )
        )

    if matter_type == "POA":
        title = ctx.get("description") or ""
        sdm_role = "Committee" if "committee" in title.lower() else "Attorney"
        for s in estateably.list_substitute_decision_makers(estate_id):
            a = _attrs(s)
            parties.append(
                _party(
                    ctx,
                    role=sdm_role,
                    party_type="Individual",
                    name=_full_name(a),
                    dob=_first(a, "dateOfBirth", "birth"),
                    source="Estateably: substitute decision makers",
                    jurisdiction=_addresses_jurisdiction(a.get("addresses")),
                    alias=_first(a, "willname"),
                )
            )

    for c in estateably.list_contacts(estate_id):
        a = _attrs(c)
        # Pass the raw category (original case) as the source role; _party
        # canonicalizes it and routes non-principals (advisors, social contacts)
        # to Excluded Parties while preserving the original text in Source Role.
        parties.append(
            _party(
                ctx,
                role=_first(a, "category", "role") or "",
                party_type="",
                name=_full_name(a),
                dob=_first(a, "birth", "dateOfBirth"),
                source="Estateably: contacts",
                jurisdiction=_addresses_jurisdiction(a.get("addresses")),
                alias=_first(a, "willname"),
            )
        )

    return parties


def _matter_gate(
    designations: Any | None,
    *,
    number: str | None,
    description: str | None,
    title: str | None,
    status: str | None,
) -> tuple[str | None, bool]:
    """Decide whether a matter is excluded, honoring Aphrodite's designation list.

    Returns ``(exclude_reason, not_on_list_flag)``. The HT list takes precedence:
    a listed *exclude* wins; a listed *include* keeps the matter (overriding a
    backstop rule); otherwise the rule-based backstop applies. A matter kept by
    default that isn't on the list (when a list is loaded) sets ``not_on_list`` so
    it can be flagged rather than silently included.
    """
    if designations is not None:
        decision, reason = designations.matter_decision(number)
        if decision == "exclude":
            return reason or "Excluded by Aphrodite designation list", False
        if decision == "include":
            return None, False
    reason = quality.exclusion_reason(
        matter_number=number, description=description, title=title, status=status
    )
    if reason:
        return reason, False
    not_on_list = bool(
        designations is not None
        and getattr(designations, "has_matters", False)
        and not designations.covers_matter(number)
    )
    return None, not_on_list


def build_estate_parties(
    estateably: Any,
    estate: dict[str, Any],
    *,
    clio_matters: list[dict[str, Any]] | None = None,
    designations: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return ``(parties, excluded_entry)`` for one Estateably case.

    ``excluded_entry`` is set (and ``parties`` empty) when the matter is filtered
    out. A per-case Estateably billing hold yields a single "source unavailable"
    row rather than silently dropping the matter.
    """
    estate_id = str(estate.get("id"))
    attrs = _attrs(estate)
    title = _description(estate)
    matter_type = _MATTER_TYPE_LABEL[classify_case(estate)]

    try:
        intake = _attrs(estateably.get_client_intake(estate_id))
    except HttpError as exc:
        if exc.status in _UNAVAILABLE_STATUSES:
            return _unavailable_row(estate_id, title, matter_type), None
        raise

    principal_name = _full_name(intake)
    clio_matter = _find_clio_matter(clio_matters or [], principal_name or title, matter_type)
    number, provenance = resolve_matter_number(attrs, clio_matter, title)

    reason, not_on_list = _matter_gate(
        designations, number=number, description=title, title=title, status=attrs.get("status")
    )
    if reason:
        return [], {
            "matter_number": number,
            "source_record_id": estate_id,
            "matter_type": matter_type,
            "description": title,
            "reason": reason,
        }

    ctx = {
        "matter_number": number,
        "source_record_id": estate_id,
        "matter_type": matter_type,
        "active": "Active",
        "screening_required": "Yes",
        "description": title,
        "matter_number_provenance": provenance,
        "jurisdiction": _jurisdiction(attrs),
        "cross_system": "both" if clio_matter else "estateably_only",
        "not_on_designation_list": not_on_list,
    }

    parties: list[dict[str, Any]] = []
    if principal_name:
        principal_role = {"Estate": "Deceased", "Trust": "Settlor", "POA": "Principal"}[matter_type]
        parties.append(
            _party(
                ctx,
                role=principal_role,
                party_type="Individual",
                name=principal_name,
                dob=_first(intake, "dateOfBirth", "birthDate"),
                source="Estateably: client intake",
                jurisdiction=_intake_jurisdiction(intake),
            )
        )
    parties += _estateably_parties(estateably, estate_id, matter_type, ctx)
    return _dedup_parties(parties), None


def _unavailable_row(estate_id: str, title: str | None, matter_type: str) -> list[dict[str, Any]]:
    ctx = {
        "matter_number": None,
        "source_record_id": estate_id,
        "matter_type": matter_type,
        "active": "Unknown",
        "screening_required": "Review",
        "description": title,
        "matter_number_provenance": "none",
        "source_unavailable": True,
    }
    return [
        _party(
            ctx, role="—", party_type="", name=title, dob=None, source="Estateably (unavailable)"
        )
    ]


# -- Clio -------------------------------------------------------------------


def _clio_matters(clio: Any | None) -> list[dict[str, Any]]:
    if clio is None:
        return []
    matters = clio.search_matters("", fields=CLIO_MATTER_FIELDS)
    return [m for m in matters if not quality.is_inactive_status(m.get("status"))]


def _clio_client_name(matter: dict[str, Any]) -> str | None:
    return (matter.get("client") or {}).get("name")


def _normalize(text: str | None) -> str:
    return " ".join(str(text or "").lower().split())


def _names_match(a: str | None, b: str | None) -> bool:
    tokens_a, tokens_b = set(_normalize(a).split()), set(_normalize(b).split())
    if not tokens_a or not tokens_b:
        return False
    smaller, larger = (
        (tokens_a, tokens_b) if len(tokens_a) <= len(tokens_b) else (tokens_b, tokens_a)
    )
    return len(smaller) >= 2 and smaller.issubset(larger)


def _matter_key(number: Any) -> str:
    """Normalized join key for a matter number, so a Clio ``display_number`` like
    "250032-Robertson" matches an Estateably number parsed as "250032"."""
    if not number:
        return ""
    return _matter_number_from_text(str(number)) or _normalize(str(number))


def _matter_number_key(number: Any) -> str:
    """A stable, PII-free key for a matter number (digits only) — no embedded
    surnames, so it is safe to persist in the manifest for the drop check."""
    if not number:
        return ""
    parsed = _matter_number_from_text(str(number))
    if parsed:
        return parsed
    return re.sub(r"[^0-9-]", "", str(number)).strip("-")


def _find_clio_matter(
    clio_matters: list[dict[str, Any]], name: str | None, matter_type: str | None = None
) -> dict[str, Any] | None:
    """Find the Clio matter for this client. When a client has several matters
    (e.g. an Estate and a POA), prefer one of the SAME type so an Estate doesn't
    adopt its POA's number — the Fairfield/Stratford/Nagle case."""
    matches = [m for m in clio_matters if _names_match(_clio_client_name(m), name)]
    if not matches:
        return None
    if matter_type:
        for m in matches:
            mt = _MATTER_TYPE_LABEL[
                classify_case_from_text(
                    f"{m.get('description') or ''} {_clio_client_name(m) or ''}"
                )
            ]
            if mt == matter_type:
                return m
    return matches[0]


def _clio_contact_party(
    ctx: dict[str, Any],
    contact: dict[str, Any],
    role: str,
    *,
    jurisdiction: str = "",
) -> dict[str, Any]:
    name = contact.get("name") or " ".join(
        p
        for p in (contact.get("first_name"), contact.get("middle_name"), contact.get("last_name"))
        if p
    )
    return _party(
        ctx,
        role=role,
        party_type=_clio_party_type(contact),
        name=name,
        dob=contact.get("date_of_birth"),
        source="Clio: matter contacts",
        jurisdiction=jurisdiction,
    )


def _clio_contact_jurisdiction(clio: Any, contact: dict[str, Any], cache: dict[str, str]) -> str:
    """Jurisdiction (province + country) for a Clio contact, from its address.

    A Clio contact's address can't be reached through the matter->client expansion
    (Clio 400s the nested field), so it is fetched per-contact via GET /contacts/{id}
    and cached by id within the run (contacts recur across matters). A missing id or a
    fetch failure degrades to "" (logged PII-free) rather than dropping the party."""
    cid = contact.get("id")
    if cid is None:
        return ""
    key = str(cid)
    if key in cache:
        return cache[key]
    jurisdiction = ""
    try:
        detail = clio.get_contact(cid, fields=CLIO_ADDRESS_FIELDS)
        addr = detail.get("primary_address")
        if not addr:
            addresses = detail.get("addresses") or []
            addr = addresses[0] if addresses else None
        jurisdiction = _address_jurisdiction(addr)
    except Exception:
        logger.exception(
            "failed to fetch Clio contact address",
            extra={"extra_fields": {"record_id": key}},
        )
    cache[key] = jurisdiction
    return jurisdiction


def build_clio_only_parties(
    clio: Any,
    matter: dict[str, Any],
    *,
    designations: Any | None = None,
    contact_cache: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Parties for a matter that exists only in Clio: client + related contacts."""
    matter_id = matter.get("id")
    detail = (
        clio.get_matter(matter_id, fields=CLIO_PARTY_FIELDS) if matter_id is not None else matter
    )
    number = detail.get("display_number") or matter.get("display_number")
    description = detail.get("description")
    matter_type = _MATTER_TYPE_LABEL[
        classify_case_from_text(f"{description or ''} {_clio_client_name(detail) or ''}")
    ]

    reason, not_on_list = _matter_gate(
        designations,
        number=number,
        description=description,
        title=description,
        status=detail.get("status"),
    )
    if reason:
        return [], {
            "matter_number": number,
            "source_record_id": str(matter_id),
            "matter_type": matter_type,
            "description": description,
            "reason": reason,
        }

    ctx = {
        "matter_number": number,
        "source_record_id": str(matter_id),
        "matter_type": matter_type,
        "active": "Active",
        "screening_required": "Yes",
        "description": description,
        "matter_number_provenance": "clio" if number else "none",
        "cross_system": "clio_only",
        "not_on_designation_list": not_on_list,
    }
    cache = contact_cache if contact_cache is not None else {}
    parties: list[dict[str, Any]] = []
    client = detail.get("client") or {}
    if client.get("name") or client.get("last_name"):
        jur = _clio_contact_jurisdiction(clio, client, cache)
        parties.append(_clio_contact_party(ctx, client, "Client", jurisdiction=jur))
    for rel in _clio_relationships(clio, matter_id):
        contact = rel.get("contact") or {}
        if contact.get("name") or contact.get("last_name"):
            # Raw relationship description is the source role; _party canonicalizes it.
            jur = _clio_contact_jurisdiction(clio, contact, cache)
            parties.append(
                _clio_contact_party(ctx, contact, rel.get("description"), jurisdiction=jur)
            )
    return _dedup_parties(parties), None


def _clio_relationships(clio: Any, matter_id: Any) -> list[dict[str, Any]]:
    """Related contacts (settlor/trustee/beneficiary/…) recorded on a Clio matter.

    Clio v4 exposes these on the separate ``/relationships`` endpoint, not as a
    nested matter field. A fetch failure degrades to no related parties (the
    matter still contributes its client) rather than dropping the matter.
    """
    if matter_id is None:
        return []
    try:
        return clio.list_relationships(matter_id=matter_id, fields=CLIO_RELATIONSHIP_FIELDS)
    except Exception:
        logger.exception(
            "failed to fetch Clio relationships",
            extra={"extra_fields": {"record_id": str(matter_id)}},
        )
        return []


# -- orchestration -----------------------------------------------------------


def _dedup_by_id(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for record in records:
        rid = str(record.get("id"))
        if rid in seen:
            continue
        seen.add(rid)
        out.append(record)
    return out


def _safe_build(
    build: Callable[[], tuple[list, dict | None]],
    record_id: Any,
    *,
    matter_number: Any = None,
    matter_type: str = "—",
    description: str = "",
) -> tuple[list, dict | None]:
    """Isolate a per-matter failure: log the id only (never PII) and surface the
    matter on the Excluded Matters tab so it is never *silently* dropped (item 5)."""
    try:
        return build()
    except Exception:
        logger.exception(
            "failed to build AML parties",
            extra={"extra_fields": {"record_id": record_id}},
        )
        return [], {
            "matter_number": matter_number,
            "source_record_id": str(record_id),
            "matter_type": matter_type,
            "description": description,
            "reason": "Could not process — verify at source (see processing logs)",
        }


def run_export(
    estateably: Any,
    clio: Any | None = None,
    *,
    designations: Any | None = None,
    prior_matter_numbers: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch the roster and build the party rows + exception report.

    Returns one row per party, plus excluded matters (with reasons), excluded
    parties, the exception list, the missing-DOB worklist, the run's matter-number
    set, and summary stats. Estateably supplies parties; Clio supplies matter
    numbers and the parties for matters that exist only in Clio. ``designations``
    applies Aphrodite's include/exclude list (over the rule backstop);
    ``prior_matter_numbers`` is the previous run's matter set (for the drop check).
    """
    estates = _dedup_by_id(estateably.list_estates())
    clio_matters = _clio_matters(clio)

    parties: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    estate_numbers: set[str] = set()  # matter-number keys already sourced from Estateably

    for estate in estates:
        rows, excl = _safe_build(
            lambda estate=estate: build_estate_parties(
                estateably, estate, clio_matters=clio_matters, designations=designations
            ),
            estate.get("id"),
            matter_type=_MATTER_TYPE_LABEL[classify_case(estate)],
            description=_description(estate) or "",
        )
        parties += rows
        if excl:
            excluded.append(excl)
            if excl.get("matter_number"):
                estate_numbers.add(_matter_key(excl["matter_number"]))
        for p in rows:
            if p.get("matter_number"):
                estate_numbers.add(_matter_key(p["matter_number"]))

    # Clio-only matters — deduped by matter NUMBER only (an Estateably case adopts
    # its matched Clio display_number, so the same matter is caught here). We do
    # NOT dedup by client name: a client with several matters (Estate + POA, e.g.
    # Nagle/Fairfield/Stratford) must keep each distinct matter, not collapse them.
    clio_only = 0
    clio_contact_cache: dict[str, str] = {}  # contact id -> jurisdiction (dedupe get_contact)
    if clio is not None:
        for matter in clio_matters:
            key = _matter_key(matter.get("display_number"))
            if key and key in estate_numbers:
                continue
            rows, excl = _safe_build(
                lambda matter=matter: build_clio_only_parties(
                    clio, matter, designations=designations, contact_cache=clio_contact_cache
                ),
                matter.get("id"),
                matter_number=matter.get("display_number"),
                description=_clio_client_name(matter) or "",
            )
            parties += rows
            if excl:
                excluded.append(excl)
            elif rows:
                clio_only += 1

    # Split screened parties from those excluded (role taxonomy / self-name /
    # designation-list party exclusion) — kept for audit, not screened.
    parties, excluded_parties = _split_excluded_parties(parties, designations)

    exceptions, quality_summary = quality.finalize(parties)
    all_parties = parties + excluded_parties
    # Flag every trust matter as roster-incomplete (screened + excluded together so
    # a trust whose only row was an excluded entity is still flagged).
    exceptions += quality.trust_roster_exceptions(all_parties)
    exceptions += _matter_level_exceptions(all_parties)
    matter_numbers = _matter_numbers(all_parties, excluded)
    exceptions += _status_drop_exceptions(prior_matter_numbers, matter_numbers)
    missing_dob_list = _missing_dob_list(parties)
    summary = _summary(parties, excluded, excluded_parties, exceptions, quality_summary)

    logger.info(
        "AML export built",
        extra={
            "extra_fields": {
                "parties": len(parties),
                "excluded_matters": len(excluded),
                "excluded_parties": len(excluded_parties),
                "exceptions": len(exceptions),
                "clio_only_matters": clio_only,
                "estates_seen": len(estates),
                **{k: v for k, v in summary.items() if isinstance(v, int)},
            }
        },
    )
    return {
        "parties": parties,
        "excluded": excluded,
        "excluded_parties": excluded_parties,
        "exceptions": exceptions,
        "missing_dob_list": missing_dob_list,
        "matter_numbers": sorted(matter_numbers),
        "summary": summary,
    }


def _split_excluded_parties(
    parties: list[dict[str, Any]], designations: Any | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition built parties into (screened, excluded-with-reason).

    A party is excluded if it was already marked (role taxonomy / self-name) or if
    its name is on Aphrodite's designation exclude list (which takes precedence)."""
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for p in parties:
        reason = p.get("excluded_reason")
        if reason is None and designations is not None:
            reason = designations.party_excluded(p.get("full_legal_name"))
        if reason:
            excluded.append(
                {
                    "matter_number": p.get("matter_number"),
                    "source_record_id": p.get("source_record_id"),
                    "matter_type": p.get("matter_type"),
                    "party_type": p.get("party_type"),
                    "full_legal_name": p.get("full_legal_name"),
                    "source_role": p.get("source_role"),
                    "reason": reason,
                }
            )
        else:
            kept.append(p)
    return kept, excluded


# One exception per matter, keyed by source record (deduped), for the matter-level
# flags: single-system coverage + "not on Aphrodite designation list".
def _matter_level_exceptions(all_parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for p in all_parties:
        sid = str(p.get("source_record_id"))
        seen.setdefault(sid, p)
    out: list[dict[str, Any]] = []
    for sid, p in seen.items():
        issues: list[str] = []
        cross = p.get("cross_system")
        if cross == "estateably_only":
            issues.append("Present in Estateably only — no matching Clio matter (verify coverage)")
        elif cross == "clio_only":
            issues.append("Present in Clio only — no Estateably file (verify coverage)")
        if p.get("not_on_designation_list"):
            issues.append(
                "Matter not on Aphrodite designation list — confirm it should be screened"
            )
        for issue in issues:
            out.append(
                {
                    "matter_number": p.get("matter_number"),
                    "source_record_id": sid,
                    "party": "—",
                    "role": "—",
                    "issue": issue,
                }
            )
    return out


def _matter_numbers(all_parties: list[dict[str, Any]], excluded: list[dict[str, Any]]) -> set[str]:
    """Distinct PII-free matter-number keys seen this run (parties + excluded).

    Used for the "matter dropped since last export" check and persisted in the
    manifest; the key is digits-only so no embedded surname leaks."""
    numbers = {
        _matter_number_key(p["matter_number"]) for p in all_parties if p.get("matter_number")
    }
    numbers |= {_matter_number_key(e["matter_number"]) for e in excluded if e.get("matter_number")}
    numbers.discard("")
    return numbers


def _status_drop_exceptions(prior: set[str] | None, current: set[str]) -> list[dict[str, Any]]:
    """Flag matters present in the previous export but absent now (item 5 / status
    changes must surface, never disappear silently)."""
    if not prior:
        return []
    return [
        {
            "matter_number": number,
            "source_record_id": "—",
            "party": "—",
            "role": "—",
            "issue": (
                "Matter was in the previous export but is absent now — "
                "verify it was not removed by a source status change"
            ),
        }
        for number in sorted(prior - current)
    ]


# Roles Aphrodite prioritizes for DOB remediation (beneficiaries + principals first).
_DOB_ROLE_PRIORITY = {
    "Beneficiary": 0,
    "Deceased": 1,
    "Settlor": 1,
    "Trustee": 2,
    "Executor": 2,
    "Attorney": 3,
    "Committee": 3,
    "Protector": 4,
}


def _missing_dob_list(parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The missing-DOB worklist (decision F), sorted by matter then role priority."""
    rows = [
        {
            "matter_number": p.get("matter_number"),
            "matter_type": p.get("matter_type"),
            "party_role": p.get("party_role"),
            "full_legal_name": p.get("full_legal_name"),
            "source": p.get("source"),
        }
        for p in parties
        if "Missing date of birth" in (p.get("exception_status") or "")
    ]
    rows.sort(
        key=lambda r: (
            str(r["matter_number"] or "~~~"),
            _DOB_ROLE_PRIORITY.get(r["party_role"], 9),
            r["full_legal_name"] or "",
        )
    )
    return rows


def _summary(
    parties: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    excluded_parties: list[dict[str, Any]],
    exceptions: list[dict[str, Any]],
    quality_summary: dict[str, Any],
) -> dict[str, Any]:
    # Count matters by DISTINCT VERIFIED MATTER NUMBER (as they appear on the
    # Parties sheet), not by source record (Aphrodite item 8c). Screened parties
    # with no matter number are counted once each as "unverified".
    matters_seen: dict[str, set[str]] = {}
    unverified: set[str] = set()
    by_role: dict[str, int] = {}
    by_party_type: dict[str, int] = {}
    for p in parties:
        by_role[p.get("party_role") or "—"] = by_role.get(p.get("party_role") or "—", 0) + 1
        pt = p.get("party_type") or "Unspecified"
        by_party_type[pt] = by_party_type.get(pt, 0) + 1
        mt = p.get("matter_type") or "—"
        number = p.get("matter_number")
        if number:
            matters_seen.setdefault(mt, set()).add(str(number))
        else:
            unverified.add(str(p.get("source_record_id")))
    by_type = {mt: len(ids) for mt, ids in matters_seen.items()}

    # Reconciliation counts, derived from the SAME exception rows they appear on so
    # the Summary sheet ties out to the Exceptions/Parties tabs line-for-line (the
    # memo-vs-file mismatch Nicole flagged, 2026-07-28). "Role to be Confirmed" is
    # taken from the party roles as it appears on the Parties tab.
    def _count_issue(prefix: str) -> int:
        return sum(1 for e in exceptions if str(e.get("issue", "")).startswith(prefix))

    roster_incomplete = _count_issue("Party roster incomplete")
    return {
        "total_parties": len(parties),
        "matters_by_type": by_type,
        "unverified_matters": len(unverified),
        "parties_by_role": by_role,
        "parties_by_party_type": by_party_type,
        "role_to_be_confirmed": by_role.get("Role to be Confirmed", 0),
        "clio_only_matters": _count_issue("Present in Clio only"),
        "estateably_only_matters": _count_issue("Present in Estateably only"),
        "not_on_designation_list": _count_issue("Matter not on Aphrodite designation list"),
        "excluded_count": len(excluded),
        "excluded_parties_count": len(excluded_parties),
        "roster_incomplete_count": roster_incomplete,
        "exceptions_count": len(exceptions),
        **quality_summary,
    }
