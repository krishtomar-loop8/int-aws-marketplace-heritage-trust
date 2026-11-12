import pytest

from backend.api.config_admin import ConfigValidationError, get_config, put_config
from services.config import ConfigClient


class _FakeSsm:
    def __init__(self):
        self.params = {}

    def get_parameter(self, *, Name):
        from botocore.exceptions import ClientError
        if Name not in self.params:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
                "GetParameter",
            )
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, *, Name, Value, Type, Overwrite):
        self.params[Name] = Value


def test_get_config_reports_default_for_every_field_when_nothing_configured(monkeypatch):
    monkeypatch.setenv("APHRODITE_ORG_NAME", "Heritage Trust")
    client = ConfigClient(client=_FakeSsm())
    fields = get_config(client)
    assert fields["org_name"] == {"value": "Heritage Trust", "source": "default"}
    assert fields["quarterly_qualifying_practice_areas"] == {"value": [], "source": "default"}


def test_get_config_reports_configured_source_after_a_put(monkeypatch):
    monkeypatch.setenv("APHRODITE_ORG_NAME", "Heritage Trust")
    client = ConfigClient(client=_FakeSsm())
    put_config(client, {"org_name": "Acme Trust"})
    assert get_config(client)["org_name"] == {"value": "Acme Trust", "source": "configured"}


def test_get_config_distinguishes_configured_empty_list_from_unconfigured():
    client = ConfigClient(client=_FakeSsm())
    put_config(client, {"quarterly_qualifying_practice_areas": []})
    field = get_config(client)["quarterly_qualifying_practice_areas"]
    assert field == {"value": [], "source": "configured"}


def test_put_config_rejects_a_pattern_that_does_not_compile():
    client = ConfigClient(client=_FakeSsm())
    with pytest.raises(ConfigValidationError, match="internal_matter_number_pattern"):
        put_config(client, {"internal_matter_number_pattern": "["})
    assert get_config(client)["internal_matter_number_pattern"]["source"] == "default"


def test_put_config_rejects_a_blank_entry_in_a_list_field():
    client = ConfigClient(client=_FakeSsm())
    with pytest.raises(ConfigValidationError, match="own_company_names"):
        put_config(client, {"own_company_names": ["Acme", "  "]})


def test_put_config_null_clears_a_field_back_to_default(monkeypatch):
    monkeypatch.setenv("APHRODITE_ORG_NAME", "Heritage Trust")
    client = ConfigClient(client=_FakeSsm())
    put_config(client, {"org_name": "Acme Trust"})
    put_config(client, {"org_name": None})
    assert get_config(client)["org_name"] == {"value": "Heritage Trust", "source": "default"}


def test_get_config_parses_a_json_encoded_list_env_var(monkeypatch):
    monkeypatch.setenv(
        "APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", '["hr", "human resources", "finance"]'
    )
    client = ConfigClient(client=_FakeSsm())
    field = get_config(client)["excluded_sharepoint_folders"]
    assert field == {
        "value": ["hr", "human resources", "finance"],
        "source": "default",
    }


def test_put_config_rejects_a_non_list_value_for_a_list_field():
    client = ConfigClient(client=_FakeSsm())
    with pytest.raises(ConfigValidationError, match="own_company_names"):
        put_config(client, {"own_company_names": "Acme"})
