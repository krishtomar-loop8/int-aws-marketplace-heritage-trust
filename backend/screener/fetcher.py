#!/usr/bin/env python3
"""
Fetcher module for downloading and parsing official sanctions lists.

Optimized for serverless deployment:
- Primary: Load from pre-bundled JSON (eliminates cold-start fetch delay)
- Fallback: HTTP fetch from official sources (for local development/refresh)

Sources:
- UN Consolidated Sanctions List (XML)
- Canadian SEMA Sanctions List (XML)
- Public Safety Canada Listed Terrorist Entities (XML)
"""

import json
import logging
import os
import ssl
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request

# Configure logging for serverless environment
logger = logging.getLogger("sanctions.fetcher")

# Official source URLs (used as fallback)
UN_CONSOLIDATED_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
CANADA_SEMA_URL = "https://www.international.gc.ca/world-monde/assets/office_docs/international_relations-relations_internationales/sanctions/sema-lmes.xml"
CANADA_PS_URL = "https://www.publicsafety.gc.ca/cnt/_xml/lstd-ntts-eng.xml"

# Path to pre-bundled sanctions data
BUNDLE_PATH = os.path.join(os.path.dirname(__file__), "data", "sanctions_bundle.json")

# In-memory storage
un_list: list[dict] = []
sema_list: list[dict] = []
ps_list: list[dict] = []  # Public Safety Canada Listed Terrorist Entities
_bundle_loaded: bool = False


def load_from_bundle() -> bool:
    """
    Load sanctions data from pre-bundled JSON file.

    This is the primary loading method for serverless deployments,
    eliminating the 30-60 second cold-start fetch delay.

    Returns:
        True if bundle loaded successfully, False otherwise
    """
    global un_list, sema_list, ps_list, _bundle_loaded

    if _bundle_loaded:
        return True

    if not os.path.exists(BUNDLE_PATH):
        logger.warning(f"Bundle not found at {BUNDLE_PATH}")
        return False

    try:
        with open(BUNDLE_PATH, 'r') as f:
            bundle = json.load(f)

        un_list = bundle.get("un_list", [])
        sema_list = bundle.get("sema_list", [])
        ps_list = bundle.get("ps_list", [])
        _bundle_loaded = True

        counts = bundle.get("counts", {})
        logger.info(
            f"Loaded from bundle: {counts.get('total', 0)} entries "
            f"(UN: {counts.get('un', len(un_list))}, SEMA: {counts.get('sema', len(sema_list))}, "
            f"PS: {counts.get('ps', len(ps_list))}, generated: {bundle.get('generated_at', 'unknown')})"
        )

        return True
    except Exception as e:
        logger.error(f"Error loading bundle: {e}")
        return False


def _create_ssl_context() -> ssl.SSLContext:
    """Create SSL context for HTTPS requests."""
    ctx = ssl.create_default_context()
    return ctx


def _fetch_url(url: str) -> bytes:
    """Fetch content from URL."""
    headers = {"User-Agent": "SanctionsScreeningService/1.0"}
    request = Request(url, headers=headers)
    ctx = _create_ssl_context()
    with urlopen(request, context=ctx, timeout=60) as response:
        return response.read()


def fetch_un_consolidated() -> list[dict]:
    """
    Download and parse the UN Consolidated Sanctions List (XML).

    Returns list of entries with:
    - government_id: UN reference number
    - source: 'UN-Consolidated'
    - full_name: Primary name
    - aliases: List of AKAs
    - dob: Date of birth (if available)
    - entity_type: 'individual' or 'entity'
    """
    global un_list

    content = _fetch_url(UN_CONSOLIDATED_URL)
    root = ET.fromstring(content)

    entries = []

    # Parse individuals
    for individual in root.findall(".//INDIVIDUAL"):
        entry = _parse_un_individual(individual)
        if entry:
            entries.append(entry)

    # Parse entities
    for entity in root.findall(".//ENTITY"):
        entry = _parse_un_entity(entity)
        if entry:
            entries.append(entry)

    un_list = entries
    return entries


def _parse_un_individual(elem: ET.Element) -> dict | None:
    """Parse an individual entry from UN XML."""
    dataid = elem.find("DATAID")
    if dataid is None:
        return None

    government_id = dataid.text or ""

    # Build full name from components
    first_name = _get_text(elem, "FIRST_NAME")
    second_name = _get_text(elem, "SECOND_NAME")
    third_name = _get_text(elem, "THIRD_NAME")
    fourth_name = _get_text(elem, "FOURTH_NAME")

    name_parts = [p for p in [first_name, second_name, third_name, fourth_name] if p]
    full_name = " ".join(name_parts)

    if not full_name:
        return None

    # Extract aliases
    aliases = []
    for alias in elem.findall(".//INDIVIDUAL_ALIAS"):
        alias_name = _get_text(alias, "ALIAS_NAME")
        if alias_name and alias_name != full_name:
            aliases.append(alias_name)

    # Extract DOB
    dob = _get_text(elem, ".//DATE") or _get_text(elem, ".//YEAR")

    return {
        "government_id": f"UN-{government_id}",
        "source": "UN-Consolidated",
        "full_name": full_name,
        "aliases": aliases,
        "dob": dob or "Unknown",
        "entity_type": "individual",
    }


def _parse_un_entity(elem: ET.Element) -> dict | None:
    """Parse an entity entry from UN XML."""
    dataid = elem.find("DATAID")
    if dataid is None:
        return None

    government_id = dataid.text or ""
    full_name = _get_text(elem, "FIRST_NAME")

    if not full_name:
        return None

    # Extract aliases
    aliases = []
    for alias in elem.findall(".//ENTITY_ALIAS"):
        alias_name = _get_text(alias, "ALIAS_NAME")
        if alias_name and alias_name != full_name:
            aliases.append(alias_name)

    return {
        "government_id": f"UN-{government_id}",
        "source": "UN-Consolidated",
        "full_name": full_name,
        "aliases": aliases,
        "dob": "N/A",
        "entity_type": "entity",
    }


def _get_text(elem: ET.Element, path: str) -> str:
    """Safely extract text from XML element."""
    child = elem.find(path)
    return child.text.strip() if child is not None and child.text else ""


def _get_first_text(elem: ET.Element, *paths: str) -> str:
    """Text of the first of ``paths`` present on the element (empty if none)."""
    for path in paths:
        text = _get_text(elem, path)
        if text:
            return text
    return ""


def fetch_canada_sema() -> list[dict]:
    """
    Download and parse the Canadian SEMA Sanctions List (XML).

    Returns list of entries with:
    - government_id: Canadian reference number
    - source: 'Canada-SEMA'
    - full_name: Primary name
    - aliases: List of AKAs
    - dob: Date of birth (if available)
    - entity_type: 'individual' or 'entity'
    """
    global sema_list

    content = _fetch_url(CANADA_SEMA_URL)
    root = ET.fromstring(content)

    entries = []

    # Parse all records in the XML
    for record in root.findall(".//record"):
        entry = _parse_sema_record(record)
        if entry:
            entries.append(entry)

    sema_list = entries
    return entries


def _parse_sema_record(record: ET.Element) -> dict | None:
    """Parse a record from Canadian SEMA XML."""
    # Extract fields from the record. The list publishes bilingual field names
    # (e.g. "LastName-NomDeFamille"); the older English-only names are kept as a
    # fallback.
    schedule = _get_first_text(record, "Schedule-Annexe", "Schedule")
    item = _get_first_text(record, "Item-NumeroDarticle", "Item")
    last_name = _get_first_text(record, "LastName-NomDeFamille", "LastName")
    given_name = _get_first_text(record, "GivenName-Prenom", "GivenName")
    entity_name = _get_text(record, "Entity")
    dob = _get_text(record, "DateOfBirth")
    aliases_raw = _get_first_text(record, "Aliases-Alias", "Aliases")

    # Build full name
    if given_name or last_name:
        full_name = f"{given_name} {last_name}".strip()
        entity_type = "individual"
    elif entity_name:
        full_name = entity_name.strip()
        entity_type = "entity"
    else:
        return None

    if not full_name:
        return None

    # Build government ID
    government_id = f"SEMA-{schedule}-{item}" if schedule and item else f"SEMA-{abs(hash(full_name)) % 100000}"

    # Parse aliases (often semicolon separated)
    aliases = []
    if aliases_raw:
        # Try different separators
        for sep in [";", ",", "/"]:
            if sep in aliases_raw:
                aliases = [a.strip() for a in aliases_raw.split(sep) if a.strip() and a.strip() != full_name]
                break
        if not aliases and aliases_raw.strip() and aliases_raw.strip() != full_name:
            aliases = [aliases_raw.strip()]

    return {
        "government_id": government_id,
        "source": "Canada-SEMA",
        "full_name": full_name,
        "aliases": aliases,
        "dob": dob if dob else "Unknown",
        "entity_type": entity_type,
    }


def fetch_canada_ps() -> list[dict]:
    """
    Download and parse the Public Safety Canada Listed Terrorist Entities (XML).

    The feed is in Atom format with entry elements.

    Returns list of entries with:
    - government_id: Public Safety reference number
    - source: 'Canada-PublicSafety'
    - full_name: Primary name
    - aliases: List of AKAs
    - dob: Date of birth (if available)
    - entity_type: 'individual' or 'entity'
    """
    global ps_list

    content = _fetch_url(CANADA_PS_URL)
    root = ET.fromstring(content)

    # Atom namespace
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    entries = []

    # Parse all entry elements in the Atom feed
    for entry in root.findall("atom:entry", ns):
        parsed = _parse_ps_entry(entry, ns)
        if parsed:
            entries.append(parsed)

    ps_list = entries
    return entries


def _parse_ps_entry(entry: ET.Element, ns: dict) -> dict | None:
    """Parse an entry from Public Safety Canada Atom feed."""
    # Extract fields using namespace
    title_elem = entry.find("atom:title", ns)
    id_elem = entry.find("atom:id", ns)
    summary_elem = entry.find("atom:summary", ns)

    name = title_elem.text.strip() if title_elem is not None and title_elem.text else ""

    if not name:
        return None

    # Get ID for government reference
    entry_id = id_elem.text.strip() if id_elem is not None and id_elem.text else ""
    government_id = f"PS-{entry_id}" if entry_id else f"PS-{abs(hash(name)) % 100000}"

    # Parse aliases from summary field (often contains "AKA: alias1; alias2")
    aliases = []
    if summary_elem is not None and summary_elem.text:
        summary = summary_elem.text.strip()
        if summary and summary != "N/A":
            # Split by semicolons or commas
            for sep in [";", ","]:
                if sep in summary:
                    aliases = [a.strip() for a in summary.split(sep) if a.strip() and a.strip() != name]
                    break
            if not aliases and summary != name:
                aliases = [summary]

    return {
        "government_id": government_id,
        "source": "Canada-PublicSafety",
        "full_name": name,
        "aliases": aliases,
        "dob": "N/A",
        "entity_type": "entity",
    }


def fetch_all(force_http: bool = False) -> dict[str, list[dict]]:
    """
    Fetch all sanctions lists and return them.

    Optimized for serverless:
    - Primary: Load from pre-bundled JSON (instant, no network)
    - Fallback: HTTP fetch from official sources

    Args:
        force_http: If True, skip bundle and fetch from HTTP sources

    Returns dict with keys: 'un_list', 'sema_list', 'ps_list'
    """
    global un_list, sema_list, ps_list

    # Try loading from bundle first (for serverless cold start optimization)
    if not force_http and load_from_bundle():
        return {
            "un_list": un_list,
            "sema_list": sema_list,
            "ps_list": ps_list,
        }

    # Fallback to HTTP fetch (for local development or bundle refresh)
    logger.info("Fetching from HTTP sources...")
    results = {
        "un_list": [],
        "sema_list": [],
        "ps_list": [],
    }

    try:
        results["un_list"] = fetch_un_consolidated()
        logger.info(f"Fetched {len(results['un_list'])} entries from UN Consolidated")
    except Exception as e:
        logger.error(f"Error fetching UN list: {e}")

    try:
        results["sema_list"] = fetch_canada_sema()
        logger.info(f"Fetched {len(results['sema_list'])} entries from Canada SEMA")
    except Exception as e:
        logger.error(f"Error fetching SEMA list: {e}")

    try:
        results["ps_list"] = fetch_canada_ps()
        logger.info(f"Fetched {len(results['ps_list'])} entries from Canada Public Safety")
    except Exception as e:
        logger.error(f"Error fetching Public Safety list: {e}")

    return results


def get_combined_list() -> list[dict]:
    """Return all entries from all lists combined."""
    return un_list + sema_list + ps_list


if __name__ == "__main__":
    # Configure logging for CLI usage
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.info("Fetching sanctions lists...")
    results = fetch_all()
    logger.info(
        f"Total entries loaded: UN={len(results['un_list'])}, "
        f"SEMA={len(results['sema_list'])}, PS={len(results['ps_list'])}, "
        f"Combined={len(get_combined_list())}"
    )

    if results["un_list"]:
        logger.debug(f"Sample UN entry: {results['un_list'][0]}")

    if results["sema_list"]:
        logger.debug(f"Sample SEMA entry: {results['sema_list'][0]}")

    if results["ps_list"]:
        logger.debug(f"Sample Public Safety entry: {results['ps_list'][0]}")
