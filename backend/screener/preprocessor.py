"""
Preprocessor for extracting and deduplicating names from Excel workbooks.

Supports:
- Multi-tab legal spreadsheets (Estates, Trusts, POA)
- Simple single-sheet lists with Name/Client Name columns
- Dynamic header detection (scans first 50 rows)
- Flexible column alias mapping

All data is held in RAM only - zero disk retention.

Note: Uses openpyxl + csv directly (no pandas) for minimal deployment size.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO

from openpyxl import load_workbook


@dataclass
class ExtractedPerson:
    """A person extracted from the workbook with metadata."""
    name: str
    dob: str
    matters: list[dict] = field(default_factory=list)

    def add_matter(self, matter_number: str, source_tab: str, role: str):
        """Add a matter/role association to this person."""
        self.matters.append({
            "matter_number": matter_number,
            "source_tab": source_tab,
            "role": role,
        })

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "dob": self.dob,
            "matters": self.matters,
        }


class WorkbookPreprocessor:
    """
    Extracts and deduplicates names from Excel workbooks.

    All data is held in RAM only during the session.
    """

    # Maximum rows to scan for headers
    HEADER_SCAN_ROWS = 50

    # Column alias mappings (priority order)
    NAME_ALIASES = ["Full Legal Name", "Name", "Client Name", "Full Name", "Client"]
    DOB_ALIASES = ["Date of Birth", "DOB", "Birth Date", "Birthdate"]
    # One-row-per-party layout (the AML "Parties" sheet): keep matter linkage + role.
    MATTER_ALIASES = ["Matter Number", "Source Record ID", "Matter"]
    ROLE_ALIASES = ["Party Role", "Role"]
    SCREENING_COL = "Screening Required"

    # Column mappings for legal tabs
    ESTATES_CONFIG = {
        "tab_name": "Estates",
        "matter_col": "Matter Number",
        "extractions": [
            {"name_col": "Client Name", "dob_col": "Date of Birth", "role": "Client"},
            {"name_col": "Beneficiary 1", "dob_col": "DOB Beneficiary 1", "role": "Beneficiary 1"},
            {"name_col": "Beneficiary 2", "dob_col": "DOB Beneficiary 2", "role": "Beneficiary 2"},
            {"name_col": "Beneficiary 3", "dob_col": "DOB Beneficiary 3", "role": "Beneficiary 3"},
            {"name_col": "Beneficiary 4", "dob_col": "DOB Beneficiary 4", "role": "Beneficiary 4"},
            {"name_col": "Beneficiary 5", "dob_col": "DOB Beneficiary 5", "role": "Beneficiary 5"},
            {"name_col": "Beneficiary 6", "dob_col": "DOB Beneficiary 6", "role": "Beneficiary 6"},
        ],
    }

    TRUSTS_CONFIG = {
        "tab_name": "Trusts",
        "matter_col": "Matter Number",
        "extractions": [
            {"name_col": "Client Name", "dob_col": "Date of Birth", "role": "Client"},
            {"name_col": "Settlor", "dob_col": "Settlor DOB", "role": "Settlor"},
            {"name_col": "Co-Trustee", "dob_col": "Co-Trustee DOB", "role": "Co-Trustee"},
            {"name_col": "Protector", "dob_col": "Protector DOB", "role": "Protector"},
            {"name_col": "Beneficiary", "dob_col": "Birth date of Beneficiary", "role": "Beneficiary"},
        ],
    }

    POA_CONFIG = {
        "tab_name": "POA",
        "matter_col": "Matter Number",
        "extractions": [
            {"name_col": "Client Name", "dob_col": "Date of Birth", "role": "Client"},
            {"name_col": "Committee of Person (COP)", "dob_col": "Date of birth of COP", "role": "Committee of Person"},
            # Tolerate the sample workbook's "Commettee" misspelling.
            {"name_col": "Commettee of Person (COP)", "dob_col": "Date of birth of COP", "role": "Committee of Person"},
            {"name_col": "Beneficiary", "dob_col": "Birth date of Beneficiary", "role": "Beneficiary"},
        ],
    }

    # Legal tab names for detection
    LEGAL_TABS = {"estates", "trusts", "poa"}

    # Audit / metadata sheets in the AML export that must NOT be screened — they
    # list parties deliberately kept out of the screening set (excluded parties,
    # excluded matters), summary stats, or duplicates of the Parties sheet.
    NON_SCREENING_SHEETS = {
        "summary",
        "instructions",
        "exceptions",
        "excluded matters",
        "excluded parties",
        "missing dob",
    }

    def __init__(self):
        self._persons: dict[str, ExtractedPerson] = {}

    def _make_key(self, name: str, dob: str) -> str:
        """Create a unique key for deduplication based on name and DOB."""
        return f"{name.strip().lower()}|{dob.strip().lower()}"

    def _is_empty(self, value) -> bool:
        """Check if a value is empty/null (replacement for pd.isna)."""
        if value is None:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True
        return False

    def _normalize_dob(self, value) -> str:
        """Normalize date of birth value to string."""
        if self._is_empty(value):
            return "Unknown"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")
        return str(value).strip() or "Unknown"

    def _normalize_name(self, value) -> str | None:
        """Normalize name value, return None if empty."""
        if self._is_empty(value):
            return None
        name = str(value).strip()
        return name if name else None

    def _normalize_header(self, header) -> str:
        """Normalize a header for comparison."""
        if header is None:
            return ""
        return str(header).lower().strip()

    def _find_column_index(self, headers: list[str], col_name: str) -> int | None:
        """
        Find column index by name (case-insensitive, whitespace-tolerant).
        """
        col_lower = self._normalize_header(col_name)
        for idx, header in enumerate(headers):
            if self._normalize_header(header) == col_lower:
                return idx
        return None

    def _find_column_index_with_aliases(
        self, headers: list[str], aliases: list[str]
    ) -> int | None:
        """
        Find column index using priority-based alias list.
        """
        for alias in aliases:
            idx = self._find_column_index(headers, alias)
            if idx is not None:
                return idx
        return None

    def _get_cell_value(self, row: list, idx: int | None):
        """Safely get cell value from row by index."""
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    def _find_header_row_excel(
        self, worksheet
    ) -> tuple[int, list[str], list[list]]:
        """
        Scan first N rows to find the header row in an Excel worksheet.

        Returns:
            Tuple of (header_row_index, headers, data_rows)
        """
        # Known header indicators (any of these suggest a header row)
        header_indicators = {
            "name", "client name", "client", "full name",
            "matter number", "matter", "dob", "date of birth",
            "beneficiary", "settlor", "trustee", "protector",
        }

        # Read all rows into memory (openpyxl is lazy, so we iterate once)
        all_rows = []
        for row in worksheet.iter_rows(values_only=True):
            all_rows.append(list(row))
            if len(all_rows) > self.HEADER_SCAN_ROWS + 500:
                # Read more for data, but stop eventually for huge files
                break

        # Continue reading remaining rows
        remaining_rows = list(worksheet.iter_rows(
            min_row=len(all_rows) + 1,
            values_only=True
        ))
        all_rows.extend([list(r) for r in remaining_rows])

        # Scan first N rows to find header
        for row_idx in range(min(len(all_rows), self.HEADER_SCAN_ROWS)):
            row_values = all_rows[row_idx]
            normalized_values = {
                self._normalize_header(v)
                for v in row_values
                if not self._is_empty(v)
            }

            # Check if this row contains header indicators
            if normalized_values & header_indicators:
                headers = [str(h) if h is not None else "" for h in row_values]
                data_rows = all_rows[row_idx + 1:]
                return row_idx, headers, data_rows

        # No header found, use first row as default
        if all_rows:
            headers = [str(h) if h is not None else "" for h in all_rows[0]]
            return 0, headers, all_rows[1:]

        return 0, [], []

    def _extract_from_legal_tab(
        self, headers: list[str], data_rows: list[list], config: dict
    ) -> int:
        """
        Extract names from a legal tab (Estates, Trusts, POA) based on config.

        Does NOT require a generic 'Name' column - uses specific column mappings.
        """
        count = 0
        matter_idx = self._find_column_index(headers, config["matter_col"])

        for row in data_rows:
            matter_value = self._get_cell_value(row, matter_idx)
            matter_number = str(matter_value) if not self._is_empty(matter_value) else "Unknown"
            if matter_number == "None":
                matter_number = "Unknown"

            for extraction in config["extractions"]:
                name_idx = self._find_column_index(headers, extraction["name_col"])
                dob_idx = self._find_column_index(headers, extraction["dob_col"])

                if name_idx is None:
                    continue

                name = self._normalize_name(self._get_cell_value(row, name_idx))
                if name is None:
                    continue

                dob = self._normalize_dob(self._get_cell_value(row, dob_idx))
                key = self._make_key(name, dob)

                if key not in self._persons:
                    self._persons[key] = ExtractedPerson(name=name, dob=dob)
                    count += 1

                self._persons[key].add_matter(
                    matter_number=matter_number,
                    source_tab=config["tab_name"],
                    role=extraction["role"],
                )

        return count

    def _extract_from_standard_sheet(
        self, headers: list[str], data_rows: list[list], sheet_name: str
    ) -> int:
        """
        Extract names from a flexible list / one-row-per-party layout.

        Name Priority: 'Full Legal Name' > 'Name' > 'Client Name' > 'Full Name' > 'Client'
        DOB Priority: 'Date of Birth' > 'DOB' > 'Birth Date' > 'Birthdate'

        When present, preserves matter linkage (Matter Number / Source Record ID)
        and Party Role, and honours a 'Screening Required' column (rows marked
        "No" are skipped). Falls back to matter "N/A" / role "Listed Person" for
        a bare name list.
        """
        count = 0

        # Find name column using alias priority
        name_idx = self._find_column_index_with_aliases(headers, self.NAME_ALIASES)
        if name_idx is None:
            return 0

        dob_idx = self._find_column_index_with_aliases(headers, self.DOB_ALIASES)
        matter_idx = self._find_column_index_with_aliases(headers, self.MATTER_ALIASES)
        role_idx = self._find_column_index_with_aliases(headers, self.ROLE_ALIASES)
        screening_idx = self._find_column_index(headers, self.SCREENING_COL)

        for row in data_rows:
            # Honour an explicit "Screening Required = No" opt-out.
            screening = self._get_cell_value(row, screening_idx)
            if not self._is_empty(screening) and str(screening).strip().lower() in ("no", "false"):
                continue

            name = self._normalize_name(self._get_cell_value(row, name_idx))
            if name is None:
                continue

            dob = self._normalize_dob(self._get_cell_value(row, dob_idx))
            key = self._make_key(name, dob)

            if key not in self._persons:
                self._persons[key] = ExtractedPerson(name=name, dob=dob)
                count += 1

            matter_value = self._get_cell_value(row, matter_idx)
            matter_number = str(matter_value) if not self._is_empty(matter_value) else "N/A"
            role_value = self._get_cell_value(row, role_idx)
            role = str(role_value).strip() if not self._is_empty(role_value) else "Listed Person"

            self._persons[key].add_matter(
                matter_number=matter_number,
                source_tab=sheet_name,
                role=role,
            )

        return count

    def _is_legal_workbook(self, sheet_names_lower: list[str]) -> bool:
        """Check if workbook contains legal tabs (Estates, Trusts, POA)."""
        return bool(set(sheet_names_lower) & self.LEGAL_TABS)

    def process_workbook(self, file_content: bytes | BinaryIO) -> list[dict]:
        """
        Process an Excel workbook and extract all names with format auto-detection.

        Multi-Tab Mode:
            If 'Estates', 'Trusts', or 'POA' tabs exist, extract using
            specific column mappings. Does NOT require a 'Name' column.

        Standard Fallback:
            For single-tab files without legal tabs, looks for
            'Name' or 'Client Name' columns.

        Dynamic Header Search:
            Scans first 50 rows to find the actual header row,
            handling files with descriptive text at the top.

        Args:
            file_content: Excel file as bytes or file-like object

        Returns:
            List of deduplicated persons with metadata

        Raises:
            ValueError: If no valid format is detected
        """
        # Clear any previous data
        self._persons.clear()

        # Read file into memory
        if isinstance(file_content, bytes):
            buffer = io.BytesIO(file_content)
        else:
            buffer = file_content

        # Load Excel file with openpyxl
        workbook = load_workbook(buffer, read_only=True, data_only=True)
        sheet_names = workbook.sheetnames
        sheet_names_lower = [s.lower() for s in sheet_names]
        sheet_name_map = {s.lower(): s for s in sheet_names}

        extraction_attempted = False
        legal_tabs_found = []

        # Check for multi-tab legal workbook
        if self._is_legal_workbook(sheet_names_lower):
            # Process each legal tab that exists
            for config in [self.ESTATES_CONFIG, self.TRUSTS_CONFIG, self.POA_CONFIG]:
                tab_lower = config["tab_name"].lower()
                if tab_lower in sheet_names_lower:
                    actual_name = sheet_name_map[tab_lower]
                    legal_tabs_found.append(actual_name)

                    worksheet = workbook[actual_name]
                    _, headers, data_rows = self._find_header_row_excel(worksheet)

                    # Extract using legal tab config (no 'Name' column required)
                    self._extract_from_legal_tab(headers, data_rows, config)
                    extraction_attempted = True

        else:
            # Standard fallback: process each sheet looking for name columns,
            # skipping the export's audit/metadata sheets (excluded parties/matters,
            # summary, exceptions, missing-DOB) so they are never screened.
            for sheet_name in sheet_names:
                if sheet_name.strip().lower() in self.NON_SCREENING_SHEETS:
                    continue
                worksheet = workbook[sheet_name]
                _, headers, data_rows = self._find_header_row_excel(worksheet)

                # Try to extract using standard column aliases
                extracted = self._extract_from_standard_sheet(headers, data_rows, sheet_name)
                if extracted > 0:
                    extraction_attempted = True

        workbook.close()

        # Validate results
        if not extraction_attempted:
            raise ValueError(
                "Unable to process file. No valid data format detected.\n\n"
                "Expected formats:\n"
                "1. Legal workbook with 'Estates', 'Trusts', or 'POA' tabs\n"
                "2. Simple list with 'Name' or 'Client Name' column\n\n"
                "Please check that your file contains the expected tabs or columns."
            )

        if not self._persons:
            if legal_tabs_found:
                raise ValueError(
                    f"Found legal tabs ({', '.join(legal_tabs_found)}) but no names "
                    "could be extracted. Please verify the column headers match:\n"
                    "- 'Client Name', 'Beneficiary 1-6', 'Settlor', etc."
                )
            else:
                raise ValueError(
                    "File was processed but no names were found.\n"
                    "Please ensure the file contains a 'Name' or 'Client Name' column "
                    "with valid data."
                )

        # Return deduplicated list (zero-retention: data only in memory)
        return self.get_persons_list()

    def get_persons_list(self) -> list[dict]:
        """Get the current list of extracted persons."""
        return [person.to_dict() for person in self._persons.values()]

    def get_names_for_screening(self) -> list[str]:
        """Get just the names for screening."""
        return [person.name for person in self._persons.values()]

    def get_person_by_name(self, name: str) -> dict | None:
        """Look up a person's full metadata by name."""
        for person in self._persons.values():
            if person.name.lower() == name.lower():
                return person.to_dict()
        return None

    def clear(self):
        """Clear all extracted data from memory."""
        self._persons.clear()

    def process_csv(self, file_content: bytes | BinaryIO) -> list[dict]:
        """
        Process a CSV file and extract names with flexible column mapping.

        Supports dynamic header detection and column aliases:
        - Name: 'Name' > 'Client Name' > 'Full Name' > 'Client'
        - DOB: 'DOB' > 'Date of Birth' > 'Birth Date' > 'Birthdate'

        Args:
            file_content: CSV file as bytes or file-like object

        Returns:
            List of deduplicated persons with metadata

        Raises:
            ValueError: If no valid name column is found
        """
        # Clear any previous data
        self._persons.clear()

        # Read file into memory
        if isinstance(file_content, bytes):
            content = file_content
        else:
            content = file_content.read()

        # Try to find header row in CSV
        headers, data_rows = self._find_csv_header_row(content)

        # Extract using standard column aliases
        extracted = self._extract_from_standard_sheet(headers, data_rows, "CSV")

        if not extracted:
            raise ValueError(
                "Unable to process CSV file. No valid name column found.\n\n"
                "Expected columns (case-insensitive):\n"
                "- 'Name', 'Client Name', 'Full Name', or 'Client'\n\n"
                "Optional DOB columns:\n"
                "- 'DOB', 'Date of Birth', 'Birth Date', or 'Birthdate'"
            )

        if not self._persons:
            raise ValueError(
                "CSV file was processed but no names were found.\n"
                "Please ensure the file contains valid data in the name column."
            )

        return self.get_persons_list()

    def _find_csv_header_row(self, content: bytes) -> tuple[list[str], list[list]]:
        """
        Scan CSV to find header row, handling descriptive text at top.

        Returns:
            Tuple of (headers, data_rows)
        """
        # Known header indicators
        header_indicators = {
            "name", "client name", "client", "full name",
            "dob", "date of birth", "matter", "beneficiary",
        }

        # Decode content with fallback encodings
        text = None
        for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue

        if text is None:
            text = content.decode("utf-8", errors="ignore")

        # Parse all lines
        lines = text.splitlines()
        if not lines:
            return [], []

        # Use csv.reader to properly parse (handles quoted fields)
        all_rows = list(csv.reader(lines))
        if not all_rows:
            return [], []

        # Scan lines to find header row
        header_row_idx = 0
        for idx, row in enumerate(all_rows[:self.HEADER_SCAN_ROWS]):
            normalized_cells = {self._normalize_header(c) for c in row}
            if normalized_cells & header_indicators:
                header_row_idx = idx
                break

        headers = all_rows[header_row_idx] if header_row_idx < len(all_rows) else []
        data_rows = all_rows[header_row_idx + 1:] if header_row_idx + 1 < len(all_rows) else []

        return headers, data_rows


# Global preprocessor instance for session use
_preprocessor = WorkbookPreprocessor()


def process_workbook(file_content: bytes | BinaryIO) -> list[dict]:
    """Process a workbook and return extracted persons."""
    return _preprocessor.process_workbook(file_content)


def process_file(file_content: bytes | BinaryIO, filename: str = "") -> list[dict]:
    """
    Process any supported file (CSV or Excel) and return extracted persons.

    Args:
        file_content: File content as bytes or file-like object
        filename: Original filename to determine file type

    Returns:
        List of deduplicated persons with metadata
    """
    filename_lower = filename.lower()

    if filename_lower.endswith(".csv"):
        return _preprocessor.process_csv(file_content)
    elif filename_lower.endswith((".xlsx", ".xls")):
        return _preprocessor.process_workbook(file_content)
    else:
        # Try Excel first, fall back to CSV
        try:
            return _preprocessor.process_workbook(file_content)
        except Exception:
            return _preprocessor.process_csv(file_content)


def get_names_for_screening() -> list[str]:
    """Get names ready for sanctions screening."""
    return _preprocessor.get_names_for_screening()


def get_person_metadata(name: str) -> dict | None:
    """Get full metadata for a screened name."""
    return _preprocessor.get_person_by_name(name)


def clear_session():
    """Clear all data from memory."""
    _preprocessor.clear()
