"""On-demand AML export trigger + self-serve monthly schedule for the portal.

Backs the AML Export tab's "Run export now" button and schedule card:

- ``AmlExportTrigger`` async-invokes the already-deployed AML export Lambda (D6)
  — the exact pattern as the quarterly ``QuarterlyTrigger``; the Lambda ignores
  its event and runs the full monthly export.
- ``AmlScheduleService`` reads and updates the EventBridge Scheduler schedule
  that fires that Lambda, so staff can enable/disable it and set the monthly
  day + time from the UI without a code change.

Neither performs the export itself — the aml_export Lambda owns that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

# Day-of-month is capped at 28 so a monthly run never skips a short month.
_MAX_DOM = 28
_DEFAULT_TZ = "America/Vancouver"


class ScheduleError(Exception):
    """Invalid schedule input (day/hour/minute out of range)."""


@dataclass
class AmlExportTrigger:
    lambda_client: Any  # boto3 Lambda client
    function_name: str

    def run(self) -> None:
        """Async-invoke the AML export Lambda (it ignores its event)."""
        self.lambda_client.invoke(
            FunctionName=self.function_name,
            InvocationType="Event",  # async; the SPA polls GET /aml-exports for the result
            Payload=b"{}",
        )
        logger.info(
            "triggered on-demand AML export",
            extra={"extra_fields": {"function": self.function_name}},
        )


@dataclass
class AmlScheduleService:
    scheduler: Any  # boto3 "scheduler" client
    schedule_name: str
    group_name: str = "default"

    def get(self) -> dict[str, Any]:
        """Current schedule state as {enabled, day_of_month, hour, minute, timezone}."""
        sched = self.scheduler.get_schedule(Name=self.schedule_name, GroupName=self.group_name)
        minute, hour, dom = _parse_cron(sched.get("ScheduleExpression", ""))
        return {
            "enabled": sched.get("State") == "ENABLED",
            "day_of_month": dom,
            "hour": hour,
            "minute": minute,
            "timezone": sched.get("ScheduleExpressionTimezone", _DEFAULT_TZ),
        }

    def update(
        self, *, enabled: bool, day_of_month: int, hour: int, minute: int
    ) -> dict[str, Any]:
        """Set the monthly cadence + enabled state; returns the new state.

        EventBridge Scheduler ``UpdateSchedule`` is a full replace, so we read the
        current schedule first to preserve the target, role, flexible-time-window
        and timezone, and only change the cron expression + state.
        """
        dom, hh, mm = _validate(day_of_month, hour, minute)
        current = self.scheduler.get_schedule(Name=self.schedule_name, GroupName=self.group_name)
        self.scheduler.update_schedule(
            Name=self.schedule_name,
            GroupName=self.group_name,
            ScheduleExpression=f"cron({mm} {hh} {dom} * ? *)",
            ScheduleExpressionTimezone=current.get("ScheduleExpressionTimezone", _DEFAULT_TZ),
            State="ENABLED" if enabled else "DISABLED",
            FlexibleTimeWindow=current["FlexibleTimeWindow"],
            Target=current["Target"],
        )
        logger.info(
            "updated AML schedule",
            extra={
                "extra_fields": {
                    "enabled": enabled,
                    "day_of_month": dom,
                    "time": f"{hh:02d}:{mm:02d}",
                }
            },
        )
        return self.get()


def _parse_cron(expr: str) -> tuple[int, int, int]:
    """``cron(minute hour day-of-month month day-of-week year)`` → (minute, hour, dom).

    Tolerant of non-numeric fields (e.g. ``*``): falls back to 0/0/1 so a hand-set
    expression can never 500 the schedule read.
    """
    try:
        inner = expr[expr.index("(") + 1 : expr.rindex(")")]
        fields = inner.split()
    except ValueError:
        return 0, 0, 1

    def num(idx: int, default: int) -> int:
        try:
            return int(fields[idx])
        except (IndexError, ValueError):
            return default

    return num(0, 0), num(1, 0), num(2, 1)


def _validate(day_of_month: int, hour: int, minute: int) -> tuple[int, int, int]:
    try:
        dom, hh, mm = int(day_of_month), int(hour), int(minute)
    except (TypeError, ValueError) as exc:
        raise ScheduleError("day_of_month, hour and minute must be integers") from exc
    if not 1 <= dom <= _MAX_DOM:
        raise ScheduleError(f"day_of_month must be between 1 and {_MAX_DOM}")
    if not 0 <= hh <= 23:
        raise ScheduleError("hour must be between 0 and 23")
    if not 0 <= mm <= 59:
        raise ScheduleError("minute must be between 0 and 59")
    return dom, hh, mm
