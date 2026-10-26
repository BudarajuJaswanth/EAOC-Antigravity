"""
EAOC Observability Timing Utility
Provides monotonic timing utilities for duration measurement.
"""

import functools
import time
from typing import Any, Callable, Optional
from eaoc.observability.context import get_current_context


class Timer:
    """
    Context manager and standalone timer for measuring elapsed durations in milliseconds.
    Uses monotonic time.perf_counter().
    """

    def __init__(self):
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None

    def start(self) -> "Timer":
        """Start the timer."""
        self._start_time = time.perf_counter()
        self._end_time = None
        return self

    def stop(self) -> float:
        """Stop the timer and return duration in milliseconds."""
        if self._start_time is None:
            raise RuntimeError("Timer was not started.")
        self._end_time = time.perf_counter()
        return self.elapsed_ms

    @property
    def elapsed_ms(self) -> float:
        """Returns elapsed time in milliseconds."""
        if self._start_time is None:
            return 0.0
        end = self._end_time if self._end_time is not None else time.perf_counter()
        return round((end - self._start_time) * 1000, 2)

    def __enter__(self) -> "Timer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


def time_function(event_name: Optional[str] = None):
    """
    Decorator for measuring sync or async function duration.
    Injects duration_ms into logging or execution result.
    """

    def decorator(func: Callable) -> Callable:
        import inspect

        name = event_name or func.__name__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                with Timer() as timer:
                    res = await func(*args, **kwargs)
                return res

            return async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs) -> Any:
                with Timer() as timer:
                    res = func(*args, **kwargs)
                return res

            return sync_wrapper

    return decorator
