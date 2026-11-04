"""Installer Lambda: CloudFormation custom-resource events.

Inlined into the installer template as ``index.py`` (``Handler: index.handler``), so this
file must stay self-contained: stdlib plus boto3/botocore only.

For each custom-resource event it decides whether to start a CodeBuild run (terraform
apply/destroy), do nothing, or fail, and replies to CloudFormation. For apply/destroy the
reply is sent later by the build itself (via CFN_RESPONSE_URL), so the handler returns
without replying: when the build finishes, its EventBridge state-change event invokes this
function again (``on_build_finished``), which replies using the reply details stored in the
build's environment variables.
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
BUILD_FAILED_MSG = "Deployment failed"
MAX_REASON = 900
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
    bundle_sha256: str = ""
    from_old: bool = False  # version/sha/build settings come from OldResourceProperties


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
    sha = props.get("BundleSha256") or ""
    old_sha = old_props.get("BundleSha256") or ""

    if request_type == "Create":
        if not version:
            return Decision("noop")
        return Decision("apply", version, bundle_sha256=sha)

    if request_type == "Update":
        if version:
            return Decision("apply", version, bundle_sha256=sha)
        if not old_version:
            return Decision("noop")
        if stack_status == "UPDATE_ROLLBACK_IN_PROGRESS":
            if not state_exists:
                return Decision("noop")  # nothing was deployed, nothing to tear down
            return Decision("destroy", old_version, True, bundle_sha256=old_sha, from_old=True)
        return Decision("fail", reason=CLEAR_VERSION_MSG)

    if request_type == "Delete":
        if stack_status == "ROLLBACK_IN_PROGRESS":
            if not state_exists:
                return Decision("noop")
            return Decision("destroy", version, True, bundle_sha256=sha)
        if not state_exists:
            return Decision("noop")
        if not version:
            return Decision("fail", reason=NO_VERSION_MSG)
        return Decision("destroy", version, props.get("DeleteData") == "true", bundle_sha256=sha)

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


def _state_exists(deps: Deps) -> bool:
    """True if the Terraform state tracks any resource.

    ``terraform destroy`` leaves an empty state object behind, which is not a deployment.
    A state that cannot be parsed counts as existing, so nothing is skipped by mistake.
    """
    from botocore.exceptions import ClientError

    try:
        body = deps.s3.get_object(Bucket=deps.state_bucket, Key=STATE_KEY)["Body"].read()
    except ClientError as exc:
        if _is_missing(exc):
            return False
        raise
    try:
        doc = json.loads(body)
    except ValueError:
        return True
    if not isinstance(doc, dict) or not isinstance(doc.get("resources"), list):
        return True
    return bool(doc["resources"])


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
    state_exists = _state_exists(deps)
    decision = decide(request_type, props, old_props, status, state_exists)
    logger.info("RequestType=%s status=%s decision=%s", request_type, status, decision.kind)

    if decision.kind == "noop":
        stored = _stored_outputs(deps)
        reply(event, "SUCCESS", "", {**NOT_DEPLOYED, **stored})
        return
    if decision.kind == "fail":
        reply(event, "FAILED", decision.reason, NOT_DEPLOYED)
        return

    # Build settings must come from the same property set the version was taken from.
    src = old_props if decision.from_old else props
    if not decision.bundle_sha256:
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
        "BUNDLE_SHA256": decision.bundle_sha256,
        "DELETE_DATA": "true" if decision.delete_data else "false",
        "TF_VAR_aws_region": region,
        "TF_VAR_environment": src.get("Environment", ""),
        "TF_VAR_saml_metadata_url": src.get("SamlMetadataUrl", ""),
        "CFN_RESPONSE_URL": event["ResponseURL"],
        "CFN_STACK_ID": stack_id,
        "CFN_REQUEST_ID": event["RequestId"],
        "CFN_LOGICAL_ID": event["LogicalResourceId"],
        "CFN_PHYSICAL_ID": physical_id(stack_id),
    }
    # Clear before starting: any failure here must fail the reply while no build exists.
    _clear_last_error(deps)
    deps.codebuild.start_build(
        projectName=deps.project_name,
        environmentVariablesOverride=[
            {"name": k, "value": v, "type": "PLAINTEXT"} for k, v in env.items() if v != ""
        ],
    )
    logger.info("Started %s build for version %s", decision.kind, version)


def _failure_message(deps: Deps, build_ids: set[str]) -> str:
    """The deploy script's last-error message if it belongs to this build, else generic."""
    from botocore.exceptions import ClientError

    try:
        raw = deps.ssm.get_parameter(Name=LAST_ERROR_PARAM)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise
        return BUILD_FAILED_MSG
    try:
        last = json.loads(raw)
    except ValueError:
        return BUILD_FAILED_MSG
    if isinstance(last, dict) and last.get("build_id") in build_ids and last.get("message"):
        return str(last["message"])
    return BUILD_FAILED_MSG


def _failure_reason(message: str, deep_link: str) -> str:
    # Keep the log link intact: trim the message, not the link.
    suffix = f" — logs: {deep_link}" if deep_link else ""
    return (message[: max(0, MAX_REASON - len(suffix))] + suffix)[:MAX_REASON]


def _deployed_outputs(deps: Deps) -> dict[str, str]:
    names = [OUTPUTS_PREFIX + k for k in OUTPUT_KEYS]
    resp = deps.ssm.get_parameters(Names=names)
    found = {p["Name"].removeprefix(OUTPUTS_PREFIX): p["Value"] for p in resp["Parameters"]}
    if resp.get("InvalidParameters"):
        logger.warning("Missing deployment outputs in SSM: %s", resp["InvalidParameters"])
    return {**NOT_DEPLOYED, **found}


def on_build_finished(event: dict) -> None:
    deps = _get_deps()
    detail = event.get("detail") or {}
    build_id = detail.get("build-id")
    status = detail.get("build-status")
    if not build_id:
        logger.warning("CodeBuild event without a build-id; ignoring")
        return

    builds = deps.codebuild.batch_get_builds(ids=[build_id]).get("builds", [])
    if not builds:
        logger.warning("build %s not found; ignoring", build_id)
        return
    build = builds[0]
    env = {
        v["name"]: v.get("value", "")
        for v in build.get("environment", {}).get("environmentVariables", [])
    }
    if not env.get("CFN_RESPONSE_URL"):
        logger.info("build %s not started by CloudFormation; ignoring", build_id)
        return

    event_like = {
        "ResponseURL": env["CFN_RESPONSE_URL"],
        "StackId": env["CFN_STACK_ID"],
        "RequestId": env["CFN_REQUEST_ID"],
        "LogicalResourceId": env["CFN_LOGICAL_ID"],
    }
    action = env.get("ACTION", "")
    logger.info("build %s (%s) finished with status %s", build_id, action, status)

    if status == "SUCCEEDED":
        data = _deployed_outputs(deps) if action == "apply" else NOT_DEPLOYED
        reply(event_like, "SUCCESS", "", data)
        return

    # last-error carries CODEBUILD_BUILD_ID ("<project>:<uuid>"); the event carries the ARN.
    message = _failure_message(deps, {build_id, build.get("id", ""), build.get("arn", "")} - {""})
    reason = _failure_reason(message, build.get("logs", {}).get("deepLink", ""))
    reply(event_like, "FAILED", reason, NOT_DEPLOYED)


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
