"""Structured logging. See PLAT-44."""

import contextvars
import json
import logging

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "message": record.getMessage(),
                "requestId": request_id.get(),
            }
        )


def schedule_background(loop, coro):
    """PLAT-44: the current context is not copied, so request_id reverts to
    its default inside the scheduled task."""
    return loop.create_task(coro)
