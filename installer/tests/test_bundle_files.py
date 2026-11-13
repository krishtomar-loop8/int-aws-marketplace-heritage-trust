"""Consistency checks for the data files deploy.sh reads from the release bundle."""

import re
from pathlib import Path

from services.llm import DEFAULT_MODEL_ID

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "installer"


def _lines(name: str) -> list[str]:
    return [ln.strip() for ln in (INSTALLER / name).read_text().splitlines() if ln.strip()]


def test_preflight_models_match_sources():
    tf = (ROOT / "terraform/modules/sharepoint_kb/variables.tf").read_text()
    m = re.search(r'variable "embedding_model_id"\s*\{[^}]*?default\s*=\s*"([^"]+)"', tf)
    assert m, "embedding_model_id default not found"
    assert sorted(_lines("preflight-models.txt")) == sorted([m[1], DEFAULT_MODEL_ID])


def test_retained_addresses_exist_in_terraform():
    addresses = _lines("retained-addresses.txt")
    assert addresses
    for addr in addresses:
        prefix, module, rtype, name = addr.split(".")
        assert prefix == "module", addr
        main = (ROOT / "terraform/modules" / module / "main.tf").read_text()
        assert f'resource "{rtype}" "{name}"' in main, addr


def test_deploy_script_never_bypasses_governance():
    script = (INSTALLER / "deploy.sh").read_text().lower()
    assert "bypass-governance" not in script
    assert "bypassgovernance" not in script


def test_deploy_script_sources_the_classifier():
    script = (INSTALLER / "deploy.sh").read_text()
    assert "source installer/classify_apply_failure.sh" in script


def _deploy_script_installer_reads() -> set[str]:
    """Every installer/... file deploy.sh reads at runtime (`source` and the
    `read_lines` helper's data-file argument), as opposed to incidental mentions of
    an installer/ path in a comment or an error message."""
    script = (INSTALLER / "deploy.sh").read_text()
    paths = set(re.findall(r"^\s*source (installer/\S+)", script, re.MULTILINE))
    paths |= set(re.findall(r"read_lines \w+ (installer/\S+)", script))
    return paths


def _release_yml_bundled_installer_files() -> set[str]:
    """Every installer/... path copied into bundle/installer/ by the release
    workflow's Assemble-bundle `cp` command."""
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    m = re.search(r"- name: Assemble bundle.*?(?=\n\s*- name:)", workflow, re.DOTALL)
    assert m, "could not find the 'Assemble bundle' step in release.yml"
    return set(re.findall(r"installer/[\w.-]+", m.group(0)))


def test_deploy_script_reads_are_all_shipped_in_the_release_bundle():
    # Guards against installer/classify_apply_failure.sh-style regressions: a file
    # deploy.sh sources or reads at runtime that the release workflow forgets to copy
    # into bundle/installer/, which would make every real install/upgrade/uninstall
    # fail as soon as deploy.sh tries to read it -- and, for a missing `source`, fail
    # before the ERR trap is even armed to report it. Generic by construction: any
    # future `source installer/...` or `read_lines ... installer/...` added to
    # deploy.sh is checked the same way, with no per-file update needed here.
    required = _deploy_script_installer_reads()
    assert required, "expected deploy.sh to read at least one installer/ data file"
    bundled = _release_yml_bundled_installer_files()
    missing = required - bundled
    assert not missing, (
        f"deploy.sh reads {sorted(missing)} but release.yml's Assemble bundle step "
        "does not copy it into bundle/installer/"
    )
