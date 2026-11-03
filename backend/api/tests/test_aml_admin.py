"""Tests for the AML on-demand trigger + schedule service (fake boto3 clients)."""

import pytest

from backend.api.aml_admin import AmlExportTrigger, AmlScheduleService, ScheduleError


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {"StatusCode": 202}


class FakeScheduler:
    def __init__(self, expr="cron(0 0 1 * ? *)", state="DISABLED"):
        self.sched = {
            "Name": "aphrodite-aml-export",
            "GroupName": "default",
            "ScheduleExpression": expr,
            "ScheduleExpressionTimezone": "America/Vancouver",
            "State": state,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {
                "Arn": "arn:aws:lambda:ca-central-1:1:function:aphrodite-aml-export",
                "RoleArn": "arn:aws:iam::1:role/aphrodite-aml-export-scheduler",
            },
            "Arn": "arn:aws:scheduler:ca-central-1:1:schedule/default/aphrodite-aml-export",
        }
        self.updated = None

    def get_schedule(self, Name, GroupName):  # noqa: N803 - boto3 kwarg names
        return dict(self.sched)

    def update_schedule(self, **kwargs):
        self.updated = kwargs
        self.sched["ScheduleExpression"] = kwargs["ScheduleExpression"]
        self.sched["State"] = kwargs["State"]


def test_trigger_fires_empty_async_invoke():
    lam = FakeLambda()
    AmlExportTrigger(lam, "aphrodite-aml-export").run()
    assert len(lam.calls) == 1
    call = lam.calls[0]
    assert call["FunctionName"] == "aphrodite-aml-export"
    assert call["InvocationType"] == "Event"
    assert call["Payload"] == b"{}"


def test_schedule_get_parses_cron_and_state():
    sched = FakeScheduler("cron(30 6 15 * ? *)", state="ENABLED")
    svc = AmlScheduleService(sched, "aphrodite-aml-export")
    state = svc.get()
    assert state == {
        "enabled": True,
        "day_of_month": 15,
        "hour": 6,
        "minute": 30,
        "timezone": "America/Vancouver",
    }


def test_schedule_update_rebuilds_cron_preserves_target_and_enables():
    sch = FakeScheduler(state="DISABLED")
    svc = AmlScheduleService(sch, "aphrodite-aml-export")

    out = svc.update(enabled=True, day_of_month=5, hour=9, minute=0)

    assert sch.updated["ScheduleExpression"] == "cron(0 9 5 * ? *)"
    assert sch.updated["State"] == "ENABLED"
    # target + flexible window preserved from the existing schedule
    assert sch.updated["Target"]["RoleArn"].endswith("aml-export-scheduler")
    assert sch.updated["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert out == {
        "enabled": True,
        "day_of_month": 5,
        "hour": 9,
        "minute": 0,
        "timezone": "America/Vancouver",
    }


def test_schedule_update_can_disable():
    sch = FakeScheduler(expr="cron(0 0 1 * ? *)", state="ENABLED")
    svc = AmlScheduleService(sch, "aphrodite-aml-export")
    out = svc.update(enabled=False, day_of_month=1, hour=0, minute=0)
    assert sch.updated["State"] == "DISABLED"
    assert out["enabled"] is False


@pytest.mark.parametrize(
    "dom,hour,minute",
    [(0, 0, 0), (29, 0, 0), (1, 24, 0), (1, 0, 60), (1, -1, 0)],
)
def test_schedule_update_rejects_out_of_range(dom, hour, minute):
    svc = AmlScheduleService(FakeScheduler(), "aphrodite-aml-export")
    with pytest.raises(ScheduleError):
        svc.update(enabled=True, day_of_month=dom, hour=hour, minute=minute)
