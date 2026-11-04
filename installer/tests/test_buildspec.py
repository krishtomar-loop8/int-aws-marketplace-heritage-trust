"""Runs the DeployProject inline buildspec with a stubbed `aws` CLI against real bundles."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from installer.render import render
from installer.tests.test_render import HANDLER, TEMPLATE, _CfnLoader

REPO = Path(__file__).resolve().parents[2]
VERSION = "1.2.3"
CHECKSUM_MSG = (
    "Uploaded release does not match the expected checksum "
    "— re-upload the file from the GitHub Release."
)

AWS_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
if [[ $1 == s3 && $2 == cp ]]; then
  [[ -f $STUB_ZIP ]] || { echo "fatal error: 404" >&2; exit 1; }
  cp "$STUB_ZIP" "${@: -1}"
elif [[ $1 == ssm && $2 == put-parameter ]]; then
  while (($#)); do
    if [[ $1 == --value ]]; then
      if [[ $2 == file://* ]]; then cat "${2#file://}" >"$STUB_SSM"
      else printf '%s' "$2" >"$STUB_SSM"; fi
    fi
    shift
  done
fi
"""

DEPLOY_STUB = '#!/usr/bin/env bash\ntouch "$MARKER"\n'


@pytest.fixture(scope="module")
def buildspec() -> dict:
    doc = yaml.load(render(TEMPLATE, HANDLER), Loader=_CfnLoader)
    return yaml.safe_load(doc["Resources"]["DeployProject"]["Properties"]["Source"]["BuildSpec"])


def _make_zip(tmp: Path, tamper: bool = False) -> Path:
    bundle = tmp / "src-bundle"
    (bundle / "installer").mkdir(parents=True)
    (bundle / "terraform").mkdir()
    shutil.copy(REPO / "installer" / "__init__.py", bundle / "installer")
    shutil.copy(REPO / "installer" / "bundle_manifest.py", bundle / "installer")
    (bundle / "installer" / "deploy.sh").write_text(DEPLOY_STUB)
    (bundle / "terraform" / "main.tf").write_text("# tf\n")
    subprocess.run(
        [sys.executable, "-m", "installer.bundle_manifest", "build", str(bundle), VERSION, "c0"],
        cwd=REPO,
        check=True,
    )
    if tamper:
        (bundle / "terraform" / "main.tf").write_text("# tampered\n")
    out = tmp / f"aphrodite-{VERSION}.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for p in sorted(bundle.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(bundle).as_posix())
    return out


def _run(buildspec: dict, tmp: Path, zip_path: Path, sha: str):
    stub_bin = tmp / "stub-bin"
    stub_bin.mkdir()
    aws = stub_bin / "aws"
    aws.write_text(AWS_STUB)
    aws.chmod(0o755)
    work = tmp / "src"
    work.mkdir()
    tmpdir = tmp / "tmp"
    tmpdir.mkdir()
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "TMPDIR": str(tmpdir),
        "VERSION": VERSION,
        "BUNDLE_SHA256": sha,
        "ARTIFACT_BUCKET": "art-bkt",
        "STATE_BUCKET": "state-bkt",
        "CODEBUILD_BUILD_ID": "proj:build-1",
        "STUB_ZIP": str(zip_path),
        "STUB_LOG": str(tmp / "aws.log"),
        "STUB_SSM": str(tmp / "ssm-value.json"),
        "MARKER": str(tmp / "deployed"),
    }
    env.pop("PYTHONPATH", None)
    script = "\n".join(buildspec["phases"]["build"]["commands"])
    return subprocess.run(
        ["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True, check=False
    )


def _last_error(tmp: Path) -> dict:
    log = (tmp / "aws.log").read_text()
    assert "ssm put-parameter --name /aphrodite/installer/last-error --overwrite" in log
    return json.loads((tmp / "ssm-value.json").read_text())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_buildspec_runs_with_bash(buildspec):
    assert buildspec["env"]["shell"] == "bash"


def test_good_bundle_runs_deploy(buildspec, tmp_path):
    zip_path = _make_zip(tmp_path)
    res = _run(buildspec, tmp_path, zip_path, _sha(zip_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert (tmp_path / "deployed").exists()
    assert not (tmp_path / "ssm-value.json").exists()


def test_wrong_checksum_writes_exact_message(buildspec, tmp_path):
    zip_path = _make_zip(tmp_path)
    res = _run(buildspec, tmp_path, zip_path, "0" * 64)
    assert res.returncode == 1
    assert not (tmp_path / "deployed").exists()
    assert _last_error(tmp_path) == {"build_id": "proj:build-1", "message": CHECKSUM_MSG}


def test_tampered_file_fails_verification(buildspec, tmp_path):
    zip_path = _make_zip(tmp_path, tamper=True)
    res = _run(buildspec, tmp_path, zip_path, _sha(zip_path))
    assert res.returncode == 1
    assert not (tmp_path / "deployed").exists()
    err = _last_error(tmp_path)
    assert err["build_id"] == "proj:build-1"
    assert err["message"] == "Release bundle failed integrity verification: terraform/main.tf"


def test_missing_zip_fails_with_download_message(buildspec, tmp_path):
    res = _run(buildspec, tmp_path, tmp_path / "absent.zip", "0" * 64)
    assert res.returncode == 1
    assert _last_error(tmp_path)["message"] == (
        f"Could not download releases/{VERSION}/aphrodite-{VERSION}.zip from art-bkt"
    )


def test_corrupt_zip_fails_with_readable_message(buildspec, tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    res = _run(buildspec, tmp_path, bad, _sha(bad))
    assert res.returncode == 1
    assert _last_error(tmp_path)["message"].startswith(
        "Release bundle failed integrity verification"
    )
