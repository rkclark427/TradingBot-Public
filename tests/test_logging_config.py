import json
import logging

import pytest

from src.logging_config import configure_logging, get_logger, sleeve_log_context


def test_configure_logging_sets_level():
    configure_logging(level="WARNING")
    assert logging.getLogger().level == logging.WARNING
    configure_logging(level="INFO")


def test_get_logger_returns_bound_logger():
    configure_logging()
    logger = get_logger("test.module")
    assert logger is not None


def test_get_logger_with_initial_context():
    configure_logging()
    logger = get_logger("test.module", component="risk")
    assert logger is not None


def test_sleeve_log_context_binds_sleeve_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO")
    logger = get_logger("test.sleeve")

    with sleeve_log_context(logger, "sleeve-abc-123") as bound:
        bound.info("test event", symbol="SPY")

    captured = capsys.readouterr()
    lines = [l for l in captured.out.strip().splitlines() if l]
    assert lines, "Expected at least one log line"
    record = json.loads(lines[-1])
    assert record["sleeve_id"] == "sleeve-abc-123"
    assert record["symbol"] == "SPY"
    assert record["event"] == "test event"


def test_log_output_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO")
    logger = get_logger("test.json")
    logger.info("json check", order_id="ord-1", side="buy")

    captured = capsys.readouterr()
    lines = [l for l in captured.out.strip().splitlines() if l]
    assert lines
    record = json.loads(lines[-1])
    assert "timestamp" in record
    assert "level" in record
    assert record["event"] == "json check"
    assert record["order_id"] == "ord-1"


def test_log_file_created(tmp_path: pytest.TempPathFactory) -> None:
    configure_logging(level="INFO", log_dir=tmp_path)
    logger = get_logger("test.file")
    logger.info("file write test")

    log_file = tmp_path / "trading-bot.log"
    assert log_file.exists()
    content = log_file.read_text()
    assert "file write test" in content
