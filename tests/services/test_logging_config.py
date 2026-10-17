import json
import logging

from services.logging_config import JsonFormatter, configure_logging
from services.logging_config import log_event, LogStatus
from services.logging_config import reset_request_id, set_request_id


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
    assert payload["event"] == "runtime.progress"
    assert payload["status"] == "progress"


def test_configure_logging_writes_json_file(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "api" / "api.jsonl"
    monkeypatch.setenv("LOG_FILE", str(log_path))
    monkeypatch.setenv("LOG_COMPONENT", "api")

    configure_logging()
    logging.getLogger("test.file").info(
        "Событие записано",
        extra={"job_id": "job-id", "prompt": "private prompt"},
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    payload = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert payload["component"] == "api"
    assert payload["job_id"] == "job-id"
    assert "prompt" not in payload


def test_log_event_builds_common_lifecycle_contract(caplog) -> None:
    logger = logging.getLogger("test.contract")

    with caplog.at_level(logging.INFO):
        log_event(
            logger,
            logging.INFO,
            "asset_probe",
            LogStatus.STARTED,
            "Технический анализ медиа начат",
            stage="prepare",
            job_id="job-id",
        )

    record = caplog.records[-1]
    assert record.operation == "asset_probe"
    assert record.event == "asset_probe.started"
    assert record.stage == "prepare"
    assert record.status == "started"
    assert record.job_id == "job-id"


def test_json_formatter_uses_request_context() -> None:
    token = set_request_id("request-id")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Событие",
            args=(),
            exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
    finally:
        reset_request_id(token)

    assert payload["request_id"] == "request-id"
