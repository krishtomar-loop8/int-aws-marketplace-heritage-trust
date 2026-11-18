"""Unit tests for the fulfillment/subscribe landing-page Lambda.

Standalone (no ``backend.*``/``services.*`` imports from the main product's
package tree - see conftest.py) and fully offline: AWS calls are never made,
including ``ResolveCustomer``. ``_dynamo``/``_marketplace_client`` are
monkeypatched to local fakes so the module's top-level functions are swapped
out rather than any real boto3 client ever being constructed.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import handler

# -- fakes --------------------------------------------------------------


class FakeDynamo:
    """Minimal stand-in for boto3's low-level DynamoDB client, matching the
    typed-attribute-map shapes ``handler.py`` sends/expects. Not imported from
    ``backend/api/tests/test_jobs.py``'s ``FakeDynamo`` - that lives inside
    the main product's package tree (``backend.api``), and this directory
    isn't on that package's path (see conftest.py), so a small local copy
    is simpler than fighting the import path for a handful of methods."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def put_item(self, *, TableName: str, Item: dict[str, Any]) -> None:
        self.items[Item["pk"]["S"]] = dict(Item)

    def get_item(self, *, TableName: str, Key: dict[str, Any]) -> dict[str, Any]:
        item = self.items.get(Key["pk"]["S"])
        return {"Item": item} if item else {}

    def update_item(
        self,
        *,
        TableName: str,
        Key: dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ExpressionAttributeNames: dict[str, str] | None = None,
    ) -> None:
        item = self.items[Key["pk"]["S"]]
        names = ExpressionAttributeNames or {}
        assignments = UpdateExpression.replace("SET", "", 1)
        for pair in assignments.split(","):
            lhs, rhs = (p.strip() for p in pair.split("="))
            item[names.get(lhs, lhs)] = ExpressionAttributeValues[rhs]


class FakeMarketplace:
    """Stub for the ``meteringmarketplace`` client's ``resolve_customer``."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[str] = []

    def resolve_customer(self, *, RegistrationToken: str) -> dict[str, Any]:
        self.calls.append(RegistrationToken)
        return dict(self._response)


class FakeMarketplaceThatRejects:
    """Stub whose ``resolve_customer`` raises, like a real ``meteringmarketplace``
    client does for an invalid/expired/replayed Marketplace token
    (``botocore.exceptions.ClientError`` with e.g. ``InvalidTokenException``)."""

    def __init__(self, error_code: str = "InvalidTokenException") -> None:
        self._error_code = error_code
        self.calls: list[str] = []

    def resolve_customer(self, *, RegistrationToken: str) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        self.calls.append(RegistrationToken)
        raise ClientError(
            {"Error": {"Code": self._error_code, "Message": "token is invalid"}},
            "ResolveCustomer",
        )


def http_event(method: str, path: str, *, body: str = "") -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "body": body,
    }


def _seed_session(
    dynamo: FakeDynamo,
    session_id: str,
    *,
    used: bool = False,
    expires_at: int | None = None,
    customer_identifier: str = "cust-1",
    customer_aws_account_id: str = "111122223333",
    product_code: str = "prod-1",
) -> None:
    dynamo.items[f"SESSION#{session_id}"] = {
        "pk": {"S": f"SESSION#{session_id}"},
        "used": {"BOOL": used},
        "expires_at": {"N": str(expires_at if expires_at is not None else int(time.time()) + 1000)},
        "customer_identifier": {"S": customer_identifier},
        "customer_aws_account_id": {"S": customer_aws_account_id},
        "product_code": {"S": product_code},
    }


# -- /fulfillment ---------------------------------------------------------


def test_fulfillment_rejects_non_post() -> None:
    result = handler.handler(http_event("GET", "/fulfillment"), None)
    assert result["statusCode"] in (400, 405)


def test_fulfillment_rejects_missing_token() -> None:
    result = handler.handler(http_event("POST", "/fulfillment", body=""), None)
    assert result["statusCode"] == 400


def test_fulfillment_resolves_token_and_redirects_with_opaque_session(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()
    fake_marketplace = FakeMarketplace(
        {
            "CustomerIdentifier": "cust-123",
            "CustomerAWSAccountId": "111122223333",
            "ProductCode": "prod-abc",
        }
    )
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.delenv("EXPECTED_PRODUCT_CODE", raising=False)
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)
    monkeypatch.setattr(handler, "_marketplace_client", lambda: fake_marketplace)

    result = handler.handler(
        http_event("POST", "/fulfillment", body="x-amzn-marketplace-token=tok-xyz"), None
    )

    assert result["statusCode"] == 302
    location = result["headers"]["Location"]
    assert location.startswith("https://deploy.loop8.io/setup.html?session=")
    session_id = location.split("session=", 1)[1]
    assert session_id != "tok-xyz"  # the opaque session id is not the marketplace token

    item = fake_dynamo.items[f"SESSION#{session_id}"]
    assert item["used"]["BOOL"] is False
    assert int(item["expires_at"]["N"]) > int(time.time())
    assert item["customer_identifier"]["S"] == "cust-123"
    assert fake_marketplace.calls == ["tok-xyz"]  # ResolveCustomer was actually called (stubbed)


def test_fulfillment_redirects_without_session_when_resolve_customer_fails(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()
    fake_marketplace = FakeMarketplaceThatRejects()
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)
    monkeypatch.setattr(handler, "_marketplace_client", lambda: fake_marketplace)

    result = handler.handler(
        http_event("POST", "/fulfillment", body="x-amzn-marketplace-token=tok-bad"), None
    )

    # Plain error state (per the design doc), not a crash: a 302 back to
    # setup.html with no ?session= - the frontend already renders this as
    # "Missing or invalid session".
    assert result["statusCode"] == 302
    assert result["headers"]["Location"] == "https://deploy.loop8.io/setup.html"
    assert fake_dynamo.items == {}  # no session written for a failed resolution
    assert fake_marketplace.calls == ["tok-bad"]  # ResolveCustomer was actually attempted


def test_fulfillment_rejects_product_code_mismatch_when_pinning_is_configured(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()
    fake_marketplace = FakeMarketplace(
        {
            "CustomerIdentifier": "cust-123",
            "CustomerAWSAccountId": "111122223333",
            "ProductCode": "some-other-product",
        }
    )
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.setenv("EXPECTED_PRODUCT_CODE", "expected-product-code")
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)
    monkeypatch.setattr(handler, "_marketplace_client", lambda: fake_marketplace)

    result = handler.handler(
        http_event("POST", "/fulfillment", body="x-amzn-marketplace-token=tok-xyz"), None
    )

    assert 400 <= result["statusCode"] < 500
    assert fake_dynamo.items == {}  # no session written for a rejected product code


def test_fulfillment_skips_product_code_check_when_pinning_is_unconfigured(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()
    fake_marketplace = FakeMarketplace(
        {
            "CustomerIdentifier": "cust-123",
            "CustomerAWSAccountId": "111122223333",
            "ProductCode": "whatever-product-code",
        }
    )
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.delenv("EXPECTED_PRODUCT_CODE", raising=False)
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)
    monkeypatch.setattr(handler, "_marketplace_client", lambda: fake_marketplace)

    result = handler.handler(
        http_event("POST", "/fulfillment", body="x-amzn-marketplace-token=tok-xyz"), None
    )

    assert result["statusCode"] == 302


# -- /subscribe -------------------------------------------------------------


def test_subscribe_rejects_missing_session() -> None:
    result = handler.handler(
        http_event(
            "POST",
            "/subscribe",
            body=json.dumps({"company_name": "Acme", "admin_email": "a@acme.com"}),
        ),
        None,
    )
    assert result["statusCode"] == 400


def test_subscribe_rejects_unknown_or_expired_session(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()  # no session seeded at all - unknown
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)

    result = handler.handler(
        http_event(
            "POST",
            "/subscribe",
            body=json.dumps(
                {"session_id": "nope", "company_name": "Acme", "admin_email": "a@acme.com"}
            ),
        ),
        None,
    )
    assert result["statusCode"] == 400


def test_subscribe_rejects_expired_session(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()
    _seed_session(fake_dynamo, "sid", expires_at=int(time.time()) - 10)
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)

    result = handler.handler(
        http_event(
            "POST",
            "/subscribe",
            body=json.dumps(
                {"session_id": "sid", "company_name": "Acme", "admin_email": "a@acme.com"}
            ),
        ),
        None,
    )
    assert result["statusCode"] == 400


def test_subscribe_rejects_already_used_session(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()
    _seed_session(fake_dynamo, "sid", used=True)
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)

    result = handler.handler(
        http_event(
            "POST",
            "/subscribe",
            body=json.dumps(
                {"session_id": "sid", "company_name": "Acme", "admin_email": "a@acme.com"}
            ),
        ),
        None,
    )
    assert result["statusCode"] == 400


def test_subscribe_writes_subscriber_record_and_marks_session_used(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()
    _seed_session(
        fake_dynamo,
        "sid",
        customer_identifier="cust-1",
        customer_aws_account_id="111122223333",
        product_code="prod-1",
    )
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)

    result = handler.handler(
        http_event(
            "POST",
            "/subscribe",
            body=json.dumps(
                {"session_id": "sid", "company_name": "Acme Co", "admin_email": "a@acme.com"}
            ),
        ),
        None,
    )

    assert result["statusCode"] == 200
    assert fake_dynamo.items["SESSION#sid"]["used"]["BOOL"] is True

    subs = [v for k, v in fake_dynamo.items.items() if k.startswith("SUB#")]
    assert len(subs) == 1
    sub = subs[0]
    assert sub["company_name"]["S"] == "Acme Co"
    assert sub["admin_email"]["S"] == "a@acme.com"
    assert sub["customer_identifier"]["S"] == "cust-1"
    assert sub["customer_aws_account_id"]["S"] == "111122223333"
    assert sub["product_code"]["S"] == "prod-1"


def test_subscribe_returns_a_correctly_encoded_launch_url(monkeypatch) -> None:
    fake_dynamo = FakeDynamo()
    _seed_session(fake_dynamo, "sid")
    monkeypatch.setenv("SUBSCRIPTIONS_TABLE", "subs")
    monkeypatch.setattr(handler, "_dynamo", lambda: fake_dynamo)

    company_name = "Acme & Sons, LLP"
    admin_email = "admin@acme.com"
    result = handler.handler(
        http_event(
            "POST",
            "/subscribe",
            body=json.dumps(
                {"session_id": "sid", "company_name": company_name, "admin_email": admin_email}
            ),
        ),
        None,
    )

    assert result["statusCode"] == 200
    launch_url = json.loads(result["body"])["launch_url"]

    # Everything after "#" is a second, independent query string (CloudFormation's
    # quick-create console convention) - parse it as such rather than the outer one.
    fragment = launch_url.split("#", 1)[1]
    assert fragment.startswith("/stacks/create/review?")
    inner_query = fragment.split("?", 1)[1]
    params = urllib.parse.parse_qs(inner_query)

    assert params["param_CompanyName"][0] == company_name
    # The "&" in company_name must not have corrupted the param that follows it.
    assert params["param_AdminEmail"][0] == admin_email
