"""Classify terminal runtime failures without importing settings or exposing responses."""

import re
import time


class EpicNonRetryableError(RuntimeError):
    pass


class EpicCaptchaRequiredError(EpicNonRetryableError):
    pass


class EpicCaptchaBudgetExhaustedError(EpicNonRetryableError):
    pass


class CaptchaBudget:
    """One counter and deadline shared by every retry in an owning flow."""

    def __init__(self, *, scope, max_attempts=3, timeout_seconds=300, clock=time.monotonic):
        self.scope = scope
        self.max_attempts = max_attempts
        self.attempts = 0
        self.clock = clock
        self.deadline = clock() + timeout_seconds

    def remaining(self):
        seconds = self.deadline - self.clock()
        if seconds <= 0:
            raise EpicCaptchaBudgetExhaustedError(
                f'{self.scope} time budget exhausted; stopping without confirming success.'
            )
        return seconds

    def begin_attempt(self, timeout_seconds):
        remaining = self.remaining()
        if self.attempts >= self.max_attempts:
            raise EpicCaptchaBudgetExhaustedError(
                f'{self.scope} CAPTCHA budget exhausted: '
                f'{self.attempts}/{self.max_attempts} attempts used across all retries.'
            )
        self.attempts += 1
        return min(timeout_seconds, remaining)


class EpicLlmQuotaExhaustedError(EpicNonRetryableError):
    pass


class EpicLlmConfigurationError(EpicNonRetryableError):
    pass


def exception_chain(error: BaseException):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def llm_failure_kind(error: BaseException) -> str | None:
    fallback = None
    for current in exception_chain(error):
        if isinstance(current, EpicLlmQuotaExhaustedError):
            return 'daily_quota'
        if isinstance(current, EpicLlmConfigurationError):
            return 'configuration'
        text = str(current).lower()
        response = getattr(current, 'response', None)
        status = getattr(response, 'status_code', None) or getattr(current, 'code', None)
        exhausted = status == 429 or '429' in text or 'resource_exhausted' in text
        daily = any(
            marker in text
            for marker in (
                'requestsperday',
                'tokensperday',
                'requests_per_day',
                'tokens_per_day',
                'per day',
                'daily quota',
                'daily limit',
            )
        )
        if exhausted and daily:
            return 'daily_quota'
        if status in (400, 401, 403) or any(
            marker in text
            for marker in (
                'api_key_invalid',
                'unauthenticated',
                'permission_denied',
            )
        ):
            return 'configuration'
        if exhausted:
            fallback = 'rate_limit'
    return fallback


def raise_if_non_retryable(error: BaseException) -> None:
    for current in exception_chain(error):
        if isinstance(current, EpicNonRetryableError):
            raise current
    kind = llm_failure_kind(error)
    if kind == 'daily_quota':
        raise EpicLlmQuotaExhaustedError(
            'LLM daily quota exhausted; stopping without further requests. '
            'Wait for the provider quota reset; this program cannot increase the quota.'
        ) from None
    if kind == 'configuration':
        raise EpicLlmConfigurationError(
            'LLM rejected the request or credentials; stopping without retries. '
            'Review the configured provider/model and access permissions.'
        ) from None


def llm_retry_delay(error: BaseException) -> float:
    for current in exception_chain(error):
        response = getattr(current, 'response', None)
        headers = getattr(response, 'headers', {})
        value = headers.get('retry-after') if headers else None
        if value is None:
            match = re.search(r'retryDelay[\"\s:]+([0-9.]+)s', str(current), re.IGNORECASE)
            value = match.group(1) if match else None
        try:
            return min(30.0, max(1.0, float(value)))
        except (TypeError, ValueError):
            continue
    return 15.0
