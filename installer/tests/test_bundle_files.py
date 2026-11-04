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
