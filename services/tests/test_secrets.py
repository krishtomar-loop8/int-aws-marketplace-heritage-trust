import json

from services.secrets import SecretsClient


class FakeSecretsManager:
    def __init__(self, value: str):
        self._value = value
        self.calls = 0

    def get_secret_value(self, SecretId: str):  # noqa: N803 (boto3 param name)
        self.calls += 1
        return {"SecretString": self._value}


def test_get_parses_json():
    fake = FakeSecretsManager(json.dumps({"token": "abc"}))
    client = SecretsClient(client=fake)
    assert client.get("aphrodite/clio") == {"token": "abc"}


def test_get_caches_until_refresh():
    fake = FakeSecretsManager(json.dumps({"token": "abc"}))
    client = SecretsClient(client=fake)
    client.get("aphrodite/clio")
    client.get("aphrodite/clio")
    assert fake.calls == 1
    client.get("aphrodite/clio", refresh=True)
    assert fake.calls == 2
