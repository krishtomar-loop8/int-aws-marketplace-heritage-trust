import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "installer" / "classify_apply_failure.sh"

# On Windows, subprocess.run(["bash", ...]) does not necessarily launch Git
# Bash: Windows' CreateProcess search order checks the system directory
# (C:\Windows\System32\bash.exe, a WSL launcher stub) before consulting PATH,
# so a bare "bash" can resolve to WSL instead of Git Bash even when PATH
# lists Git's bash.exe first. WSL has its own filesystem namespace (it needs
# /mnt/c/... rather than C:\...), so it can't see paths passed from Windows
# Python. shutil.which does a PATH-ordered search (same as the interactive
# shell) and reliably finds Git Bash. On non-Windows this is a no-op.
BASH = shutil.which("bash") or "bash"


def _classify(output_text: str) -> str:
    tmp = ROOT / "installer" / "tests" / "_tmp_apply_output.txt"
    tmp.write_text(output_text)
    try:
        # Paths are double-quoted in the bash -c string: this repo's working
        # directory contains spaces ("heritage trust marketplace"), which
        # would otherwise be word-split by bash before classify_apply_failure
        # ever sees them (observed on Windows/Git Bash).
        result = subprocess.run(
            [BASH, "-c", f'source "{SCRIPT}" && classify_apply_failure "{tmp}"'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    finally:
        tmp.unlink(missing_ok=True)


def test_quota_limit_exceeded_is_classified():
    msg = _classify("Error: creating Lambda Function: LimitExceededException: ...")
    assert "quota" in msg.lower() or "limit" in msg.lower()
    assert "retry" in msg.lower() or "request" in msg.lower()

def test_service_quota_exceeded_exception_is_classified():
    msg = _classify("Error: ServiceQuotaExceededException: The maximum number of...")
    assert "quota" in msg.lower() or "limit" in msg.lower()

def test_account_billing_hold_is_classified():
    msg = _classify("Error: Your account is currently blocked. Contact aws-verification@amazon.com")
    assert "billing" in msg.lower() or "account" in msg.lower()

def test_iam_access_denied_is_classified():
    msg = _classify("Error: creating IAM Role: AccessDenied: User is not authorized to perform iam:CreateRole")
    assert "permission" in msg.lower() or "iam" in msg.lower()

def test_unauthorized_operation_is_classified():
    msg = _classify("Error: UnauthorizedOperation: You are not authorized to perform this operation")
    assert "permission" in msg.lower() or "iam" in msg.lower()

def test_unrecognized_failure_returns_empty():
    msg = _classify("Error: creating S3 Bucket: BucketAlreadyExists: the requested bucket name is not available")
    assert msg == ""

def test_empty_output_returns_empty():
    msg = _classify("")
    assert msg == ""

def test_quota_wins_when_multiple_signatures_present():
    # Priority order: quota/limit is checked first, deterministically, when a
    # failure output happens to contain more than one recognized signature.
    msg = _classify(
        "Error: LimitExceededException: too many functions\n"
        "Warning: AccessDenied on an unrelated describe call"
    )
    assert "quota" in msg.lower() or "limit" in msg.lower()
