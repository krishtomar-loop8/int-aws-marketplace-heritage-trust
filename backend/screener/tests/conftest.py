"""Fixtures for the ported screener tests (matcher + preprocessor).

Trimmed from the inherited tool's conftest: only the fixtures the matcher and
preprocessor tests use. Workbook fixtures use pandas (a dev-only dependency) to
build in-memory .xlsx files, matching the source tests.
"""

import io

import pandas as pd
import pytest


@pytest.fixture
def mock_sanctions_data() -> list[dict]:
    """Small hand-built watchlist for matcher tests."""
    return [
        {
            "full_name": "John Doe",
            "aliases": ["J. Doe", "Johnny Doe"],
            "government_id": "UN-12345",
            "source": "UN-Consolidated",
            "dob": "1980-01-01",
            "entity_type": "individual",
            "_source_label": "UN",
        },
        {
            "full_name": "Joan Smith",
            "aliases": [],
            "government_id": "SEMA-001",
            "source": "Canada-SEMA",
            "dob": "1950-01-01",
            "entity_type": "individual",
            "_source_label": "Canada-SEMA",
        },
        {
            "full_name": "Mohammed Al Bashir",
            "aliases": ["Al-Bashir", "M. Al Bashir"],
            "government_id": "UN-67890",
            "source": "UN-Consolidated",
            "dob": "Unknown",
            "entity_type": "individual",
            "_source_label": "UN",
        },
    ]


@pytest.fixture
def sample_csv_content() -> bytes:
    """Sample CSV with a lowercase 'name' column."""
    return (
        b"name,dob\n"
        b"John Doe,1980-01-01\n"
        b"Jane Smith,1975-05-15\n"
        b"Viktor Petrov,1970-03-20\n"
        b"Random Person,1990-08-10\n"
    )


@pytest.fixture
def sample_csv_with_client_name() -> bytes:
    """Sample CSV with a 'Client Name' column."""
    return (
        b"Client Name,Date of Birth\n"
        b"Alice Johnson,1990-02-28\n"
        b"Bob Williams,1985-11-11\n"
    )


@pytest.fixture
def legal_workbook_bytes() -> bytes:
    """A multi-tab legal workbook (Estates, Trusts, POA) like the AML export."""
    buffer = io.BytesIO()
    estates = pd.DataFrame(
        {
            "Matter Number": ["E001", "E002"],
            "Client Name": ["Estate Client 1", "Estate Client 2"],
            "Date of Birth": ["1970-01-01", "1965-05-20"],
            "Beneficiary 1": ["Ben One", "Ben Two"],
            "DOB Beneficiary 1": ["2000-01-01", "1995-03-15"],
        }
    )
    trusts = pd.DataFrame(
        {
            "Matter Number": ["T001"],
            "Client Name": ["Trust Client"],
            "Date of Birth": ["1980-01-01"],
            "Settlor": ["The Settlor"],
            "Settlor DOB": ["1950-06-15"],
        }
    )
    poa = pd.DataFrame(
        {
            "Matter Number": ["P001"],
            "Client Name": ["POA Client"],
            "Date of Birth": ["1980-03-25"],
        }
    )
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        estates.to_excel(writer, sheet_name="Estates", index=False)
        trusts.to_excel(writer, sheet_name="Trusts", index=False)
        poa.to_excel(writer, sheet_name="POA", index=False)
    buffer.seek(0)
    return buffer.read()
