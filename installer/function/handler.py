"""Installer Lambda: CloudFormation custom-resource events.

Inlined into the installer template as ``index.py`` (``Handler: index.handler``), so this
file must stay self-contained: stdlib plus boto3/botocore only.

For each custom-resource event it decides whether to start a CodeBuild run (terraform
apply/destroy), do nothing, or fail, and replies to CloudFormation. For apply/destroy the
reply is sent later by the build itself (via CFN_RESPONSE_URL), so the handler returns
without replying.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SSM_PREFIX = "/aphrodite/installer/"
OUTPUTS_PREFIX = SSM_PREFIX + "outputs/"
LAST_ERROR_PARAM = SSM_PREFIX + "last-error"
STATE_KEY = "aphrodite/terraform.tfstate"

OUTPUT_KEYS = ("AppUrl", "DeployedVersion", "CognitoUserPoolId", "SamlSpEntityId", "SamlAcsUrl")
NOT_DEPLOYED: dict[str, str] = {k: "not-deployed" for k in OUTPUT_KEYS}

_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
CLEAR_VERSION_MSG = "Clearing AphroditeVersion is not supported; delete the stack to uninstall."
NO_VERSION_MSG = (
    "Terraform state exists but no version is set — see installer/README.md 'Manual teardown'."
)


@dataclass
class Deps:
    codebuild: Any
    ssm: Any
    s3: Any
    cfn: Any
    http_put: Callable[[str, bytes], None]
    project_name: str
    state_bucket: str
    artifact_bucket: str


@dataclass
class Decision:
    kind: str  # "noop" | "apply" | "destroy" | "fail"
    version: str = ""
    delete_data: bool = False
    reason: str = ""


def _default_http_put(url: str, body: bytes) -> None:
    # CloudFormation's pre-signed URL requires an empty Content-Type.
    req = urllib.request.Request(url, data=body, method="PUT")  # noqa: S310
    req.add_header("Content-Type", "")
    req.add_header("Content-Length", str(len(body)))
    with urllib.request.urlopen(req, timeout=30):  # noqa: S310
        pass


_DEPS: Deps | None = None  # cold-start cache / test injection point


def _build_deps() -> Deps:
    import boto3

    return Deps(
        codebuild=boto3.client("codebuild"),
        ssm=boto3.client("ssm"),
        s3=boto3.client("s3"),
        cfn=boto3.client("cloudformation"),
        http_put=_default_http_put,
        project_name=os.environ["PROJECT_NAME"],
        state_bucket=os.environ["STATE_BUCKET"],
        artifact_bucket=os.environ["ARTIFACT_BUCKET"],
    )


def _get_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_deps()
    return _DEPS


def _stack_parts(stack_id: str) -> tuple[str, str]:
    """Return (region, account) from arn:aws:cloudformation:<region>:<account>:stack/..."""
    parts = stack_id.split(":")
    return parts[3], parts[4]


def physical_id(stack_id: str) -> str:
    region, account = _stack_parts(stack_id)
    return f"aphrodite-{account}-{region}"


def decide(
    request_type: str,
    props: dict,
    old_props: dict,
    stack_status: str,
    state_exists: bool,
) -> Decision:
    version = props.get("Version") or ""
    old_version = old_props.get("Version") or ""

    if request_type == "Create":
        return Decision("apply", version) if version else Decision("noop")

    if request_type == "Update":
        if version:
            return Decision("apply", version)
        if not old_version:
            return Decision("noop")
        if stack_status == "UPDATE_ROLLBACK_IN_PROGRESS":
            return Decision("destroy", old_version, True)
        return Decision("fail", reason=CLEAR_VERSION_MSG)

    if request_type == "Delete":
        if stack_status == "ROLLBACK_IN_PROGRESS":
            return Decision("destroy", version, True) if state_exists else Decision("noop")
        if not state_exists:
            return Decision("noop")
        if not version:
            return Decision("fail", reason=NO_VERSION_MSG)
        return Decision("destroy", version, props.get("DeleteData") == "true")

    return Decision("fail", reason=f"Unknown RequestType {request_type}")


def reply(event_like: dict, status: str, reason: str, data: dict) -> None:
    body = {
        "Status": status,
        "Reason": reason,
        "PhysicalResourceId": physical_id(event_like["StackId"]),
        "StackId": event_like["StackId"],
        "RequestId": event_like["RequestId"],
        "LogicalResourceId": event_like["LogicalResourceId"],
        "Data": data,
    }
    logger.info("Replying %s to CloudFormation", status)  # never log the response URL
    _get_deps().http_put(event_like["ResponseURL"], json.dumps(body).encode())


def _is_missing(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code in _MISSING_CODES


def _object_exists(deps: Deps, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        deps.s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _is_missing(exc):
            return False
        raise
    return True


def _stored_outputs(deps: Deps) -> dict[str, str]:
    out: dict[str, str] = {}
    kwargs: dict[str, Any] = {"Path": OUTPUTS_PREFIX}
    while True:
        resp = deps.ssm.get_parameters_by_path(**kwargs)
        for p in resp.get("Parameters", []):
            out[p["Name"].removeprefix(OUTPUTS_PREFIX)] = p["Value"]
        token = resp.get("NextToken")
        if not token:
            return out
        kwargs["NextToken"] = token


def _build_in_progress(deps: Deps) -> bool:
    ids = deps.codebuild.list_builds_for_project(
        projectName=deps.project_name, sortOrder="DESCENDING"
    ).get("ids", [])[:5]
    if not ids:
        return False
    builds = deps.codebuild.batch_get_builds(ids=ids).get("builds", [])
    return any(b.get("buildStatus") == "IN_PROGRESS" for b in builds)


def _clear_last_error(deps: Deps) -> None:
    from botocore.exceptions import ClientError

    try:
        deps.ssm.delete_parameter(Name=LAST_ERROR_PARAM)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise


def _on_custom_resource(event: dict) -> None:
    deps = _get_deps()
    request_type = event["RequestType"]
    props = event.get("ResourceProperties") or {}
    old_props = event.get("OldResourceProperties") or {}
    stack_id = event["StackId"]

    status = deps.cfn.describe_stacks(StackName=stack_id)["Stacks"][0]["StackStatus"]
    state_exists = _object_exists(deps, deps.state_bucket, STATE_KEY)
    decision = decide(request_type, props, old_props, status, state_exists)
    logger.info("RequestType=%s status=%s decision=%s", request_type, status, decision.kind)

    if decision.kind == "noop":
        stored = _stored_outputs(deps)
        reply(event, "SUCCESS", "", {**NOT_DEPLOYED, **stored})
        return
    if decision.kind == "fail":
        reply(event, "FAILED", decision.reason, NOT_DEPLOYED)
        return

    if not props.get("BundleSha256"):
        reply(event, "FAILED", "BundleSha256 is required", NOT_DEPLOYED)
        return

    version = decision.version
    key = f"releases/{version}/aphrodite-{version}.zip"
    if not _object_exists(deps, deps.artifact_bucket, key):
        reason = (
            f"Release {version} has not been uploaded to {deps.artifact_bucket} "
            f"(expected key {key})"
        )
        reply(event, "FAILED", reason, NOT_DEPLOYED)
        return

    if _build_in_progress(deps):
        reply(event, "FAILED", "a deployment is already in progress", NOT_DEPLOYED)
        return

    region, _ = _stack_parts(stack_id)
    env = {
        "ACTION": decision.kind,
        "VERSION": version,
        "BUNDLE_SHA256": props.get("BundleSha256", ""),
        "DELETE_DATA": "true" if decision.delete_data else "false",
        "TF_VAR_aws_region": region,
        "TF_VAR_environment": props.get("Environment", ""),
        "TF_VAR_saml_metadata_url": props.get("SamlMetadataUrl", ""),
        "CFN_RESPONSE_URL": event["ResponseURL"],
        "CFN_STACK_ID": stack_id,
        "CFN_REQUEST_ID": event["RequestId"],
        "CFN_LOGICAL_ID": event["LogicalResourceId"],
        "CFN_PHYSICAL_ID": physical_id(stack_id),
    }
    deps.codebuild.start_build(
        projectName=deps.project_name,
        environmentVariablesOverride=[
            {"name": k, "value": v, "type": "PLAINTEXT"} for k, v in env.items()
        ],
    )
    logger.info("Started %s build for version %s", decision.kind, version)
    _clear_last_error(deps)


def on_build_finished(event: dict) -> None:
    logger.info("CodeBuild state-change handling not implemented")


def handler(event: dict, context: Any = None) -> None:
    if "RequestType" in event:
        try:
            _on_custom_resource(event)
        except Exception as exc:
            url = event.get("ResponseURL", "")
            msg = str(exc).replace(url, "<redacted>") if url else str(exc)
            logger.error("Custom resource failed: %s: %s", type(exc).__name__, msg[:500])
            reply(event, "FAILED", str(exc)[:500], NOT_DEPLOYED)
    elif event.get("source") == "aws.codebuild":
        on_build_finished(event)
    else:
        logger.warning("Ignoring unrecognised event")
