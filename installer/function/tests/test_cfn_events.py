"""Tests for the installer Lambda's custom-resource event handling (fake AWS clients)."""

import io
import json
import logging

import pytest
from botocore.exceptions import ClientError

from installer.function import handler as h

URL = "https://cfn-response.example.com/signed?X-Amz-Signature=secret123"
STACK_ID = "arn:aws:cloudformation:ca-central-1:123456789012:stack/aphrodite/abcd-1234"
BUCKET = "artifacts-bkt"


def _client_error(code: str, op: str = "HeadObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeCfn:
    def __init__(self, status="CREATE_IN_PROGRESS", error=None):
        self.status = status
        self.error = error

    def describe_stacks(self, StackName):  # noqa: N803
        if self.error:
            raise self.error
        return {"Stacks": [{"StackStatus": self.status}]}


class FakeS3:
    def __init__(self, keys=(), bodies=None):
        self.keys = set(keys)
        self.bodies = dict(bodies or {})

    def head_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) in self.keys:
            return {}
        raise _client_error("404")

    def get_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) in self.bodies:
            return {"Body": io.BytesIO(self.bodies[Bucket, Key].encode())}
        raise _client_error("NoSuchKey", "GetObject")


STATE_OBJ = ("state-bkt", "aphrodite/terraform.tfstate")
STATE_WITH_RESOURCES = json.dumps({"version": 4, "resources": [{"type": "aws_s3_bucket"}]})
EMPTY_STATE = json.dumps({"version": 4, "serial": 9, "resources": []})


class FakeSsm:
    def __init__(self, outputs=None):
        self.params = dict(outputs or {})
        self.deleted: list[str] = []

    def get_parameters_by_path(self, Path, **kwargs):  # noqa: N803
        return {"Parameters": [{"Name": k, "Value": v} for k, v in self.params.items()]}

    def delete_parameter(self, Name):  # noqa: N803
        self.deleted.append(Name)
        raise _client_error("ParameterNotFound", "DeleteParameter")


class FakeCodebuild:
    def __init__(self, in_progress=False):
        self.in_progress = in_progress
        self.started: list[dict] = []

    def list_builds_for_project(self, **kwargs):
        return {"ids": ["b1", "b2"]}

    def batch_get_builds(self, ids):
        status = "IN_PROGRESS" if self.in_progress else "SUCCEEDED"
        return {"builds": [{"id": i, "buildStatus": status} for i in ids]}

    def start_build(self, **kwargs):
        self.started.append(kwargs)
        return {"build": {"id": "new"}}


def _setup(
    monkeypatch,
    *,
    keys=(),
    status="CREATE_IN_PROGRESS",
    in_progress=False,
    outputs=None,
    cfn_error=None,
    state=False,
):
    puts: list[dict] = []
    bodies = {}
    if state:
        bodies[STATE_OBJ] = STATE_WITH_RESOURCES if state is True else state
    deps = h.Deps(
        codebuild=FakeCodebuild(in_progress),
        ssm=FakeSsm(outputs),
        s3=FakeS3(keys, bodies),
        cfn=FakeCfn(status, cfn_error),
        http_put=lambda url, body: puts.append({"url": url, "body": json.loads(body)}),
        project_name="proj",
        state_bucket="state-bkt",
        artifact_bucket=BUCKET,
    )
    monkeypatch.setattr(h, "_DEPS", deps)
    return deps, puts


def _event(request_type="Create", version="1.0.0", old_version=None, sha="abc", **extra):
    props = {
        "Version": version,
        "BundleSha256": sha,
        "Environment": "prod",
        "SamlMetadataUrl": "",
        "DeleteData": "false",
    }
    event = {
        "RequestType": request_type,
        "ResponseURL": URL,
        "StackId": STACK_ID,
        "RequestId": "req-1",
        "LogicalResourceId": "Installer",
        "ResourceProperties": props,
    }
    if request_type == "Update":
        event["OldResourceProperties"] = {**props, "Version": old_version or ""}
    event.update(extra)
    return event


RELEASE_1 = (BUCKET, "releases/1.0.0/aphrodite-1.0.0.zip")
URB = "UPDATE_ROLLBACK_IN_PROGRESS"


@pytest.mark.parametrize(
    ("rt", "props", "old", "status", "state", "kind", "version", "delete_data"),
    [
        ("Create", {"Version": ""}, {}, "CREATE_IN_PROGRESS", False, "noop", "", False),
        ("Create", {"Version": "1.0.0"}, {}, "CREATE_IN_PROGRESS", False, "apply", "1.0.0", False),
        (
            "Update",
            {"Version": ""},
            {"Version": ""},
            "UPDATE_IN_PROGRESS",
            False,
            "noop",
            "",
            False,
        ),
        (
            "Update",
            {"Version": "2.0.0"},
            {"Version": "1.0.0"},
            "UPDATE_IN_PROGRESS",
            True,
            "apply",
            "2.0.0",
            False,
        ),
        (
            "Update",
            {"Version": ""},
            {"Version": "1.0.0"},
            "UPDATE_ROLLBACK_IN_PROGRESS",
            True,
            "destroy",
            "1.0.0",
            True,
        ),
        (
            "Update",
            {"Version": ""},
            {"Version": "1.0.0"},
            "UPDATE_IN_PROGRESS",
            True,
            "fail",
            "",
            False,
        ),
        (
            "Delete",
            {"Version": "1.0.0"},
            {},
            "ROLLBACK_IN_PROGRESS",
            True,
            "destroy",
            "1.0.0",
            True,
        ),
        ("Delete", {"Version": "1.0.0"}, {}, "ROLLBACK_IN_PROGRESS", False, "noop", "", False),
        ("Delete", {"Version": "1.0.0"}, {}, "DELETE_IN_PROGRESS", False, "noop", "", False),
        ("Delete", {"Version": ""}, {}, "DELETE_IN_PROGRESS", True, "fail", "", False),
        (
            "Delete",
            {"Version": "1.0.0", "DeleteData": "true"},
            {},
            "DELETE_IN_PROGRESS",
            True,
            "destroy",
            "1.0.0",
            True,
        ),
        (
            "Delete",
            {"Version": "1.0.0", "DeleteData": "false"},
            {},
            "DELETE_IN_PROGRESS",
            True,
            "destroy",
            "1.0.0",
            False,
        ),
    ],
)
def test_decide_table(rt, props, old, status, state, kind, version, delete_data):
    d = h.decide(rt, props, old, status, state)
    assert (d.kind, d.version, d.delete_data) == (kind, version, delete_data)


SHA_OLD = "a" * 64


def test_decide_rollback_uses_old_sha_when_state_present():
    old = {"Version": "1.0.0", "BundleSha256": SHA_OLD}
    d = h.decide("Update", {"Version": "", "BundleSha256": ""}, old, URB, True)
    assert (d.kind, d.version, d.delete_data, d.bundle_sha256) == (
        "destroy",
        "1.0.0",
        True,
        SHA_OLD,
    )


def test_decide_rollback_without_state_is_noop():
    old = {"Version": "1.0.0", "BundleSha256": SHA_OLD}
    assert h.decide("Update", {"Version": ""}, old, URB, False).kind == "noop"


def _rollback_event():
    event = _event("Update", version="", old_version="1.0.0", sha="")
    event["OldResourceProperties"]["BundleSha256"] = SHA_OLD
    return event


def test_rollback_handler_destroys_with_old_sha(monkeypatch):
    deps, puts = _setup(monkeypatch, keys={RELEASE_1}, status=URB, state=True)
    h.handler(_rollback_event(), None)
    assert puts == []
    env = {e["name"]: e["value"] for e in deps.codebuild.started[0]["environmentVariablesOverride"]}
    assert env["ACTION"] == "destroy"
    assert env["VERSION"] == "1.0.0"
    assert env["BUNDLE_SHA256"] == SHA_OLD
    assert env["DELETE_DATA"] == "true"


def test_rollback_handler_without_state_replies_success(monkeypatch):
    deps, puts = _setup(monkeypatch, status=URB, state=False)
    h.handler(_rollback_event(), None)
    assert [p["body"]["Status"] for p in puts] == ["SUCCESS"]
    assert deps.codebuild.started == []


def test_decide_fail_messages():
    d = h.decide("Update", {"Version": ""}, {"Version": "1"}, "UPDATE_IN_PROGRESS", True)
    assert d.reason == "Clearing AphroditeVersion is not supported; delete the stack to uninstall."
    d = h.decide("Delete", {"Version": ""}, {}, "DELETE_IN_PROGRESS", True)
    assert d.reason == (
        "Terraform state exists but no version is set — see installer/README.md 'Manual teardown'."
    )


def test_create_empty_version_replies_success_without_build(monkeypatch):
    deps, puts = _setup(monkeypatch)
    h.handler(_event(version=""), None)
    assert len(puts) == 1
    assert puts[0]["body"]["Status"] == "SUCCESS"
    assert puts[0]["body"]["Data"] == h.NOT_DEPLOYED
    assert deps.codebuild.started == []


def test_noop_replies_with_stored_outputs(monkeypatch):
    _, puts = _setup(monkeypatch, outputs={"/aphrodite/installer/outputs/AppUrl": "https://x"})
    h.handler(_event(version=""), None)
    assert puts[0]["body"]["Data"]["AppUrl"] == "https://x"
    assert puts[0]["body"]["Data"]["DeployedVersion"] == "not-deployed"


def test_apply_starts_build_and_does_not_reply(monkeypatch):
    deps, puts = _setup(monkeypatch, keys={RELEASE_1})
    h.handler(_event(), None)
    assert puts == []
    assert len(deps.codebuild.started) == 1
    call = deps.codebuild.started[0]
    assert call["projectName"] == "proj"
    env = {e["name"]: e["value"] for e in call["environmentVariablesOverride"]}
    assert env["ACTION"] == "apply"
    assert env["VERSION"] == "1.0.0"
    assert env["CFN_RESPONSE_URL"] == URL
    assert env["TF_VAR_aws_region"] == "ca-central-1"
    assert env["CFN_PHYSICAL_ID"] == "aphrodite-123456789012-ca-central-1"
    assert all(e["type"] == "PLAINTEXT" for e in call["environmentVariablesOverride"])
    assert deps.ssm.deleted == ["/aphrodite/installer/last-error"]
    assert "TF_VAR_saml_metadata_url" not in env
    assert all(e["value"] != "" for e in call["environmentVariablesOverride"])


def test_last_error_clear_failure_fails_before_build(monkeypatch):
    deps, puts = _setup(monkeypatch, keys={RELEASE_1})

    def boom(Name):  # noqa: N803
        raise _client_error("AccessDeniedException", "DeleteParameter")

    deps.ssm.delete_parameter = boom
    h.handler(_event(), None)
    assert len(puts) == 1
    assert puts[0]["body"]["Status"] == "FAILED"
    assert deps.codebuild.started == []


def test_missing_bundle_sha_fails(monkeypatch):
    deps, puts = _setup(monkeypatch, keys={RELEASE_1})
    h.handler(_event(sha=""), None)
    assert puts[0]["body"]["Status"] == "FAILED"
    assert deps.codebuild.started == []


def test_missing_release_zip_fails_with_key(monkeypatch):
    _, puts = _setup(monkeypatch)
    h.handler(_event(), None)
    body = puts[0]["body"]
    assert body["Status"] == "FAILED"
    assert "releases/1.0.0/aphrodite-1.0.0.zip" in body["Reason"]
    assert BUCKET in body["Reason"]
    assert body["Reason"].startswith("Release 1.0.0 has not been uploaded to")


def test_rollback_names_old_version_key(monkeypatch):
    _, puts = _setup(monkeypatch, status="UPDATE_ROLLBACK_IN_PROGRESS", state=True)
    h.handler(_event("Update", version="", old_version="1.0.0"), None)
    body = puts[0]["body"]
    assert body["Status"] == "FAILED"
    assert "releases/1.0.0/" in body["Reason"]


def test_build_in_progress_fails(monkeypatch):
    deps, puts = _setup(monkeypatch, keys={RELEASE_1}, in_progress=True)
    h.handler(_event(), None)
    assert puts[0]["body"]["Status"] == "FAILED"
    assert puts[0]["body"]["Reason"] == "a deployment is already in progress"
    assert deps.codebuild.started == []


def test_exception_replies_failed(monkeypatch):
    _, puts = _setup(monkeypatch, cfn_error=RuntimeError("boom"))
    h.handler(_event(), None)
    assert puts[0]["body"]["Status"] == "FAILED"
    assert puts[0]["body"]["Reason"] == "boom"
    assert puts[0]["body"]["Data"] == h.NOT_DEPLOYED


def test_physical_id_stable_across_updates(monkeypatch):
    _, puts = _setup(monkeypatch)
    h.handler(_event(version=""), None)
    _, puts2 = _setup(monkeypatch)
    h.handler(_event("Update", version="", old_version=""), None)
    for p in (puts[0], puts2[0]):
        assert p["body"]["PhysicalResourceId"] == "aphrodite-123456789012-ca-central-1"


def test_codebuild_event_is_ignored(monkeypatch):
    _, puts = _setup(monkeypatch)
    h.handler({"source": "aws.codebuild"}, None)
    assert puts == []


def test_response_url_never_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _setup(monkeypatch, keys={RELEASE_1})
    h.handler(_event(), None)  # apply
    _setup(monkeypatch)
    h.handler(_event(), None)  # fail (missing zip)
    _setup(monkeypatch)
    h.handler(_event(version=""), None)  # noop
    _setup(monkeypatch, cfn_error=RuntimeError(f"bad {URL}"))
    h.handler(_event(), None)  # exception path
    assert caplog.records
    assert not any("secret123" in r.getMessage() or URL in r.getMessage() for r in caplog.records)
    assert "secret123" not in caplog.text


def test_delete_after_rollback_teardown_with_empty_state_is_noop(monkeypatch):
    # terraform destroy leaves an empty state object behind; that is not a deployment.
    deps, puts = _setup(monkeypatch, status="DELETE_IN_PROGRESS", state=EMPTY_STATE)
    h.handler(_event("Delete", version="", sha=""), None)
    assert [p["body"]["Status"] for p in puts] == ["SUCCESS"]
    assert deps.codebuild.started == []


def test_delete_with_resources_in_state_and_no_version_still_fails(monkeypatch):
    deps, puts = _setup(monkeypatch, status="DELETE_IN_PROGRESS", state=STATE_WITH_RESOURCES)
    h.handler(_event("Delete", version="", sha=""), None)
    assert puts[0]["body"]["Status"] == "FAILED"
    assert puts[0]["body"]["Reason"] == h.NO_VERSION_MSG
    assert deps.codebuild.started == []


def test_rollback_with_empty_state_is_noop(monkeypatch):
    deps, puts = _setup(monkeypatch, status=URB, state=EMPTY_STATE)
    h.handler(_rollback_event(), None)
    assert [p["body"]["Status"] for p in puts] == ["SUCCESS"]
    assert deps.codebuild.started == []


def test_unreadable_state_counts_as_deployed(monkeypatch):
    _, puts = _setup(monkeypatch, status="DELETE_IN_PROGRESS", state="not json")
    h.handler(_event("Delete", version="", sha=""), None)
    assert puts[0]["body"]["Reason"] == h.NO_VERSION_MSG


def test_exception_reason_redacts_response_url(monkeypatch):
    _, puts = _setup(monkeypatch, cfn_error=RuntimeError(f"bad {URL}"))
    h.handler(_event(), None)
    assert puts[0]["body"]["Status"] == "FAILED"
    assert puts[0]["body"]["Reason"] == "bad <redacted>"
