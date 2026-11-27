"""Lambda entry point for the AWS Marketplace landing-page fulfillment/subscribe API.

Two HTTP routes, dispatched the same way ``backend/api/handlers.py`` dispatches
its own routes (method + path off ``event["requestContext"]["http"]``):

- ``POST /fulfillment`` - AWS Marketplace POSTs the buyer's browser here with
  ``x-amzn-marketplace-token`` in a form-encoded body. The token is resolved
  server-side (``aws-marketplace:ResolveCustomer`` - the only authentication
  this flow has; the token is never trusted at face value). On success, a
  short-lived pending-resolution record is written and the browser is
  302-redirected to the static site with an opaque session id in the query
  string - never the Marketplace token itself.
- ``POST /subscribe`` - the static site's form submission (``session_id``,
  ``company_name``, ``admin_email``). The pending record is looked up,
  checked unexpired/unused, the durable subscriber record is written, the
  session is marked used, and a CloudFormation quick-create "Launch Stack"
  URL pre-filled with the buyer's values is returned.

See docs/superpowers/specs/2026-11-17-marketplace-landing-page-design.md for
the full design. This module has no dependency on ``backend/`` or
``services/`` (the main product's packages) - it is a standalone Lambda for
Loop8's own landing-page stack (``loop8-landing/terraform/``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
import uuid
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# -- small, named constants (not magic strings buried in the function bodies) -

# Region the buyer's own CloudFormation stack deploys into by default. This is
# unrelated to Loop8's own landing-stack region (loop8-landing/terraform's
# var.aws_region, which runs this very Lambda) - it's the customer-deployed
# product's region, defaulting to the product's own stated default (CLAUDE.md).
REGION = os.environ.get("LAUNCH_REGION", "ca-central-1")

# Public URL of the mirrored installer template (loop8-landing/terraform/api.tf's
# installer_mirror bucket, "installer/latest/installer.yaml" key - always the
# latest stable release per the design doc's anti-goal against pinning a
# version in code). Terraform/CI wires the real value once the bucket name
# (which embeds the account id) and release workflow exist; this default is
# only a placeholder for local/test runs.
TEMPLATE_URL = os.environ.get(
    "INSTALLER_TEMPLATE_URL",
    "https://loop8-landing-installer-mirror.s3.amazonaws.com/installer/latest/installer.yaml",
)

STACK_NAME = os.environ.get("LAUNCH_STACK_NAME", "Aphrodite")

# The SPA page Task 4 builds. ".html" (not the design doc's original
# extensionless "/setup") - a deliberate controller ruling made during Task 2's
# review to avoid needing a CloudFront rewrite rule.
SETUP_URL = "https://deploy.loop8.io/setup.html"

# Pending-resolution sessions expire ~30 minutes after fulfillment (design doc).
SESSION_TTL_SECONDS = 30 * 60


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _redirect(location: str) -> dict[str, Any]:
    return {"statusCode": 302, "headers": {"Location": location}}


def _raw_body(event: dict[str, Any]) -> str:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return raw


def _json_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = _raw_body(event)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _table_name() -> str:
    return os.environ["SUBSCRIPTIONS_TABLE"]


def _dynamo() -> Any:
    """The raw (low-level) DynamoDB client. Tests monkeypatch this function
    directly (not a cached singleton), so a fake can be swapped in per test
    without ever touching AWS."""
    import boto3

    return boto3.client("dynamodb")


def _marketplace_client() -> Any:
    """The AWS Marketplace Metering client, whose ``resolve_customer`` method
    is boto3's binding for the ``ResolveCustomer`` API. Tests monkeypatch this
    function directly so ``ResolveCustomer`` is never actually called."""
    import boto3

    return boto3.client("meteringmarketplace")


def _handle_fulfillment(event: dict[str, Any]) -> dict[str, Any]:
    fields = urllib.parse.parse_qs(_raw_body(event))
    token = (fields.get("x-amzn-marketplace-token") or [""])[0]
    if not token:
        return _response(400, {"error": "missing x-amzn-marketplace-token"})

    from botocore.exceptions import ClientError

    try:
        result = _marketplace_client().resolve_customer(RegistrationToken=token)
    except ClientError as exc:
        # An invalid/expired/replayed Marketplace token raises here (e.g.
        # InvalidTokenException/ExpiredTokenException). Per the design doc,
        # this must surface as a plain error state, not a crash - redirect to
        # the same setup page with no ?session=, which the frontend already
        # renders as a "Missing or invalid session" error.
        logger.warning(
            "ResolveCustomer failed: %s: %s",
            exc.response.get("Error", {}).get("Code", "Unknown"),
            exc.response.get("Error", {}).get("Message", str(exc)),
        )
        return _redirect(SETUP_URL)

    product_code = result.get("ProductCode", "")
    expected_product_code = os.environ.get("EXPECTED_PRODUCT_CODE", "").strip()
    if expected_product_code and product_code != expected_product_code:
        return _response(403, {"error": "product code does not match this listing"})
    if not expected_product_code:
        logger.info("EXPECTED_PRODUCT_CODE is unset - skipping product-code pinning check")

    session_id = str(uuid.uuid4())
    now = int(time.time())
    _dynamo().put_item(
        TableName=_table_name(),
        Item={
            "pk": {"S": f"SESSION#{session_id}"},
            "customer_identifier": {"S": result.get("CustomerIdentifier", "")},
            "customer_aws_account_id": {"S": result.get("CustomerAWSAccountId", "")},
            "product_code": {"S": product_code},
            "used": {"BOOL": False},
            "expires_at": {"N": str(now + SESSION_TTL_SECONDS)},
        },
    )
    location = f"{SETUP_URL}?{urllib.parse.urlencode({'session': session_id})}"
    return _redirect(location)


def _build_launch_url(company_name: str, admin_email: str) -> str:
    """AWS's own CloudFormation quick-create convention: everything after the
    ``#`` is itself a second, independent query string (the console's own
    routing path plus a nested query of its own) - built as its own
    ``urlencode`` call, never hand-rolled concatenation, since a buyer's own
    ``&``/space/etc. in ``company_name`` would otherwise corrupt the params
    that follow it."""
    inner_query = urllib.parse.urlencode(
        {
            "templateURL": TEMPLATE_URL,
            "stackName": STACK_NAME,
            "param_CompanyName": company_name,
            "param_AdminEmail": admin_email,
        }
    )
    outer_query = urllib.parse.urlencode({"region": REGION})
    return (
        "https://console.aws.amazon.com/cloudformation/home"
        f"?{outer_query}#/stacks/create/review?{inner_query}"
    )


def _handle_subscribe(event: dict[str, Any]) -> dict[str, Any]:
    body = _json_body(event)
    session_id = str(body.get("session_id") or "").strip()
    company_name = str(body.get("company_name") or "").strip()
    admin_email = str(body.get("admin_email") or "").strip()
    if not session_id:
        return _response(400, {"error": "missing session_id"})
    if not company_name or not admin_email:
        return _response(400, {"error": "missing company_name or admin_email"})

    dynamo = _dynamo()
    table = _table_name()
    resp = dynamo.get_item(TableName=table, Key={"pk": {"S": f"SESSION#{session_id}"}})
    item = resp.get("Item")
    if not item:
        return _response(400, {"error": "unknown or expired session"})
    if int(item.get("expires_at", {}).get("N", "0")) < int(time.time()):
        return _response(400, {"error": "unknown or expired session"})
    if item.get("used", {}).get("BOOL"):
        return _response(400, {"error": "session already used"})

    now = int(time.time())
    sub_id = str(uuid.uuid4())
    dynamo.put_item(
        TableName=table,
        Item={
            "pk": {"S": f"SUB#{sub_id}"},
            "company_name": {"S": company_name},
            "admin_email": {"S": admin_email},
            "customer_identifier": item.get("customer_identifier", {"S": ""}),
            "customer_aws_account_id": item.get("customer_aws_account_id", {"S": ""}),
            "product_code": item.get("product_code", {"S": ""}),
            "created_at": {"N": str(now)},
        },
    )
    dynamo.update_item(
        TableName=table,
        Key={"pk": {"S": f"SESSION#{session_id}"}},
        UpdateExpression="SET used = :u",
        ExpressionAttributeValues={":u": {"BOOL": True}},
    )

    return _response(200, {"launch_url": _build_launch_url(company_name, admin_email)})


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", ""), http.get("path", "")

    if method == "POST" and path.endswith("/fulfillment"):
        return _handle_fulfillment(event)

    if method == "POST" and path.endswith("/subscribe"):
        return _handle_subscribe(event)

    return _response(400, {"error": "unsupported method or path"})
