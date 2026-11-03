"""Matter listing + on-demand quarterly generation for the portal.

Backs the Quarterly Reports tab's temporary "generate per matter" tool (until the
EventBridge schedule is switched on). Listing is read-only from Clio; generation
async-invokes the already-deployed quarterly-report Lambda (which does the Clio
reads, rendering, and immutable S3 write — this service never writes reports
itself).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

_MATTER_FIELDS = "id,display_number,description,client{id,name}"


@dataclass
class MattersService:
    clio: Any  # ClioConnector

    def list_open(self) -> list[dict[str, Any]]:
        """Open matters for the picker: matter number, description, client name."""
        rows = self.clio.list_matters(status="open", fields=_MATTER_FIELDS)
        out: list[dict[str, Any]] = []
        for m in rows:
            client = m.get("client") or {}
            out.append(
                {
                    "matter_id": str(m.get("id") or ""),
                    "matter_number": str(m.get("display_number") or ""),
                    "description": str(m.get("description") or ""),
                    "client_name": str(client.get("name") or ""),
                }
            )
        return out


@dataclass
class QuarterlyTrigger:
    lambda_client: Any  # boto3 Lambda client
    function_name: str

    def generate(self, matter_id: str, year: int, quarter: int) -> None:
        """Async-invoke the quarterly Lambda for one matter + period (letter date
        defaults to today inside that Lambda)."""
        payload = {
            "matter_ids": [str(matter_id)],
            "period": {"year": int(year), "quarter": int(quarter)},
        }
        self.lambda_client.invoke(
            FunctionName=self.function_name,
            InvocationType="Event",  # async; the SPA polls GET /reports for the result
            Payload=json.dumps(payload).encode("utf-8"),
        )
        logger.info(
            "triggered on-demand quarterly generation",
            extra={"extra_fields": {"matter": str(matter_id), "period": f"{year}-Q{quarter}"}},
        )
