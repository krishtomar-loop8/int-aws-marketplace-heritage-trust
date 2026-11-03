"""Secrets Manager access with in-memory caching."""

from __future__ import annotations

import json
from typing import Any

import boto3


class SecretsClient:
    """Fetch and cache JSON secrets from AWS Secrets Manager.

    The client is injectable for testing. Secret values are expected to be JSON
    objects (the connector credentials), returned as dicts.
    """

    def __init__(self, client: Any | None = None) -> None:
        self._client = client or boto3.client("secretsmanager")
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh and name in self._cache:
            return self._cache[name]
        response = self._client.get_secret_value(SecretId=name)
        secret = json.loads(response["SecretString"])
        self._cache[name] = secret
        return secret

    def put(self, name: str, secret: dict[str, Any]) -> None:
        """Write a JSON secret back to Secrets Manager and refresh the cache.

        Used to persist OAuth refresh tokens that the provider rotates (e.g.
        QuickBooks), so the rotated value survives a Lambda cold start rather
        than living only in this process's cache.
        """
        self._client.put_secret_value(SecretId=name, SecretString=json.dumps(secret))
        self._cache[name] = secret
