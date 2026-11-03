import json
import logging

from services.logging import get_logger


def test_get_logger_emits_json(capsys):
    logger = get_logger("test.logging", level=logging.INFO)
    logger.info("hello")
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logging"
    assert payload["message"] == "hello"


def test_get_logger_is_idempotent():
    a = get_logger("test.logging.idempotent")
    b = get_logger("test.logging.idempotent")
    assert a is b
    assert len(a.handlers) == 1
