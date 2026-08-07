from __future__ import annotations

import threading
import time


class CancelledError(RuntimeError):
    """Raised when an operation is cancelled or its deadline expires."""


class CancellationToken:
    """Thread-safe cooperative cancellation with an optional monotonic deadline."""

    def __init__(self, deadline: float | None = None) -> None:
        self.deadline = deadline
        self._cancelled = threading.Event()
        self._reason = "operation cancelled"

    @classmethod
    def with_timeout(cls, seconds: float) -> "CancellationToken":
        if seconds <= 0:
            raise ValueError("timeout must be positive")
        return cls(time.monotonic() + seconds)

    def cancel(self, reason: str = "operation cancelled") -> None:
        self._reason = reason
        self._cancelled.set()

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise CancelledError(self._reason)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise CancelledError("operation timed out")

    def bounded_timeout(self, maximum: float) -> float:
        self.raise_if_cancelled()
        remaining = self.remaining()
        return maximum if remaining is None else max(0.001, min(maximum, remaining))

    def wait(self, seconds: float) -> None:
        self.raise_if_cancelled()
        remaining = self.remaining()
        delay = seconds if remaining is None else min(seconds, remaining)
        if self._cancelled.wait(max(0.0, delay)):
            raise CancelledError(self._reason)
        self.raise_if_cancelled()


def cancellation_or_default(value: CancellationToken | None) -> CancellationToken:
    return value if value is not None else CancellationToken()
