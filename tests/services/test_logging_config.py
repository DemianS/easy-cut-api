import json
import logging

from services.logging_config import JsonFormatter


def test_json_logs_keep_safe_context_and_drop_prompt() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Задача запущена",
        args=(),
        exc_info=None,
    )
    record.job_id = "job-id"
    record.prompt = "private prompt"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["job_id"] == "job-id"
    assert "prompt" not in payload
