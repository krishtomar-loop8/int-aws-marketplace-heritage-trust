"""Deployment business-config field schema + ``GET``/``PUT`` business logic.

The 13 fields mirror the existing Terraform variables 1:1 (see
``terraform/variables.tf``) — this module doesn't replace them, it adds a
runtime layer in front: a value stored via :class:`services.config.ConfigClient`
wins; otherwise the Terraform-set env var (or, failing that, the in-code
default) applies. Routes calling into this module are admin-gated in
``handlers.py`` (see ``_require_admin``); this module itself does no
authorization — it is pure validate-then-store logic, unit-testable without an
API Gateway event.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from services.config import ConfigClient

# Internal / administrative matter-title keywords, mirroring
# backend/lambdas/aml_export/quality.py::INTERNAL_TITLE_KEYWORDS.
_DEFAULT_INTERNAL_TITLE_KEYWORDS = [
    "accounting",
    "bcfsa communication",
    "finance & compliance",
    "finance and compliance",
    "board of directors",
    "wills bank",
    "suspense",
    "natalie's files",
    "natalies files",
    "aphrodite - administration",
    "aphrodite administration",
]


class ConfigValidationError(Exception):
    """A submitted config value failed validation; nothing was stored."""


@dataclass(frozen=True)
class _Field:
    name: str
    type_: type
    env_var: str
    default: Any


_FIELDS: tuple[_Field, ...] = (
    _Field("org_name", str, "APHRODITE_ORG_NAME", "Aphrodite"),
    _Field(
        "org_office_address",
        str,
        "APHRODITE_ORG_OFFICE_ADDRESS",
        "545 Clyde Avenue, West Vancouver",
    ),
    _Field(
        "excluded_sharepoint_folders",
        list,
        "APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS",
        ["hr", "human resources", "finance"],
    ),
    _Field("own_company_names", list, "APHRODITE_OWN_COMPANY_NAMES", ["Aphrodite"]),
    _Field(
        "internal_title_keywords",
        list,
        "APHRODITE_INTERNAL_TITLE_KEYWORDS",
        _DEFAULT_INTERNAL_TITLE_KEYWORDS,
    ),
    _Field(
        "internal_matter_number_pattern",
        str,
        "APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN",
        r"^\s*(2100-\d+|999999)\b",
    ),
    _Field("letterhead_name", str, "APHRODITE_LETTERHEAD_NAME", "Aphrodite"),
    _Field(
        "letterhead_address",
        str,
        "APHRODITE_LETTERHEAD_ADDRESS",
        "220-545 Clyde Avenue, West Vancouver, BC V7T 1C5 | 778.742.5005 | aphrodite.example",
    ),
    _Field("letterhead_signoff", str, "APHRODITE_LETTERHEAD_SIGNOFF", "APHRODITE"),
    _Field(
        "letterhead_privacy_notice",
        str,
        "APHRODITE_LETTERHEAD_PRIVACY_NOTICE",
        "Our privacy practices are described at www.aphrodite.example/privacy.",
    ),
    _Field(
        "quarterly_qualifying_practice_areas",
        list,
        "APHRODITE_QUARTERLY_QUALIFYING_PRACTICE_AREAS",
        [],
    ),
    _Field(
        "quarterly_exclusion_field",
        str,
        "APHRODITE_QUARTERLY_EXCLUSION_FIELD",
        "Report Exclusion",
    ),
    _Field("sharepoint_sites", list, "APHRODITE_SHAREPOINT_SITES", []),
)

_FIELDS_BY_NAME: dict[str, _Field] = {field.name: field for field in _FIELDS}


def _validate(field: _Field, value: Any) -> None:
    """Raise :class:`ConfigValidationError` if ``value`` fails ``field``'s rule.

    Mirrors the existing Terraform ``validation`` blocks word-for-word: a list
    field rejects any blank/whitespace-only entry;
    ``internal_matter_number_pattern`` must be non-empty and compile as a
    regex. No other string field has extra validation.
    """
    if field.type_ is list:
        for entry in value:
            if not str(entry).strip():
                raise ConfigValidationError(f"{field.name}: entries must not be blank")
    elif field.name == "internal_matter_number_pattern":
        if not str(value).strip():
            raise ConfigValidationError(f"{field.name}: must not be blank")
        try:
            re.compile(value)
        except re.error as exc:
            raise ConfigValidationError(f"{field.name}: not a valid regex ({exc})") from exc


def get_config(config: ConfigClient | None) -> dict[str, dict[str, Any]]:
    """Return the ``GET /config`` response body's ``"fields"`` value.

    For each of the 13 fields: if it's a key in the stored config, report its
    stored value with ``"source": "configured"``; otherwise report the env
    var (or in-code default) with ``"source": "default"``.
    """
    stored = config.get()
    fields: dict[str, dict[str, Any]] = {}
    for field in _FIELDS:
        if field.name in stored:
            fields[field.name] = {"value": stored[field.name], "source": "configured"}
        else:
            fields[field.name] = {
                "value": os.environ.get(field.env_var, field.default),
                "source": "default",
            }
    return fields


def put_config(config: ConfigClient, updates: dict[str, Any]) -> None:
    """Validate and store a partial update to the deployment config.

    Every key is validated before anything is written, so a partially-invalid
    request writes nothing. An unknown key is itself a validation error. A
    ``None`` value passes through unvalidated for :meth:`ConfigClient.put` to
    clear, reverting that field to its env var/default.
    """
    for name, value in updates.items():
        field = _FIELDS_BY_NAME.get(name)
        if field is None:
            raise ConfigValidationError(f"{name}: unknown config field")
        if value is not None:
            _validate(field, value)
    config.put(updates)
