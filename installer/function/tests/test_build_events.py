"""Tests for the installer Lambda's CodeBuild build-finished handling (fake AWS clients)."""

import json
import logging

from botocore.exceptions import ClientError

from installer.function import handler as h

URL = "https://cfn-response.example.com/signed?X-Amz-Signature=secret123"
STACK_ID = "arn:aws:cloudformation:ca-central-1:123456789012:stack/aphrodite/abcd-1234"
BUILD_ID = "aphrodite-deploy:1111-2222"
BUILD_ARN = f"arn:aws:codebuild:ca-central-1:123456789012:build/{BUILD_ID}"
DEEP_LINK = (
    "https://console.aws.amazon.com/cloudwatch/home?region=ca-central-1"
    "#logEvent:group=/aws/codebuild/aphrodite-deploy;stream=1111-2222"
)
OUTPUTS = {
    "AppUrl": "https://d123.cloudfront.net",
    "DeployedVersion": "1.0.0",
    "CognitoUserPoolId": "ca-central-1_abc",
    "SamlSpEntityId": "urn:amazon:cognito:sp:ca-central-1_abc",
    "SamlAcsUrl": "https://aphrodite.auth.ca-central-1.amazoncognito.com/saml2/idpresponse",
}


def _client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeCodebuild:
    def __init__(self, env: dict[str, str]):
        self.env = env
        self.requested: list[list[str]] = []

    def batch_get_builds(self, ids):
        self.requested.append(ids)
        variables = [{"name": k, "value": v, "type": "PLAINTEXT"} for k, v in self.env.items()]
        build = {
            "id": BUILD_ID,
            "arn": BUILD_ARN,
            "environment": {"environmentVariables": variables},
            "logs": {"deepLink": DEEP_LINK},
        }
        return {"builds": [build]}


class FakeSsm:
    def __init__(self, params: dict[str, str]):
        self.params = params

    def get_parameter(self, Name):  # noqa: N803
        if Name not in self.params:
            raise _client_error("ParameterNotFound", "GetParameter")
        return {"Parameter": {"Name": Name, "Value": self.params[Name]}}

    def get_parameters(self, Names):  # noqa: N803
        found = [{"Name": n, "Value": self.params[n]} for n in Names if n in self.params]
        missing = [n for n in Names if n not in self.params]
        return {"Parameters": found, "InvalidParameters": missing}


def _env(action="apply", **extra):
    env = {
        "ACTION": action,
        "VERSION": "1.0.0",
        "DELETE_DATA": "false",
        "CFN_RESPONSE_URL": URL,
        "CFN_STACK_ID": STACK_ID,
        "CFN_REQUEST_ID": "req-1",
        "CFN_LOGICAL_ID": "Deployment",
        "CFN_PHYSICAL_ID": "aphrodite-123456789012-ca-central-1",
    }
    env.update(extra)
    return env


def _last_error(build_id: str, message: str) -> dict[str, str]:
    return {h.LAST_ERROR_PARAM: json.dumps({"build_id": build_id, "message": message})}


def _setup(monkeypatch, env, params=None):
    puts: list[dict] = []
    deps = h.Deps(
        codebuild=FakeCodebuild(env),
        ssm=FakeSsm(dict(params or {})),
        s3=None,
        cfn=None,
        http_put=lambda url, body: puts.append({"url": url, "body": json.loads(body)}),
        project_name="aphrodite-deploy",
        state_bucket="state-bkt",
        artifact_bucket="artifacts-bkt",
    )
    monkeypatch.setattr(h, "_DEPS", deps)
    return deps, puts


def _event(status="SUCCEEDED"):
    return {
        "source": "aws.codebuild",
        "detail-type": "CodeBuild Build State Change",
        "detail": {
            "build-id": BUILD_ARN,
            "build-status": status,
            "project-name": "aphrodite-deploy",
        },
    }


def _outputs_params():
    return {h.OUTPUTS_PREFIX + k: v for k, v in OUTPUTS.items()}


def test_success_replies_with_ssm_outputs(monkeypatch):
    deps, puts = _setup(monkeypatch, _env(), _outputs_params())
    h.handler(_event(), None)
    assert deps.codebuild.requested == [[BUILD_ARN]]
    assert len(puts) == 1
    assert puts[0]["url"] == URL
    body = puts[0]["body"]
    assert body["Status"] == "SUCCESS"
    assert body["Data"] == OUTPUTS
    assert body["StackId"] == STACK_ID
    assert body["RequestId"] == "req-1"
    assert body["LogicalResourceId"] == "Deployment"
    assert body["PhysicalResourceId"] == "aphrodite-123456789012-ca-central-1"


def test_destroy_success_replies_not_deployed(monkeypatch):
    _, puts = _setup(monkeypatch, _env("destroy"), _outputs_params())
    h.handler(_event(), None)
    assert puts[0]["body"]["Status"] == "SUCCESS"
    assert puts[0]["body"]["Data"] == h.NOT_DEPLOYED


def test_failure_uses_matching_last_error(monkeypatch):
    msg = "Bedrock model x is not accessible in ca-central-1."
    _, puts = _setup(monkeypatch, _env(), _last_error(BUILD_ID, msg))
    h.handler(_event("FAILED"), None)
    body = puts[0]["body"]
    assert body["Status"] == "FAILED"
    assert body["Reason"].startswith(msg)
    assert DEEP_LINK in body["Reason"]
    assert body["Data"] == h.NOT_DEPLOYED


def test_failure_ignores_stale_last_error(monkeypatch):
    _, puts = _setup(monkeypatch, _env(), _last_error("aphrodite-deploy:old", "old problem"))
    h.handler(_event("FAILED"), None)
    reason = puts[0]["body"]["Reason"]
    assert reason.startswith("Deployment failed")
    assert "old problem" not in reason
    assert DEEP_LINK in reason


def test_failure_without_last_error_is_generic(monkeypatch):
    _, puts = _setup(monkeypatch, _env())
    h.handler(_event("FAILED"), None)
    assert puts[0]["body"]["Status"] == "FAILED"
    assert puts[0]["body"]["Reason"].startswith("Deployment failed")


def test_stopped_build_fails(monkeypatch):
    _, puts = _setup(monkeypatch, _env(), _outputs_params())
    h.handler(_event("STOPPED"), None)
    assert puts[0]["body"]["Status"] == "FAILED"


def test_manual_build_without_response_url_is_ignored(monkeypatch):
    env = _env()
    del env["CFN_RESPONSE_URL"]
    _, puts = _setup(monkeypatch, env, _outputs_params())
    h.handler(_event(), None)
    assert puts == []


def test_reason_truncated(monkeypatch):
    _, puts = _setup(monkeypatch, _env(), _last_error(BUILD_ID, "x" * 5000))
    h.handler(_event("FAILED"), None)
    reason = puts[0]["body"]["Reason"]
    assert len(reason) <= 900
    assert reason.endswith(DEEP_LINK)


def test_response_url_never_logged_on_build_events(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _setup(monkeypatch, _env(), _outputs_params())
    h.handler(_event(), None)
    _setup(monkeypatch, _env(), _last_error(BUILD_ID, "boom"))
    h.handler(_event("FAILED"), None)
    assert caplog.records
    assert "secret123" not in caplog.text
