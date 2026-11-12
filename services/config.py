"""Deployment business-config access via SSM Parameter Store, with in-memory caching.

The whole config lives in one String parameter as a JSON object, so changes take
effect at runtime without a redeploy.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError


class ConfigClient:
    """Read and merge-write the deployment config JSON object in SSM.

    The client is injectable for testing. A parameter that has never been
    created reads as an empty dict.
    """

    def __init__(
        self,
        client: Any | None = None,
        parameter_name: str = "/aphrodite/config/deployment",
    ) -> None:
        self._client = client or boto3.client("ssm")
        self._parameter_name = parameter_name
        self._cache: dict[str, Any] | None = None

    def _read(self) -> dict[str, Any]:
        try:
            response = self._client.get_parameter(Name=self._parameter_name)
        except ClientError as error:
            if error.response["Error"]["Code"] == "ParameterNotFound":
                return {}
            raise
        return json.loads(response["Parameter"]["Value"])

    def get(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._cache is None:
            self._cache = self._read()
        return self._cache

    def put(self, values: dict[str, Any]) -> None:
        """Merge ``values`` into the stored config and write it back.

        A ``None`` value removes that key. The current stored config is re-read
        (bypassing the cache) so concurrent keys written elsewhere are kept.
        """
        merged = self._read()
        for key, value in values.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        self._client.put_parameter(
            Name=self._parameter_name,
            Value=json.dumps(merged),
            Type="String",
            Overwrite=True,
        )
        self._cache = merged
