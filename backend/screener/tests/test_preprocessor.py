"""Tests for the preprocessor module."""

import io
import pytest
import pandas as pd

from backend.screener.preprocessor import WorkbookPreprocessor, process_file


def create_test_workbook(sheets: dict[str, pd.DataFrame]) -> bytes:
    """Create an in-memory Excel workbook."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
    buffer.seek(0)
    return buffer.read()


def create_workbook_with_header_offset(
    sheets: dict[str, tuple[list[list], int]]
) -> bytes:
    """Create workbook with descriptive rows before headers."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, (data_rows, _) in sheets.items():
            df = pd.DataFrame(data_rows)
            df.to_excel(writer, sheet_name=sheet_name, index=False, header=False)
    buffer.seek(0)
    return buffer.read()


class TestColumnMapping:
    """Test column alias mapping and priority."""

    def test_name_takes_priority_over_client_name(self):
        """'Name' column should be used over 'Client Name' when both exist."""
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Name": ["John Doe", "Jane Smith"],
            "Client Name": ["WRONG1", "WRONG2"],
            "DOB": ["1980-01-01", "1975-05-15"],
        })
        workbook = create_test_workbook({"Sheet1": df})
        persons = processor.process_workbook(workbook)

        assert persons[0]["name"] == "John Doe"
        assert persons[1]["name"] == "Jane Smith"

    def test_fallback_to_client_name(self):
        """Should use 'Client Name' when 'Name' column doesn't exist."""
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Client Name": ["Alice Johnson", "Bob Williams"],
            "Date of Birth": ["1990-02-28", "1985-11-11"],
        })
        workbook = create_test_workbook({"Clients": df})
        persons = processor.process_workbook(workbook)

        assert len(persons) == 2
        assert persons[0]["name"] == "Alice Johnson"

    def test_case_insensitive_and_whitespace_tolerant(self):
        """Column names should be matched case-insensitively with whitespace tolerance."""
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "  CLIENT NAME  ": ["Whitespace Client"],
            " dob ": ["1977-08-22"],
        })
        workbook = create_test_workbook({"Sheet1": df})
        persons = processor.process_workbook(workbook)

        assert persons[0]["name"] == "Whitespace Client"
        assert persons[0]["dob"] == "1977-08-22"


class TestPartiesLayout:
    """The one-row-per-party AML export ('Parties' sheet)."""

    def test_parties_sheet_preserves_matter_and_role(self):
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Matter Number": ["190101", "190101"],
            "Party Role": ["Deceased", "Beneficiary"],
            "Full Legal Name": ["Margaret Whitfield", "Tom Whitfield"],
            "Date of Birth": ["1941-03-14", "1970-01-02"],
            "Screening Required": ["Yes", "Yes"],
        })
        persons = processor.process_workbook(create_test_workbook({"Parties": df}))

        by_name = {p["name"]: p for p in persons}
        assert set(by_name) == {"Margaret Whitfield", "Tom Whitfield"}
        # Matter number and role are retained (not hardcoded to N/A / Listed Person).
        assert by_name["Margaret Whitfield"]["matters"][0]["matter_number"] == "190101"
        assert by_name["Margaret Whitfield"]["matters"][0]["role"] == "Deceased"
        assert by_name["Tom Whitfield"]["matters"][0]["role"] == "Beneficiary"

    def test_screening_required_no_is_skipped(self):
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Matter Number": ["190101", "2100-11"],
            "Full Legal Name": ["Real Client", "Internal Admin"],
            "Screening Required": ["Yes", "No"],
        })
        persons = processor.process_workbook(create_test_workbook({"Parties": df}))
        assert [p["name"] for p in persons] == ["Real Client"]

    def test_audit_sheets_are_not_screened(self):
        # The Excluded Parties / Excluded Matters / Missing DOB sheets are audit
        # views — parties there must NOT be pulled into the screening set.
        processor = WorkbookPreprocessor()
        parties = pd.DataFrame({
            "Matter Number": ["190101"],
            "Party Role": ["Beneficiary"],
            "Full Legal Name": ["Tom Whitfield"],
            "Screening Required": ["Yes"],
        })
        excluded = pd.DataFrame({
            "Matter Number": ["240030"],
            "Full Legal Name": ["Aphrodite"],
            "Source Role": ["Executor"],
            "Reason Excluded": ["own company"],
        })
        missing = pd.DataFrame({
            "Matter Number": ["190101"],
            "Party Role": ["Deceased"],
            "Full Legal Name": ["Margaret Whitfield"],
        })
        wb = create_test_workbook(
            {"Parties": parties, "Excluded Parties": excluded, "Missing DOB": missing}
        )
        names = {p["name"] for p in processor.process_workbook(wb)}
        assert names == {"Tom Whitfield"}  # only the Parties sheet is screened

    def test_rev2_layout_with_source_role_and_text_dob(self):
        # The revised export adds a "Source Role" column and writes DOB as text;
        # the screener keys on "Party Role" + text DOB and must still round-trip.
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Matter Number": ["190101"],
            "Party Role": ["Beneficiary"],
            "Source Role": ["contingent beneficiary"],
            "Full Legal Name": ["Tom Whitfield"],
            "Date of Birth": ["1970-01-02"],
            "Screening Required": ["Yes"],
        })
        persons = processor.process_workbook(create_test_workbook({"Parties": df}))
        assert len(persons) == 1
        assert persons[0]["name"] == "Tom Whitfield"
        assert persons[0]["matters"][0]["role"] == "Beneficiary"  # normalized Party Role wins


class TestLegalWorkbook:
    """Test multi-tab legal workbook processing."""

    def test_estates_tab_extraction(self):
        """Should extract clients and beneficiaries from Estates tab."""
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Matter Number": ["E001", "E002"],
            "Client Name": ["Estate Client 1", "Estate Client 2"],
            "Date of Birth": ["1970-01-01", "1965-05-20"],
            "Beneficiary 1": ["Ben One", "Ben Two"],
            "DOB Beneficiary 1": ["2000-01-01", "1995-03-15"],
        })
        workbook = create_test_workbook({"Estates": df})
        persons = processor.process_workbook(workbook)
        names = [p["name"] for p in persons]

        assert "Estate Client 1" in names
        assert "Ben One" in names
        assert len(persons) >= 4  # 2 clients + 2 beneficiaries

    def test_multi_tab_without_name_column(self):
        """Legal tabs should work without a generic 'Name' column."""
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Matter Number": ["T001"],
            "Client Name": ["Trust Client"],
            "Date of Birth": ["1980-01-01"],
            "Settlor": ["The Settlor"],
            "Settlor DOB": ["1950-06-15"],
        })
        workbook = create_test_workbook({"Trusts": df})
        persons = processor.process_workbook(workbook)
        names = [p["name"] for p in persons]

        assert "Trust Client" in names
        assert "The Settlor" in names

    def test_full_legal_workbook(self, legal_workbook_bytes):
        """Should extract from all three tabs correctly."""
        processor = WorkbookPreprocessor()
        persons = processor.process_workbook(legal_workbook_bytes)
        names = [p["name"] for p in persons]

        assert "Estate Client 1" in names
        assert "Trust Client" in names
        assert "POA Client" in names
        assert "The Settlor" in names


class TestDynamicHeaderDetection:
    """Test header detection in files with descriptive text at top."""

    def test_header_detection_with_offset(self):
        """Should find headers even when not on first row."""
        processor = WorkbookPreprocessor()
        data_rows = [
            ["Report Generated: 2024-01-01", None, None],
            ["Confidential - Do Not Distribute", None, None],
            [None, None, None],  # Empty row
            ["Client Name", "DOB", "Notes"],  # Actual headers on row 4
            ["Dynamic Header Person", "1988-04-15", "Test note"],
            ["Another Person", "1992-07-20", "Another note"],
        ]
        workbook = create_workbook_with_header_offset({"Sheet1": (data_rows, 3)})
        persons = processor.process_workbook(workbook)
        names = [p["name"] for p in persons]

        assert "Dynamic Header Person" in names
        assert "Another Person" in names

    def test_header_detection_with_name_column(self):
        """Should detect headers with 'Name' column."""
        processor = WorkbookPreprocessor()
        data_rows = [
            ["ACME Corporation Client List", None],
            ["Generated: January 2024", None],
            ["Name", "Date of Birth"],  # Headers on row 3
            ["Header Row Person", "1975-03-10"],
            ["Second Person", "1980-12-25"],
        ]
        workbook = create_workbook_with_header_offset({"Data": (data_rows, 2)})
        persons = processor.process_workbook(workbook)

        assert len(persons) == 2


class TestErrorHandling:
    """Test error handling for invalid files."""

    def test_error_on_unknown_format(self):
        """Should raise ValueError for files without recognized columns."""
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "ID": [1, 2, 3],
            "Value": ["A", "B", "C"],
            "Random": ["X", "Y", "Z"],
        })
        workbook = create_test_workbook({"Data": df})

        with pytest.raises(ValueError) as excinfo:
            processor.process_workbook(workbook)

        assert "Unable to process file" in str(excinfo.value) or "No valid" in str(excinfo.value)

    def test_error_message_for_legal_tabs_no_data(self):
        """Should mention legal tabs in error when they exist but have no valid data."""
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Random Column": ["No valid data"],
            "Another Column": ["Still nothing"],
        })
        workbook = create_test_workbook({"Estates": df})

        with pytest.raises(ValueError) as excinfo:
            processor.process_workbook(workbook)

        error_msg = str(excinfo.value).lower()
        assert "estates" in error_msg or "legal tabs" in error_msg


class TestCSVProcessing:
    """Test CSV file processing."""

    def test_process_csv_with_name_column(self, sample_csv_content):
        """Should process CSV with 'name' column."""
        persons = process_file(sample_csv_content, "test.csv")

        assert len(persons) == 4
        names = [p["name"] for p in persons]
        assert "John Doe" in names

    def test_process_csv_with_client_name(self, sample_csv_with_client_name):
        """Should process CSV with 'Client Name' column."""
        persons = process_file(sample_csv_with_client_name, "test.csv")

        assert len(persons) == 2
        assert persons[0]["name"] == "Alice Johnson"

    def test_unknown_dob_normalization(self):
        """None/empty DOB should be normalized to 'Unknown'."""
        processor = WorkbookPreprocessor()
        df = pd.DataFrame({
            "Client Name": ["Person 1", "Person 2", "Person 3"],
            "Date of Birth": ["1990-01-01", None, ""],
        })
        workbook = create_test_workbook({"Clients": df})
        persons = processor.process_workbook(workbook)

        assert persons[1]["dob"] == "Unknown"
        assert persons[2]["dob"] == "Unknown"
