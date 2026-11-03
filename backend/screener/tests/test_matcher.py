"""Tests for the matcher module."""

import pytest

from backend.screener.matcher import (
    SanctionMatcher,
    normalize_name,
    parse_year,
    compare_dobs,
    MATCH_HIGH_CONFIDENCE,
    MATCH_FALSE_POSITIVE,
    MATCH_POTENTIAL_HIT,
)


class TestNormalizeName:
    """Test name normalization function."""

    def test_lowercase_conversion(self):
        """Should convert names to lowercase."""
        assert normalize_name("John DOE") == "john doe"

    def test_removes_titles(self):
        """Should remove common titles."""
        assert normalize_name("Dr. John Smith") == "john smith"
        assert normalize_name("Mr. John Smith") == "john smith"
        assert normalize_name("Prof. Jane Doe") == "jane doe"

    def test_removes_punctuation(self):
        """Should replace punctuation with spaces (preserves word boundaries)."""
        # Apostrophe and comma become spaces, then collapsed
        assert normalize_name("O'Brien, John") == "o brien john"
        # Hyphen becomes space, preserving compound name parts
        assert normalize_name("Smith-Jones, Mary") == "smith jones mary"

    def test_removes_corporate_stopwords(self):
        """Should remove corporate stopwords."""
        assert normalize_name("ACME Corporation Limited") == "acme"
        assert normalize_name("John Smith Trust") == "john smith"

    def test_empty_input(self):
        """Should handle empty input."""
        assert normalize_name("") == ""
        assert normalize_name(None) == ""


class TestParseYear:
    """Test year extraction from DOB strings."""

    def test_iso_date_format(self):
        """Should extract year from ISO date format."""
        assert parse_year("1980-01-01") == 1980
        assert parse_year("2020-12-31") == 2020

    def test_year_only(self):
        """Should handle year-only strings."""
        assert parse_year("1970") == 1970

    def test_text_date_format(self):
        """Should extract year from text date formats."""
        assert parse_year("May 20, 1970") == 1970
        assert parse_year("December 1, 1985") == 1985

    def test_unknown_values(self):
        """Should return None for unknown/invalid values."""
        assert parse_year("Unknown") is None
        assert parse_year("N/A") is None
        assert parse_year("") is None
        assert parse_year(None) is None


class TestCompareDobs:
    """Test DOB comparison function."""

    def test_exact_match(self):
        """Should detect exact DOB matches."""
        exact, diff = compare_dobs("1980-01-01", "1980-01-01")
        assert exact is True
        assert diff == 0

    def test_same_year_different_day(self):
        """Should match on same year even with different day/month."""
        exact, diff = compare_dobs("1980-01-01", "1980-12-31")
        assert exact is True
        assert diff == 0

    def test_year_difference(self):
        """Should calculate year difference correctly."""
        exact, diff = compare_dobs("1980-01-01", "1950-01-01")
        assert exact is False
        assert diff == 30

    def test_unparseable_dob(self):
        """Should handle unparseable DOBs gracefully."""
        exact, diff = compare_dobs("1980-01-01", "Unknown")
        assert exact is False
        assert diff is None


class TestSanctionMatcher:
    """Test the SanctionMatcher class."""

    @pytest.fixture
    def matcher(self, mock_sanctions_data):
        """Create a matcher with mock data."""
        m = SanctionMatcher(threshold=85.0)
        m._build_index_from_data(mock_sanctions_data)
        return m

    def test_exact_name_match(self, matcher):
        """Should match exact names with high score."""
        matches = matcher.find_matches("John Doe", "1980-01-01")

        assert len(matches) > 0
        assert matches[0]["score"] >= 95
        assert matches[0]["dob_match"] is True

    def test_name_swap_handling(self, matcher):
        """Should handle first/last name swaps (Doe, John vs John Doe)."""
        matches = matcher.find_matches("Doe, John", "1980-01-01")

        assert len(matches) > 0
        # token_sort_ratio should give high score for swapped names
        assert matches[0]["score"] >= 95

    def test_dob_false_positive_penalty(self, matcher):
        """Name match with DOB mismatch should be penalized."""
        # John Smith vs Joan Smith with 30 year DOB difference
        matches = matcher.find_matches("John Smith", "1980-01-01")

        # If there's a match, it should have the false positive penalty applied
        for match in matches:
            if match["full_name"] == "Joan Smith":
                # 30 year DOB difference should trigger penalty
                assert match["match_type"] == MATCH_FALSE_POSITIVE

    def test_alias_matching(self, matcher):
        """Should match against aliases."""
        matches = matcher.find_matches("Al-Bashir")

        assert len(matches) > 0
        # Should match "Mohammed Al Bashir" via alias
        matched_names = [m["full_name"] for m in matches]
        assert "Mohammed Al Bashir" in matched_names

    def test_no_match_below_threshold(self, matcher):
        """Should not return matches below threshold."""
        matches = matcher.find_matches("Completely Random Name")

        # Should not match anything in our mock data
        assert len(matches) == 0

    def test_batch_matching(self, matcher):
        """Should handle batch matching correctly."""
        names = ["John Doe", "Unknown Person"]
        dobs = ["1980-01-01", "1990-01-01"]

        results = matcher.find_matches_batch(names, dobs)

        assert "John Doe" in results
        assert len(results["John Doe"]) > 0
        assert len(results["Unknown Person"]) == 0


class TestScoringScenarios:
    """Test specific scoring scenarios from the requirements."""

    @pytest.fixture
    def matcher(self, mock_sanctions_data):
        """Create a matcher with mock data."""
        m = SanctionMatcher(threshold=85.0)
        m._build_index_from_data(mock_sanctions_data)
        return m

    def test_scenario_1_name_swap_with_dob_match(self, matcher):
        """Scenario 1: 'Doe, John' (1980-01-01) vs 'John Doe' (1980-01-01) -> ~100%"""
        matches = matcher.find_matches("Doe, John", "1980-01-01")

        assert len(matches) > 0
        top_match = matches[0]

        # Should be high confidence with DOB boost
        assert top_match["score"] >= 100 or top_match["score"] >= 95
        assert top_match["match_type"] == MATCH_HIGH_CONFIDENCE

    def test_scenario_2_similar_name_dob_mismatch(self, matcher):
        """Scenario 2: 'John Smith' vs 'Joan Smith' with 30yr DOB diff -> <70%"""
        matches = matcher.find_matches("John Smith", "1980-01-01")

        # Any match to Joan Smith should have penalty applied
        for match in matches:
            if match["full_name"] == "Joan Smith":
                # With 30 year DOB difference, should be penalized heavily
                assert match["score"] < 70 or match["match_type"] == MATCH_FALSE_POSITIVE

    def test_scenario_3_partial_name_match(self, matcher):
        """Scenario 3: 'Al-Bashir' vs 'Mohammed Al Bashir' -> 85%+"""
        matches = matcher.find_matches("Al-Bashir")

        assert len(matches) > 0
        # Should find Mohammed Al Bashir
        top_match = matches[0]
        assert top_match["score"] >= 85
