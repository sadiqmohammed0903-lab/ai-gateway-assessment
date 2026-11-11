"""Logging that writes only to stderr and carries a request_id on every record."""

import contextvars
import logging
import sys
import uuid

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def set_request_id(request_id: str | None = None) -> str:
    rid = request_id or new_request_id()
    request_id_var.set(rid)
    return rid


def configure_logging(level: int = logging.INFO) -> None:
    """Route all logging to stderr. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
