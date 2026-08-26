"""Bounded exponential retry policy."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_delay_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("retry delays must be positive")

    def can_retry(self, attempt_number: int) -> bool:
        return attempt_number < self.max_attempts

    def delay_for(self, attempt_number: int, *, jitter: float) -> float:
        if attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if not 0.0 <= jitter <= 1.0:
            raise ValueError("jitter must be between zero and one")
        base = float(
            min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** (attempt_number - 1)),
            )
        )
        return float(min(self.max_delay_seconds, base + base * jitter))
