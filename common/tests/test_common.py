import io
import logging
import sys

from common import configure_logging, make_error, set_request_id


def test_error_schema_shape() -> None:
    body = make_error("rate_limit", "too_many_requests", "slow down", "abc").model_dump()
    assert body == {
        "error": {
            "type": "rate_limit",
            "code": "too_many_requests",
            "message": "slow down",
            "request_id": "abc",
        }
    }


def test_logging_goes_only_to_stderr_with_request_id(monkeypatch) -> None:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    configure_logging()
    set_request_id("req-123")
    logging.getLogger("t").info("hello")
    assert out.getvalue() == ""
    assert "req-123" in err.getvalue() and "hello" in err.getvalue()
