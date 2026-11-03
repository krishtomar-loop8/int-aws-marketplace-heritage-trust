"""Screening orchestration: run extracted persons through the matcher and
return only the flagged results.

Exception-only reporting: a person with no match at/above the potential-match
threshold is discarded immediately and never added to the response, so clean
client names never leave the server. Ported from the inherited WorkSmarter
tool's ``api/index.py`` and adapted to this package layout; the fuzzy-matching
internals live in :mod:`backend.screener.matcher`.
"""

from __future__ import annotations

from typing import Any

# Score bands (percent). >=95 High Confidence (red), 85-94 Potential (orange),
# <85 discarded. Mirrors the thresholds the source tool shipped with.
HIGH_CONFIDENCE_THRESHOLD = 95
POTENTIAL_MATCH_THRESHOLD = 85


def categorize_match(match: dict[str, Any]) -> dict[str, Any]:
    """Tag a match with a category + colour based on its score band."""
    score = match.get("score", 0)
    if score >= HIGH_CONFIDENCE_THRESHOLD:
        match["category"] = "High Confidence"
        match["category_color"] = "red"
        match["action_required"] = "Immediate Review"
    elif score >= POTENTIAL_MATCH_THRESHOLD:
        match["category"] = "Potential Match"
        match["category_color"] = "orange"
        match["action_required"] = "Review Required"
    return match


def filter_and_categorize_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep matches at/above the potential-match threshold; drop the rest."""
    return [
        categorize_match(m) for m in matches if m.get("score", 0) >= POTENTIAL_MATCH_THRESHOLD
    ]


def build_summary(total_screened: int, flagged_results: list[dict[str, Any]]) -> dict[str, int]:
    """Summary counts for the screen response (no names)."""
    unique = {r["client_name"] for r in flagged_results}
    high = sum(1 for r in flagged_results if r.get("category") == "High Confidence")
    potential = sum(1 for r in flagged_results if r.get("category") == "Potential Match")
    return {
        "total_screened": total_screened,
        "total_flagged_persons": len(unique),
        "total_matches": len(flagged_results),
        "high_confidence_count": high,
        "potential_match_count": potential,
        "clear_count": total_screened - len(unique),
    }


def process_and_filter_results(
    persons: list[dict[str, Any]], matcher: Any
) -> tuple[list[dict[str, Any]], int]:
    """Screen each extracted person and return ``(flagged_results, total)``.

    Clean persons are never appended, so the returned list carries only names
    with a potential/confirmed hit — each flattened to one row per match.
    """
    flagged: list[dict[str, Any]] = []
    total = len(persons)
    for person in persons:
        name = person["name"]
        dob = person.get("dob")
        matters = person.get("matters", [])
        matches = filter_and_categorize_matches(matcher.find_matches(name, dob))
        for match in matches:
            flagged.append(
                {
                    # Client info (from the uploaded list)
                    "client_name": name,
                    "client_dob": dob,
                    "matter_number": matters[0]["matter_number"] if matters else "N/A",
                    "role": matters[0]["role"] if matters else "Listed Person",
                    "source_tab": matters[0]["source_tab"] if matters else "N/A",
                    # Sanction info (from the watchlist entry)
                    "matched_name": match["matched_name"],
                    "full_name": match["full_name"],
                    "score": match["score"],
                    "category": match["category"],
                    "category_color": match["category_color"],
                    "source_list": match["source_list"],
                    "government_id": match.get("government_id", ""),
                    "sanction_dob": match.get("dob", "Unknown"),
                    "dob_match": match.get("dob_match", False),
                    # Adjudication state (analyst sets confirmed/dismissed in the UI)
                    "status": "pending",
                }
            )
    flagged.sort(key=lambda x: x["score"], reverse=True)
    return flagged, total
