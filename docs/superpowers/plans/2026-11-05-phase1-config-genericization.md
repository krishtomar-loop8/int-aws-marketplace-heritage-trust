# Phase 1 Config Genericization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the business rules hardcoded for one deployment (org name, office
address, HR/Finance folder exclusions, AML own-company/internal-matter filters,
quarterly-report letterhead) configurable per deployment via `APHRODITE_*`
environment variables, seeded with Aphrodite's current values as defaults.

**Architecture:** Shallow call chains (framework/synthesis, SharePoint
scoping, letterhead) get explicit parameter threading from the Lambda handler
down to the function that needs the value. The one deep call chain (AML
export's `quality.py`, called from deep inside `export.py`'s internal party
builders) gets a module-level `OrgConfig` object with `configure()`/
`reset_config()`, set once at Lambda cold start, so `export.py` needs no
changes.

**Tech Stack:** Python 3.13, pytest, Terraform (AWS Lambda environment
variables).

**Spec:** `docs/superpowers/specs/2026-11-05-phase1-config-genericization-design.md`

## Global Constraints

- Every new config value is an `APHRODITE_*` environment variable, matching
  the existing naming convention (`backend/api/handlers.py`, every
  `backend/lambdas/*/handler.py`).
- Every new default equals Aphrodite's current hardcoded value — no behavior
  change until a deployment overrides it.
- JSON-list env vars (own-company names, internal title keywords, excluded
  SharePoint folders) parse with the same graceful-fallback pattern already
  used by `_qualifying_from_env()` in `backend/lambdas/quarterly_report/handler.py:151-163`
  (JSON first, comma-split fallback, empty on total failure) — never crash on
  a malformed value.
- No existing test changes behavior or is deleted; all new tests are
  additive.
- Terraform: new variables go in the affected module's `variables.tf`, are
  declared at root in `terraform/variables.tf`, and are wired through
  `terraform/main.tf`'s module blocks — follow the exact existing pattern of
  `quarterly_qualifying_practice_areas` (root `variables.tf` → `main.tf`
  passthrough → module `variables.tf` → module `main.tf` environment block).

## Review Focus

- An empty `APHRODITE_OWN_COMPANY_NAMES` override (`"[]"`) must exclude
  nothing — not crash, not fall back to matching everything.
- A malformed JSON value in any new JSON-list env var must fall back
  gracefully (per the Global Constraint above), not raise during Lambda
  cold start.
- A custom own-company name with different casing than the party name it
  should match (e.g. configured `"Acme"`, party name `"ACME TRUST LTD."`)
  must still match, consistent with the existing `.lower()` comparison.
- An invalid regex string in `APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN`
  should fail loudly (visible in CloudWatch) the first time it's actually
  used — `quality.configure()` itself just swaps the dataclass and can't
  validate the pattern eagerly, so the `re.error` surfaces on the first
  `exclusion_reason()` call inside that invocation of `run_export()`, not at
  configure time. Not silently swallowed anywhere in between is what matters
  — consistent with how `os.environ["APHRODITE_OUTPUTS_BUCKET"]` and similar
  required values already fail in this codebase.
- Overriding only one letterhead field (e.g. just the name) via env must
  leave the other three at their defaults, not blank them out.

---

### Task 1: `framework.py` — configurable org name/office address, fix leftover string

**Files:**
- Modify: `backend/search/framework.py`
- Test: `backend/search/tests/test_framework.py` (new file)

**Interfaces:**
- Produces: `DEFAULT_ORG_NAME: str = "Aphrodite"`, `DEFAULT_ORG_OFFICE_ADDRESS: str = "545 Clyde Avenue, West Vancouver"`, `render_framework(org_name: str = DEFAULT_ORG_NAME, office_address: str = DEFAULT_ORG_OFFICE_ADDRESS) -> str` — consumed by Task 2.

- [ ] **Step 1: Write the failing tests**

```python
# backend/search/tests/test_framework.py
from backend.search.framework import render_framework

def test_render_framework_default_org():
    text = render_framework()
    assert "Aphrodite's internal matter-status assistant" in text
    assert "545 Clyde Avenue, West Vancouver" in text
    assert "c/o Aphrodite (mailing address)" in text
    assert "Heritage Trust" not in text

def test_render_framework_custom_org():
    text = render_framework(org_name="Acme Trust", office_address="1 Main St")
    assert "Acme Trust's internal matter-status assistant" in text
    assert "c/o Acme Trust (mailing address)" in text
    assert "1 Main St" in text
    assert "Aphrodite" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/search/tests/test_framework.py -v`
Expected: FAIL — `render_framework` doesn't accept `org_name`/`office_address`, and the current text has no "Acme Trust" variant (the first test also currently fails because line 99-100 still reads `"c/o Heritage\n  Trust (mailing address)"`, not `"c/o Aphrodite (mailing address)"`).

- [ ] **Step 3: Implement in `backend/search/framework.py`**

Rename the `FRAMEWORK` string constant to `_FRAMEWORK_TEMPLATE`, replacing
**three** org-name mentions with the literal placeholder token
`{{ORG_NAME}}` (current lines 36-37: `"You are Aphrodite's internal
matter-status assistant. Aphrodite\nis a BC-regulated trust company."`; and
current line 98: `"If a client's address matches Aphrodite's own office"` —
easy to miss since it's a third, separate occurrence from the office-line
templating below, not folded into it) and the office address on current
lines 98-100 with `{{OFFICE_ADDRESS}}`. Fix the leftover in the same edit:
the new office line reads `label it "c/o {{ORG_NAME}} (mailing address)"`
(today it literally says `"c/o Heritage Trust (mailing address)"` — a
rename-era leftover that survived because the string wraps across two source
lines). Add the two `DEFAULT_*` constants above `_FRAMEWORK_TEMPLATE`.
Implement:

```python
def render_framework(
    org_name: str = DEFAULT_ORG_NAME, office_address: str = DEFAULT_ORG_OFFICE_ADDRESS
) -> str:
```

Use `.replace("{{ORG_NAME}}", org_name).replace("{{OFFICE_ADDRESS}}", office_address)`
— not `.format()`, since the template's JSON example block contains literal
`{`/`}` characters that `.format()` would try to parse as fields.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/search/tests/test_framework.py -v`
Expected: PASS (2/2)

- [ ] **Step 5: Commit**

```bash
git add backend/search/framework.py backend/search/tests/test_framework.py
git commit -m "feat: make framework org name/office address configurable"
```

---

### Task 2: Thread org config through `MatterSynthesizer` and `handlers.py`

**Files:**
- Modify: `backend/search/synthesize.py:224-238` (`MatterSynthesizer.__init__`, `synthesize`)
- Modify: `backend/api/handlers.py:69-112` (`_build_deps`)
- Test: `backend/search/tests/test_synthesize.py`

**Interfaces:**
- Consumes: `DEFAULT_ORG_NAME`, `DEFAULT_ORG_OFFICE_ADDRESS`, `render_framework(org_name, office_address)` from Task 1.
- Produces: `MatterSynthesizer.__init__(self, llm, *, max_tokens=DEFAULT_MAX_TOKENS, org_name=DEFAULT_ORG_NAME, office_address=DEFAULT_ORG_OFFICE_ADDRESS)`.

- [ ] **Step 1: Write the failing test**

```python
# add to backend/search/tests/test_synthesize.py
def test_synthesizer_uses_custom_org_config():
    llm = FakeLLM('{"sections": {}, "insufficient_info": false}')
    synth = MatterSynthesizer(llm, org_name="Acme Trust", office_address="1 Main St")
    synth.synthesize(make_dossier(), "status?")
    assert "Acme Trust" in llm.calls[0]["cache_system"]
    assert "Aphrodite" not in llm.calls[0]["cache_system"]
```

(Match the existing `FakeLLM`/`make_dossier` fixtures already in this file —
see the test at line 90 for the equivalent default-org assertion pattern.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/search/tests/test_synthesize.py::test_synthesizer_uses_custom_org_config -v`
Expected: FAIL — `MatterSynthesizer.__init__` has no `org_name`/`office_address` parameters.

- [ ] **Step 3: Implement**

In `synthesize.py`, import `DEFAULT_ORG_NAME, DEFAULT_ORG_OFFICE_ADDRESS` from
`backend.search.framework` alongside the existing `render_framework` import.
Add the two keyword-only parameters to `__init__`, and compute
`self._framework = render_framework(org_name, office_address)` once (the
docstring's "static, byte-identical prefix" claim already assumes this is
computed once per Lambda lifetime, not per call). Replace both
`cache_system=render_framework()` call sites (lines 237, 263) with
`cache_system=self._framework`.

In `handlers.py::_build_deps`, read:
```python
org_name = os.environ.get("APHRODITE_ORG_NAME", DEFAULT_ORG_NAME)
office_address = os.environ.get("APHRODITE_ORG_OFFICE_ADDRESS", DEFAULT_ORG_OFFICE_ADDRESS)
```
(import the two defaults from `backend.search.framework`) and pass
`org_name=org_name, office_address=office_address` into the existing
`MatterSynthesizer(BedrockLLM(...), read_timeout=650)` call at line 112.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/search/tests/test_synthesize.py -v`
Expected: PASS, all tests including the new one and the existing line-90
default-org assertion (unchanged, since `MatterSynthesizer(llm)` still
defaults to `DEFAULT_ORG_NAME`).

- [ ] **Step 5: Commit**

```bash
git add backend/search/synthesize.py backend/api/handlers.py backend/search/tests/test_synthesize.py
git commit -m "feat: thread org config through MatterSynthesizer and handlers"
```

---

### Task 3: Configurable HR/Finance excluded SharePoint folders

**Files:**
- Modify: `backend/search/orchestrator.py:900-921` (`DossierBuilder.__init__`), `:1727` (the `is_excluded` call)
- Modify: `backend/api/handlers.py:99-105` (`DossierBuilder(...)` construction)
- Test: `backend/search/tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `scoping.is_excluded(item, exclude: frozenset[str] = DEFAULT_EXCLUDED_FOLDERS) -> bool` (already exists, unchanged — `scoping.py` needs no code change, only a new caller-side parameter).
- Produces: `DossierBuilder.__init__(..., excluded_sharepoint_folders: frozenset[str] = scoping.DEFAULT_EXCLUDED_FOLDERS)`, `handlers.py::_excluded_folders_from_env() -> frozenset[str]` (the malformed-JSON-fallback helper — the pattern Task 4 Step 5 reuses).

- [ ] **Step 1: Write the failing test**

```python
# add to backend/search/tests/test_orchestrator.py — modeled exactly on the
# existing test_collect_sharepoint_excludes_hr_finance_defensively (same file,
# ~line 1022), reusing its FakeKB / _kbhit / MatterCandidate fixtures, but with
# a folder name ("payroll") the *default* exclusion set would NOT catch, to
# prove the override — not just the default — is what's doing the excluding.
def test_dossier_builder_uses_custom_excluded_folders() -> None:
    cand = MatterCandidate(
        name="Estate of John Smith",
        refs=[SourceRef("clio", "123", "Smith")],
        matter_type="estate",
    )
    kb = FakeKB(
        [
            _kbhit(
                "John Smith matter doc.",
                "will.pdf",
                web_url="https://x/sites/DataA/Matters/Smith/will.pdf",
            ),
            _kbhit("Payroll.", "pay.xlsx", web_url="https://x/sites/DataA/Payroll/2026/pay.xlsx"),
        ]
    )
    d = DossierBuilder(
        knowledge_base=kb, excluded_sharepoint_folders=frozenset({"payroll"})
    ).build(cand, correspondence_term="smith")

    docs = d.section("documents").items
    assert len(docs) == 1 and "will.pdf" in docs[0].summary  # the Payroll doc is dropped
```

(Ruling: don't assert on the `sources_reviewed` "N HR/Finance excluded"
message text here — `orchestrator.py:1762` hardcodes that literal label
regardless of which folder names are actually configured, so it would read
"1 HR/Finance excluded" even though "payroll" isn't HR or Finance. Leaving
that label as-is is a deliberate choice: genericizing its wording would
require changing `test_collect_sharepoint_excludes_hr_finance_defensively`'s
existing assertion, which the Global Constraints rule out as non-additive.
This is a known, accepted cosmetic inaccuracy, not a functional one — the
new test verifies the actual document filtering, which is what matters.)

```python
# add to backend/api/tests/test_handlers.py
# (add `from backend.search import scoping` alongside this file's existing imports)
def test_excluded_folders_from_env_malformed_json_falls_back(monkeypatch):
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", "{not valid json")
    assert handlers._excluded_folders_from_env() == scoping.DEFAULT_EXCLUDED_FOLDERS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/search/tests/test_orchestrator.py::test_dossier_builder_uses_custom_excluded_folders backend/api/tests/test_handlers.py::test_excluded_folders_from_env_malformed_json_falls_back -v`
Expected: FAIL — `DossierBuilder.__init__` has no `excluded_sharepoint_folders` parameter (so "payroll" is never excluded), and `handlers._excluded_folders_from_env` doesn't exist yet.

- [ ] **Step 3: Implement**

Add `excluded_sharepoint_folders: frozenset[str] = scoping.DEFAULT_EXCLUDED_FOLDERS`
to `DossierBuilder.__init__`, store as `self._excluded_sharepoint_folders`.
Change the call at line 1727 from `is_excluded({"webUrl": web_url})` to
`is_excluded({"webUrl": web_url}, exclude=self._excluded_sharepoint_folders)`.

In `handlers.py::_build_deps`, parse the JSON-list env var the same way
`_qualifying_from_env()` does in `quarterly_report/handler.py:151-163`
(copy that fallback pattern, don't import it — different module):
```python
def _excluded_folders_from_env() -> frozenset[str]:
    raw = os.environ.get("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", "").strip()
    if not raw:
        return scoping.DEFAULT_EXCLUDED_FOLDERS
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = [p.strip() for p in raw.split(",") if p.strip()]
    return frozenset(str(v).lower() for v in parsed) if isinstance(parsed, list) else scoping.DEFAULT_EXCLUDED_FOLDERS
```
Pass `excluded_sharepoint_folders=_excluded_folders_from_env()` into the
existing `DossierBuilder(...)` construction. Import `json` (already imported
in this file) and `backend.search.scoping as scoping`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/search/tests/test_orchestrator.py backend/api/tests/test_handlers.py -v`
Expected: PASS, including both new tests and all pre-existing orchestrator/
handlers tests (the default-exclusion tests are unaffected since the
parameter defaults to the current constant).

- [ ] **Step 5: Commit**

```bash
git add backend/search/orchestrator.py backend/api/handlers.py backend/search/tests/test_orchestrator.py
git commit -m "feat: make HR/Finance SharePoint folder exclusions configurable"
```

---

### Task 4: `quality.py` module-level `OrgConfig` + AML handler wiring + comment cleanup

**Files:**
- Modify: `backend/lambdas/aml_export/quality.py`
- Modify: `backend/lambdas/aml_export/export.py` (comment-only: remove bare "Nicole"/"HT" attributions at lines 184, 288, 315, 466, 1089 — no behavior change)
- Modify: `backend/lambdas/aml_export/handler.py` (`_build_deps`, `handler`)
- Test: `backend/lambdas/aml_export/tests/test_quality.py`

**Interfaces:**
- Produces: `quality.OrgConfig` (frozen dataclass: `own_company_names: tuple[str, ...] = ("Aphrodite",)`, `internal_title_keywords: tuple[str, ...] = INTERNAL_TITLE_KEYWORDS` (the existing tuple, kept as the default), `internal_number_pattern: str = r"^\s*(2100-\d+|999999)\b"`), `quality.configure(config: OrgConfig) -> None`, `quality.reset_config() -> None`. `exclusion_reason(...)` and `is_excluded_party_name(...)` keep their exact existing signatures — callers in `export.py` need no changes.

- [ ] **Step 1: Write the failing tests**

```python
# add to backend/lambdas/aml_export/tests/test_quality.py
import re

import pytest

from backend.lambdas.aml_export.quality import OrgConfig

@pytest.fixture(autouse=True)
def _reset_quality_config():
    yield
    quality.reset_config()

def test_configure_changes_own_company_match():
    quality.configure(OrgConfig(own_company_names=("Acme Trust",)))
    # Different casing than configured, to pin the existing case-insensitive match.
    assert quality.is_excluded_party_name("ACME TRUST LTD.") is not None
    assert quality.is_excluded_party_name("Aphrodite") is None

def test_configure_invalid_regex_raises_at_call_time():
    quality.configure(OrgConfig(internal_number_pattern="("))  # unbalanced group
    with pytest.raises(re.error):
        quality.exclusion_reason(matter_number="1", description="", title="", status="open")

def test_configure_empty_own_company_excludes_nothing():
    quality.configure(OrgConfig(own_company_names=()))
    assert quality.is_excluded_party_name("Aphrodite") is None
    assert quality.is_excluded_party_name("Anything") is None

def test_configure_changes_internal_title_keywords():
    quality.configure(OrgConfig(internal_title_keywords=("test-internal-marker",)))
    assert quality.exclusion_reason(
        matter_number="1", description="", title="test-internal-marker", status="open"
    ) == "Internal / administrative matter"
    assert quality.exclusion_reason(
        matter_number="2100-11", description="", title="Finance & Compliance", status="open"
    ) is None  # old default keyword no longer matches once overridden

def test_configure_changes_internal_number_pattern():
    quality.configure(OrgConfig(internal_number_pattern=r"^\s*9999-\d+"))
    assert quality.exclusion_reason(
        matter_number="9999-1", description="", title="", status="open"
    ) == "Internal / administrative matter"

def test_reset_config_restores_defaults():
    quality.configure(OrgConfig(own_company_names=("Acme Trust",)))
    quality.reset_config()
    assert quality.is_excluded_party_name("Aphrodite") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/lambdas/aml_export/tests/test_quality.py -v`
Expected: FAIL — `OrgConfig`, `configure`, `reset_config` don't exist yet.

- [ ] **Step 3: Implement in `quality.py`**

Add `from dataclasses import dataclass` (or extend the existing imports).
Rename the current `INTERNAL_TITLE_KEYWORDS` tuple to stay as-is (it becomes
`OrgConfig`'s default value directly — no separate constant needed) and the
current `_INTERNAL_NUMBER_RE` pattern's source string becomes the
`internal_number_pattern` default. Add:

```python
@dataclass(frozen=True)
class OrgConfig:
    own_company_names: tuple[str, ...] = ("Aphrodite",)
    internal_title_keywords: tuple[str, ...] = INTERNAL_TITLE_KEYWORDS
    internal_number_pattern: str = r"^\s*(2100-\d+|999999)\b"

_CONFIG = OrgConfig()

def configure(config: OrgConfig) -> None:
    global _CONFIG
    _CONFIG = config

def reset_config() -> None:
    global _CONFIG
    _CONFIG = OrgConfig()
```

Change `exclusion_reason` to use `_CONFIG.internal_title_keywords` and
`re.compile(_CONFIG.internal_number_pattern).match(num)` instead of the
module constants directly (compile on each call — this function is not
hot-path enough to need caching, and a bad pattern must raise immediately
per the Global Constraints, not be silently swallowed). Change
`is_excluded_party_name` to check
`any(normalize_text(name).lower().startswith(n.lower()) for n in _CONFIG.own_company_names)`
instead of the hardcoded `"aphrodite"` prefix check; when
`_CONFIG.own_company_names` is empty, this correctly returns `None` for
every name (an empty `any(...)` over an empty sequence is `False`).

Export `OrgConfig`, `configure`, `reset_config` in `__all__`.

In this same file, remove the bare "Nicole" attributions at lines 68, 89,
166, and 170 (e.g. `"(Nicole, 2026-07-15/20)"` → delete the parenthetical;
`"(Nicole item 1)"` / `"(Nicole item 2 / decision D)"` / `"(Nicole item 3)"`
→ drop the `"Nicole "` prefix, keeping `"(item 1)"` etc.), keeping the
rationale text itself. In `export.py`:
- Line 184 (`"(Nicole 2026-08-05)"`) and line 288 (`"(Nicole 2026-08-05)"`)
  and line 315 (`"(Nicole 2026-07-28)"`): delete the parenthetical, keep the
  rest of the sentence.
- Line 466: `"The HT list takes precedence:"` → `"The designation list takes
  precedence:"`.
- Line 1089: `"the memo-vs-file mismatch Nicole flagged, 2026-07-28"` → `"the
  memo-vs-file mismatch flagged during review"` (this one isn't a clean
  parenthetical like the others — rewrite the clause, don't just strip a
  suffix).

Both files' cleanup is comment-only; no test applies — verify with
`grep -n "Nicole\|\bHT\b" backend/lambdas/aml_export/quality.py backend/lambdas/aml_export/export.py`
returning no matches.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/lambdas/aml_export/tests/test_quality.py backend/lambdas/aml_export/tests/test_export.py -v`
Expected: PASS — the new tests, and all pre-existing `test_quality.py`/
`test_export.py` tests (which exercise the default `OrgConfig`, matching
today's hardcoded values).

- [ ] **Step 5: Wire the AML export handler**

In `backend/lambdas/aml_export/handler.py`, add a helper mirroring the
existing JSON-list fallback pattern (same shape as Task 3's), and call
`quality.configure(...)` once in `_build_deps()` before constructing `Deps`
(cold-start-only — `_build_deps` already runs once per cold start via the
`_get_deps()` cache):

```python
def _org_config_from_env() -> quality.OrgConfig:
    ...  # parse APHRODITE_OWN_COMPANY_NAMES, APHRODITE_INTERNAL_TITLE_KEYWORDS
    ...  # (JSON-list, same fallback as Task 3) and APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN
    ...  # (plain string, defaults to OrgConfig()'s own default when unset)
```
`handler.py` does not currently import `quality` — add
`from backend.lambdas.aml_export import quality` alongside its existing
imports. Call `quality.configure(_org_config_from_env())` at the top of
`_build_deps()`.

- [ ] **Step 6: Run the full AML export test suite**

Run: `python -m pytest backend/lambdas/aml_export/ -v`
Expected: PASS, all tests.

- [ ] **Step 7: Commit**

```bash
git add backend/lambdas/aml_export/quality.py backend/lambdas/aml_export/export.py backend/lambdas/aml_export/handler.py backend/lambdas/aml_export/tests/test_quality.py
git commit -m "feat: make AML own-company and internal-matter filters configurable"
```

---

### Task 5: Configurable quarterly-report letterhead

**Files:**
- Modify: `backend/lambdas/quarterly_report/render_docx.py:24-32` (constants), `_letterhead`, `_signoff`
- Modify: `backend/lambdas/quarterly_report/handler.py` (`Deps`, `_build_deps`, `_generate_one`)
- Test: `backend/lambdas/quarterly_report/tests/test_render.py`

**Interfaces:**
- Produces: `LetterheadConfig` (frozen dataclass: `name`, `address`, `signoff`, `privacy_notice`, all `str`, defaults = today's literal values), `DEFAULT_LETTERHEAD = LetterheadConfig()`, `render_cover_letter(model: ReportModel, letterhead: LetterheadConfig = DEFAULT_LETTERHEAD) -> bytes`, `handler.py::_letterhead_from_env() -> LetterheadConfig` (pure — no AWS calls — mirroring the existing `_qualifying_from_env()` pattern in this same file).

- [ ] **Step 1: Write the failing test**

```python
# add to backend/lambdas/quarterly_report/tests/test_render.py
from backend.lambdas.quarterly_report.render_docx import LetterheadConfig

def test_render_cover_letter_custom_letterhead():
    letterhead = LetterheadConfig(
        name="Acme Trust",
        address="1 Main St",
        signoff="ACME TRUST",
        privacy_notice="See acme.example/privacy.",
    )
    text = _docx_text(render_cover_letter(_model(), letterhead))
    assert "Acme Trust" in text
    assert "1 Main St" in text
    assert "ACME TRUST" in text
    assert "acme.example/privacy" in text
    assert "Aphrodite" not in text

def test_render_cover_letter_default_letterhead_unchanged():
    text = _docx_text(render_cover_letter(_model()))
    assert "Aphrodite" in text
    assert "www.aphrodite.example/privacy" in text  # pre-existing assertion, must still hold
```

(`_docx_text` and `_model` are the existing helpers already used at the
current line-161 test in this file.)

```python
# add to backend/lambdas/quarterly_report/tests/test_handler.py
# (this file already imports the handler module as `H` — see its existing
# `from backend.lambdas.quarterly_report import handler as H`; add
# `from backend.lambdas.quarterly_report.render_docx import LetterheadConfig`
# alongside the existing imports)
def test_letterhead_from_env_partial_override(monkeypatch):
    monkeypatch.setenv("APHRODITE_LETTERHEAD_NAME", "Acme Trust")
    monkeypatch.delenv("APHRODITE_LETTERHEAD_ADDRESS", raising=False)
    letterhead = H._letterhead_from_env()
    assert letterhead.name == "Acme Trust"
    assert letterhead.address == LetterheadConfig().address  # untouched sibling field
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python -m pytest backend/lambdas/quarterly_report/tests/test_render.py backend/lambdas/quarterly_report/tests/test_handler.py::test_letterhead_from_env_partial_override -v`
Expected: `test_render_cover_letter_custom_letterhead` and
`test_letterhead_from_env_partial_override` FAIL (`render_cover_letter`
takes one positional arg today; `_letterhead_from_env` doesn't exist yet);
`test_render_cover_letter_default_letterhead_unchanged` PASSES already (it
only pins existing default behavior).

- [ ] **Step 3: Implement in `render_docx.py`**

Replace the four module constants with:
```python
@dataclass(frozen=True)
class LetterheadConfig:
    name: str = "Aphrodite"
    address: str = "220-545 Clyde Avenue, West Vancouver, BC V7T 1C5 | 778.742.5005 | aphrodite.example"
    signoff: str = "APHRODITE"
    privacy_notice: str = "Our privacy practices are described at www.aphrodite.example/privacy."

DEFAULT_LETTERHEAD = LetterheadConfig()
```
This replaces the block at current lines 24-32, which also deletes the bare
`"# Standing privacy disclosure at the foot of the letter (Nicole,
2026-08-11)."` comment sitting directly above `_PRIVACY_NOTICE` (not
mentioned in the spec's cleanup list, but it's inside the exact lines this
step already rewrites, so dropping it here is free — keep the comment's
substance, i.e. that this is a standing privacy disclosure, drop the name/date).
Change `render_cover_letter(model: ReportModel, letterhead: LetterheadConfig = DEFAULT_LETTERHEAD) -> bytes`,
and thread `letterhead` into the two internal calls it makes:
`_letterhead(doc, letterhead)` (replacing its use of the old
`_LETTERHEAD_NAME`/`_LETTERHEAD_ADDRESS` with `letterhead.name`/
`letterhead.address`), and `_signoff(doc, model, letterhead)` (replacing
`_SIGNOFF_COMPANY`/`_PRIVACY_NOTICE` with `letterhead.signoff`/
`letterhead.privacy_notice`, both places `_SIGNOFF_COMPANY` is used in that
function).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/lambdas/quarterly_report/tests/test_render.py -v`
Expected: PASS, both new tests and every pre-existing test in this file.

- [ ] **Step 5: Wire the quarterly-report handler**

In `handler.py`, add `letterhead: LetterheadConfig = LetterheadConfig()` to
the `Deps` dataclass, and import `LetterheadConfig` from `.render_docx`.
Add a pure, directly-testable helper (same shape as the existing
`_qualifying_from_env()` in this file):

```python
def _letterhead_from_env() -> LetterheadConfig:
    default = LetterheadConfig()
    return LetterheadConfig(
        name=os.environ.get("APHRODITE_LETTERHEAD_NAME", default.name),
        address=os.environ.get("APHRODITE_LETTERHEAD_ADDRESS", default.address),
        signoff=os.environ.get("APHRODITE_LETTERHEAD_SIGNOFF", default.signoff),
        privacy_notice=os.environ.get("APHRODITE_LETTERHEAD_PRIVACY_NOTICE", default.privacy_notice),
    )
```

Call it in `_build_deps()` and pass `letterhead=_letterhead_from_env()` into
the `Deps(...)` construction. In `_generate_one`, change
`docx = render_cover_letter(model)` to
`docx = render_cover_letter(model, deps.letterhead)`.

- [ ] **Step 6: Run the full quarterly-report test suite**

Run: `python -m pytest backend/lambdas/quarterly_report/ -v`
Expected: PASS, all tests, including both new tests from Step 1.

- [ ] **Step 7: Commit**

```bash
git add backend/lambdas/quarterly_report/render_docx.py backend/lambdas/quarterly_report/handler.py backend/lambdas/quarterly_report/tests/test_render.py
git commit -m "feat: make quarterly-report letterhead configurable"
```

---

### Task 6: Terraform variables and environment wiring

**Files:**
- Modify: `terraform/variables.tf` (root — add 10 new variables)
- Modify: `terraform/main.tf` (3 module blocks — pass the new variables through)
- Modify: `terraform/modules/search_api/variables.tf`, `.../main.tf:344` and `:389` (both Lambda environment blocks — main search function and `ab_runner`)
- Modify: `terraform/modules/aml_export/variables.tf`, `.../main.tf:113`
- Modify: `terraform/modules/quarterly_report/variables.tf`, `.../main.tf:112`
- Cleanup (comment-only, see Ruling below): bare "Nicole"/"HT" attributions in
  `terraform/modules/quarterly_report/variables.tf`, `terraform/main.tf:132,135`,
  and `terraform/modules/quarterly_report/main.tf:75,121,125`

**Ruling:** the spec only names `quality.py`/`export.py` for the bare-name
comment cleanup, not Terraform. But Task 6 is already editing
`quarterly_report/variables.tf` (which has the same kind of comment) and two
more files with identical leftover "Nicole 2026-08-06" / "HT owns this
field" phrasing — `terraform/main.tf:132,135` and
`terraform/modules/quarterly_report/main.tf:75,121,125`. Doing one of the
three files and not the other two would be a worse outcome than doing none,
so this task's comment cleanup is extended to all three files found. Cost if
this ruling is wrong (i.e., if the comment cleanup should've stayed
spec-scoped): a few extra comment-only line changes in files already being
touched by this task — negligible.

**Interfaces:**
- Consumes: the 10-row config table in the spec (env var name + default for each).
- No Python interface — this task has no unit test; verified structurally.

Add, in each of the three modules, one Terraform variable per spec-table row
that module owns, following the exact existing pattern of
`quarterly_qualifying_practice_areas` (root `variables.tf` declares it,
`main.tf`'s module block passes `= var.<name>`, the module's own
`variables.tf` declares the same name, the module's `main.tf` environment
block reads `var.<name>`). JSON-list values use `jsonencode(var.<name>)` in
the environment block (matching how other list-shaped env vars are already
passed in this codebase); plain-string values pass through directly.

| Module | New Terraform variable | Env var | Default |
|---|---|---|---|
| `search_api` | `org_name` | `APHRODITE_ORG_NAME` | `"Aphrodite"` |
| `search_api` | `org_office_address` | `APHRODITE_ORG_OFFICE_ADDRESS` | `"545 Clyde Avenue, West Vancouver"` |
| `search_api` | `excluded_sharepoint_folders` | `APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS` | `["hr", "human resources", "finance"]` |
| `aml_export` | `own_company_names` | `APHRODITE_OWN_COMPANY_NAMES` | `["Aphrodite"]` |
| `aml_export` | `internal_title_keywords` | `APHRODITE_INTERNAL_TITLE_KEYWORDS` | current `INTERNAL_TITLE_KEYWORDS` tuple, as a list |
| `aml_export` | `internal_matter_number_pattern` | `APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN` | `"^\\\\s*(2100-\\\\d+|999999)\\\\b"` |
| `quarterly_report` | `letterhead_name` | `APHRODITE_LETTERHEAD_NAME` | `"Aphrodite"` |
| `quarterly_report` | `letterhead_address` | `APHRODITE_LETTERHEAD_ADDRESS` | current address string |
| `quarterly_report` | `letterhead_signoff` | `APHRODITE_LETTERHEAD_SIGNOFF` | `"APHRODITE"` |
| `quarterly_report` | `letterhead_privacy_notice` | `APHRODITE_LETTERHEAD_PRIVACY_NOTICE` | current privacy string |

Add the same three `org_name`/`org_office_address`/`excluded_sharepoint_folders`
environment entries to the `ab_runner` Lambda's environment block
(`search_api/main.tf:391`) as well as the main search function's
(`:346`) — `ab_runner` runs the same synthesis pipeline and must see the same
org configuration.

- [ ] **Step 1: Make the edits** per the table above, in all five files, following the `quarterly_qualifying_practice_areas` pattern exactly. Do the bare-name comment cleanup (per the Ruling above) across all three affected files in the same pass: drop `"(Nicole 2026-08-06)"`/`"per Nicole 2026-08-06"` parentheticals keeping the rationale, and reword `"HT owns this field in Clio"` → `"the customer owns this field in Clio"`.

- [ ] **Step 2: Format and validate**

Run: `terraform -chdir=terraform fmt -check -recursive`
Expected: no output (already formatted) — if it reformats a file, re-run
until clean.

Run: `terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add terraform/
git commit -m "feat: wire Phase 1 config variables through Terraform"
```

---

### Task 7: Documentation

**Files:**
- Modify: `backend/README.md` (new short section: search-module config knobs)
- Modify: `backend/lambdas/aml_export/README.md` (extend the existing "Configuration" table)
- Modify: `backend/lambdas/quarterly_report/README.md` (extend the existing "Configuration" section)

**Interfaces:**
- Consumes: the final env var names from Tasks 2-6 (no code interface).

- [ ] **Step 1: Add a "Search configuration" section to `backend/README.md`** listing `APHRODITE_ORG_NAME`, `APHRODITE_ORG_OFFICE_ADDRESS`, `APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS` with their defaults and a one-line purpose each (same table style already used in `aml_export/README.md`'s configuration table).

- [ ] **Step 2: Extend `aml_export/README.md`'s configuration table** with `APHRODITE_OWN_COMPANY_NAMES`, `APHRODITE_INTERNAL_TITLE_KEYWORDS`, `APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN`.

- [ ] **Step 3: Extend `quarterly_report/README.md`'s configuration section** with `APHRODITE_LETTERHEAD_NAME`, `APHRODITE_LETTERHEAD_ADDRESS`, `APHRODITE_LETTERHEAD_SIGNOFF`, `APHRODITE_LETTERHEAD_PRIVACY_NOTICE`.

- [ ] **Step 4: Verify** — `grep -rn "APHRODITE_ORG_NAME\|APHRODITE_OWN_COMPANY_NAMES\|APHRODITE_LETTERHEAD_NAME" backend/README.md backend/lambdas/aml_export/README.md backend/lambdas/quarterly_report/README.md` each returns at least one match.

- [ ] **Step 5: Commit**

```bash
git add backend/README.md backend/lambdas/aml_export/README.md backend/lambdas/quarterly_report/README.md
git commit -m "docs: document Phase 1 configuration knobs"
```
