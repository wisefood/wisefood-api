"""Logging configuration.

Two things beyond the stdlib defaults:

* every record carries the in-flight request's correlation id and caller, so one
  user action can be followed across this service's lines and — because the id
  is forwarded downstream — across FoodChat, FoodScholar and RecipeWrangler too;
* an optional JSON formatter, because the text formatter silently discarded
  every ``extra={...}`` field the code already passes (method, path, status,
  duration_ms, request_id), which made the error path unqueryable.

``LOG_FORMAT=json`` switches formatters. The default stays ``text`` so a
deployment that has not opted in sees exactly the output it saw before, plus the
request id.
"""

import json
import logging
import logging.config
import os
from datetime import datetime, timezone

import context

_override = os.getenv("FASTAPI_DEBUG", "false").lower() in ["true", "1", "yes"]


def override_level(level: str):
    global _override
    if _override:
        return "DEBUG"
    return level


class ContextTextFormatter(logging.Formatter):
    """Text formatter that fills the correlation id in itself.

    The obvious alternative — ``%(request_id)s`` in the format string, fed by
    the filter below — turns any handler the filter missed into a formatting
    error, i.e. a logging change that can break a request. Reading the context
    here means the formatter is correct on its own and the filter is only an
    optimisation.
    """

    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, "request_id", None) in (None, ""):
            record.request_id = context.get_request_id() or "-"
        return super().format(record)


class ContextFilter(logging.Filter):
    """Stamp the in-flight request's context onto every record.

    A filter rather than a formatter concern: the text formatter references
    ``%(request_id)s``, and a record that never reached this filter would raise
    a formatting error instead of logging.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        fields = context.log_fields()
        for key, value in fields.items():
            if not hasattr(record, key) or getattr(record, key, None) in (None, ""):
                setattr(record, key, value)
        return True


# Attributes LogRecord always carries. Anything else on a record came from an
# `extra={...}` at the call site and is what we actually want in the JSON.
_STANDARD_ATTRS = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info thread threadName taskName""".split()
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including every ``extra`` field."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:
            # Never let a log line take down the request that emitted it.
            return json.dumps(
                {
                    "ts": payload["ts"],
                    "level": payload["level"],
                    "logger": payload["logger"],
                    "message": payload["message"],
                    "log_error": "payload not serialisable",
                }
            )


def log_format() -> str:
    return (os.getenv("LOG_FORMAT", "text") or "text").strip().lower()


def configure():
    fmt = "json" if log_format() == "json" else "text"
    default_formatter = "json" if fmt == "json" else "standard"
    # uvicorn's own lines go through the same formatter under JSON, so a log
    # collector never has to parse two shapes from one stream.
    uvicorn_formatter = "json" if fmt == "json" else "uvicorn"
    error_formatter = "json" if fmt == "json" else "standard"

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "context": {"()": "logsys.ContextFilter"},
            },
            "formatters": {
                "standard": {
                    "()": "logsys.ContextTextFormatter",
                    "format": "[%(asctime)s] %(name)s - %(levelname)s - [%(request_id)s] %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
                "simple": {"format": "%(name)s:%(levelname)s:%(message)s"},
                "uvicorn": {
                    "()": "logsys.ContextTextFormatter",
                    "format": "%(levelname)s: [%(request_id)s] %(message)s",
                },
                "json": {"()": "logsys.JsonFormatter"},
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "level": override_level("INFO"),
                    "formatter": default_formatter,
                    "filters": ["context"],
                    "stream": "ext://sys.stdout",
                },
                "uvicorn_access": {
                    "class": "logging.StreamHandler",
                    "level": "INFO",
                    "formatter": uvicorn_formatter,
                    "filters": ["context"],
                    "stream": "ext://sys.stdout",
                },
                "uvicorn_error": {
                    "class": "logging.StreamHandler",
                    "level": "INFO",
                    "formatter": error_formatter,
                    "filters": ["context"],
                    "stream": "ext://sys.stderr",
                },
            },
            "root": {
                "level": override_level("INFO"),
                "handlers": ["default"],
            },
            "loggers": {
                "uvicorn": {
                    "level": override_level("INFO"),
                    "handlers": ["uvicorn_error"],
                    "propagate": False,
                },
                "uvicorn.access": {
                    "level": override_level("INFO"),
                    "handlers": ["uvicorn_access"],
                    "propagate": False,
                },
                "uvicorn.error": {
                    "level": override_level("INFO"),
                    "handlers": ["uvicorn_error"],
                    "propagate": False,
                },
                "httpx": {"level": override_level("WARNING"), "propagate": False},
                "urllib3": {"level": override_level("WARNING"), "propagate": False},
                "fastapi": {"level": override_level("INFO"), "propagate": True},
            },
        }
    )
