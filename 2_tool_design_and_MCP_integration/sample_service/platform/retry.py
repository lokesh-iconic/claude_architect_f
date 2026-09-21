"""Retry helper. See PLAT-31."""

import time
from typing import Callable, TypeVar

T = TypeVar("T")


class RetryError(Exception):
    pass


def with_retries(fn: Callable[[], T], attempts: int = 3, delay: float = 0.1) -> T:
    """PLAT-31: on exhaustion this raises a bare RetryError, dropping the
    original exception and its traceback."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception:
            if attempt == attempts - 1:
                raise RetryError(f"failed after {attempts} attempts")
            time.sleep(delay * (2**attempt))
    raise RetryError("unreachable")
