"""
Fuzzy matching engine for sanctions screening.

Provides:
- Name normalization (removes titles, stopwords)
- DOB comparison with year tolerance
- Weighted scoring based on name + DOB match
"""

import re
from typing import Optional


# Titles to remove from names
TITLES = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "professor",
    "sir", "dame", "lord", "lady", "rev", "reverend",
    "hon", "honorable", "honourable", "judge", "justice",
}

# Corporate stopwords to remove
CORPORATE_STOPWORDS = {
    "limited", "ltd", "llc", "inc", "incorporated", "corp", "corporation",
    "trust", "estate", "estates", "foundation", "fund", "funds",
    "holdings", "holding", "group", "international", "intl",
    "company", "co", "plc", "gmbh", "ag", "sa", "nv", "bv",
    "the", "of", "and",
}

# Match type constants
MATCH_HIGH_CONFIDENCE = "High Confidence Match"
MATCH_POTENTIAL_HIT = "Potential Hit"
MATCH_FALSE_POSITIVE = "Likely False Positive"
MATCH_STANDARD = "Standard Match"


def normalize_name(name: str) -> str:
    """
    Normalize a name for fuzzy matching.

    - Converts to lowercase
    - Removes common titles (Mr., Ms., Dr., Prof., etc.)
    - Removes non-alphanumeric characters (commas, periods, hyphens)
    - Strips corporate stopwords (Limited, Trust, Estate, Inc, etc.)

    Args:
        name: The name to normalize

    Returns:
        Normalized name string
    """
    if not name:
        return ""

    # Convert to lowercase
    normalized = name.lower()

    # Remove non-alphanumeric characters (keep spaces)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)

    # Split into tokens
    tokens = normalized.split()

    # Remove titles and corporate stopwords
    filtered_tokens = [
        token for token in tokens
        if token not in TITLES and token not in CORPORATE_STOPWORDS
    ]

    # Rejoin and clean up extra spaces
    result = " ".join(filtered_tokens)
    return result.strip()


def parse_year(dob_str: str) -> Optional[int]:
    """
    Extract year from a DOB string.

    Handles formats like:
    - "1970-05-20"
    - "1970"
    - "May 20, 1970"
    - "Unknown"

    Returns:
        Year as int, or None if unparseable
    """
    if not dob_str or dob_str.lower() in ("unknown", "n/a", ""):
        return None

    # Try to find a 4-digit year
    year_match = re.search(r"\b(19|20)\d{2}\b", str(dob_str))
    if year_match:
        return int(year_match.group())

    return None


def compare_dobs(query_dob: str, target_dob: str) -> tuple[bool, Optional[int]]:
    """
    Compare two DOB values.

    Returns:
        Tuple of (exact_match: bool, year_difference: Optional[int])
        - exact_match is True if DOBs match exactly (same string or same year)
        - year_difference is the absolute difference in years, or None if unparseable
    """
    if not query_dob or not target_dob:
        return False, None

    # Check for exact string match (normalized)
    q_norm = str(query_dob).strip().lower()
    t_norm = str(target_dob).strip().lower()

    if q_norm == t_norm:
        return True, 0

    # Parse years and compare
    q_year = parse_year(query_dob)
    t_year = parse_year(target_dob)

    if q_year is None or t_year is None:
        return False, None

    year_diff = abs(q_year - t_year)
    exact_match = year_diff == 0

    return exact_match, year_diff


class SanctionMatcher:
    """Fuzzy matcher for screening names against sanctions lists."""

    def __init__(
        self,
        threshold: float = 85.0,
        sanctions_data: Optional[list[dict]] = None,
    ):
        """
        Initialize the matcher.

        Args:
            threshold: Minimum similarity score (0-100) for a match
            sanctions_data: Optional pre-loaded sanctions data. If not provided,
                          will attempt to load from fetcher module.
        """
        from rapidfuzz import fuzz
        self._fuzz = fuzz
        self.threshold = threshold
        self.entries: list[dict] = []

        if sanctions_data is not None:
            self._build_index_from_data(sanctions_data)

    def load_from_fetcher(self, fetcher_module) -> None:
        """
        Load sanctions data from a fetcher module.

        Args:
            fetcher_module: Module with un_list, sema_list, ps_list attributes
        """
        all_data = []

        # Combine all three lists with source labels
        list_mappings = [
            (fetcher_module.un_list, "UN"),
            (fetcher_module.sema_list, "Canada-SEMA"),
            (fetcher_module.ps_list, "Public Safety Canada"),
        ]

        for sanctions_list, source_label in list_mappings:
            for entry in sanctions_list:
                entry_copy = dict(entry)
                entry_copy["_source_label"] = source_label
                all_data.append(entry_copy)

        self._build_index_from_data(all_data)

    def _build_index_from_data(self, sanctions_data: list[dict]) -> None:
        """Build search index from sanctions data."""
        self.entries = []

        for entry in sanctions_data:
            full_name = entry.get("full_name", "")
            if not full_name:
                continue

            aliases = entry.get("aliases", [])
            source_label = entry.get("_source_label", entry.get("source", "Unknown"))

            self.entries.append({
                "full_name": full_name,
                "full_name_normalized": normalize_name(full_name),
                "aliases": aliases,
                "aliases_normalized": [normalize_name(a) for a in aliases],
                "government_id": entry.get("government_id", ""),
                "source_list": source_label,
                "dob": entry.get("dob", "Unknown"),
                "entity_type": entry.get("entity_type", "unknown"),
            })

    def reload(self, fetcher_module) -> None:
        """Reload data from fetcher module."""
        self.load_from_fetcher(fetcher_module)

    def find_matches(
        self,
        query_name: str,
        query_dob: Optional[str] = None,
    ) -> list[dict]:
        """
        Match a query name against all sanctions lists with weighted scoring.

        Scoring Logic:
        - Name match > 90%: "Potential Match"
        - Name match > 80% AND DOB exact match: "High Confidence Match"
        - Name match > 85% AND DOB > 2 years apart: Score -20, "Likely False Positive"

        Args:
            query_name: The name to search for
            query_dob: Optional date of birth for weighted scoring

        Returns a list of Hits. Each hit contains:
        - matched_name: The original name or alias that matched
        - score: Weighted similarity score (0-100)
        - name_score: Original name-only score before DOB adjustment
        - match_type: Classification of match confidence
        - source_list: The source list (e.g., 'UN', 'Canada-SEMA')
        - full_name: The primary name on the sanctions entry
        - government_id: The official government reference ID
        - dob: Date of birth from sanctions list
        - dob_match: Whether DOB matched exactly

        Returns list of hits with score > threshold, sorted by score desc.
        """
        if not query_name or not query_name.strip():
            return []

        # Normalize the query name
        query_normalized = normalize_name(query_name)
        if not query_normalized:
            return []

        hits = []

        for entry in self.entries:
            best_score = 0.0
            best_matched_name = None

            # Check against normalized full_name using token_sort_ratio
            # token_sort_ratio sorts tokens alphabetically before comparing,
            # ensuring swapped first/last names don't lower the score
            score = self._fuzz.token_sort_ratio(query_normalized, entry["full_name_normalized"])
            if score > best_score:
                best_score = score
                best_matched_name = entry["full_name"]

            # Check against each normalized alias
            for i, alias_normalized in enumerate(entry["aliases_normalized"]):
                score = self._fuzz.token_sort_ratio(query_normalized, alias_normalized)
                if score > best_score:
                    best_score = score
                    best_matched_name = entry["aliases"][i]

            # Skip if below threshold
            if best_score <= self.threshold or not best_matched_name:
                continue

            # Calculate weighted score based on DOB
            name_score = best_score
            weighted_score = best_score
            match_type = MATCH_STANDARD
            dob_exact_match = False
            year_diff = None

            # Compare DOBs if query_dob provided
            if query_dob:
                dob_exact_match, year_diff = compare_dobs(query_dob, entry["dob"])

            # Apply scoring rules
            if query_dob and dob_exact_match and name_score > 85:
                # High Confidence: Name > 85% AND DOB exact match
                match_type = MATCH_HIGH_CONFIDENCE
                # Boost score slightly for exact DOB match
                weighted_score = min(100, name_score + 5)

            elif query_dob and year_diff is not None and year_diff > 2 and name_score > 85:
                # Likely False Positive: Name > 85% but DOB > 2 years apart
                # Penalty of 30 points - only near-100% matches will remain visible
                match_type = MATCH_FALSE_POSITIVE
                weighted_score = max(0, name_score - 30)

            elif name_score > 90:
                # Potential Hit: Name > 90%
                match_type = MATCH_POTENTIAL_HIT

            # Only include if weighted score still above threshold
            if weighted_score > self.threshold:
                hits.append({
                    "matched_name": best_matched_name,
                    "score": round(weighted_score, 2),
                    "name_score": round(name_score, 2),
                    "match_type": match_type,
                    "source_list": entry["source_list"],
                    "full_name": entry["full_name"],
                    "government_id": entry["government_id"],
                    "dob": entry["dob"],
                    "dob_match": dob_exact_match,
                })

        # Sort by score descending
        hits.sort(key=lambda x: x["score"], reverse=True)
        return hits

    def find_matches_batch(
        self,
        query_names: list[str],
        query_dobs: Optional[list[str]] = None,
    ) -> dict[str, list[dict]]:
        """
        Match multiple names against all sanctions lists.

        Args:
            query_names: List of names to search
            query_dobs: Optional list of DOBs (same length as query_names)

        Returns dict mapping each query name to its list of hits.
        """
        if query_dobs and len(query_dobs) == len(query_names):
            return {
                name: self.find_matches(name, dob)
                for name, dob in zip(query_names, query_dobs)
            }
        return {name: self.find_matches(name) for name in query_names}

    # Backwards compatibility aliases
    match = find_matches
    match_batch = find_matches_batch
