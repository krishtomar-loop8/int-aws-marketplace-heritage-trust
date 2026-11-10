import json

from botocore.exceptions import ClientError

from services.config import ConfigClient


class _FakeSsm:
    def __init__(self):
        self.params = {}

    def get_parameter(self, *, Name):
        if Name not in self.params:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
                "GetParameter",
            )
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, *, Name, Value, Type, Overwrite):
        self.params[Name] = Value


def test_get_returns_empty_dict_when_parameter_never_created():
    client = ConfigClient(client=_FakeSsm())
    assert client.get() == {}


def test_put_then_get_round_trips():
    ssm = _FakeSsm()
    client = ConfigClient(client=ssm)
    client.put({"org_name": "Acme Trust"})
    assert client.get(refresh=True) == {"org_name": "Acme Trust"}


def test_put_merges_rather_than_replaces():
    ssm = _FakeSsm()
    client = ConfigClient(client=ssm)
    client.put({"org_name": "Acme Trust"})
    client.put({"org_office_address": "1 Main St"})
    assert client.get(refresh=True) == {
        "org_name": "Acme Trust",
        "org_office_address": "1 Main St",
    }


def test_put_with_none_value_clears_that_key():
    ssm = _FakeSsm()
    client = ConfigClient(client=ssm)
    client.put({"org_name": "Acme Trust", "org_office_address": "1 Main St"})
    client.put({"org_name": None})
    assert client.get(refresh=True) == {"org_office_address": "1 Main St"}


def test_get_caches_until_refresh():
    ssm = _FakeSsm()
    client = ConfigClient(client=ssm)
    client.put({"org_name": "Acme Trust"})
    ssm.params["/aphrodite/config/deployment"] = json.dumps({"org_name": "tampered"})
    assert client.get() == {"org_name": "Acme Trust"}  # cached, not re-read
    assert client.get(refresh=True) == {"org_name": "tampered"}
