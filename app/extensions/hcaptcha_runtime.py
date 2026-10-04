# -*- coding: utf-8 -*-
import asyncio
import sys

from hcaptcha_challenger.agent import AgentV
from hcaptcha_challenger.models import ChallengeSignal
from loguru import logger

from extensions.runtime_failures import (
    CaptchaBudget,
    EpicCaptchaRequiredError,
    llm_failure_kind,
    llm_retry_delay,
    raise_if_non_retryable,
)


class EpicCaptchaAgent(AgentV):
    """Observe challenges without solving unless that action was explicitly enabled."""

    async def _task_handler(self, response):
        if getattr(self.config, 'ALLOW_CAPTCHA_SOLVING', False):
            return await super()._task_handler(response)
        # Do not inject/decode HSW or process puzzle payloads in the disabled mode.
        if '/getcaptcha/' in response.url:
            self._epic_captcha_required = True
            if 'application/json' in response.headers.get('content-type', ''):
                try:
                    payload = await response.json()
                    self._epic_captcha_required = not bool(payload.get('pass'))
                except Exception:
                    pass

    async def _solve_captcha(self):
        if not getattr(self.config, 'ALLOW_CAPTCHA_SOLVING', False):
            while True:
                required = getattr(self, '_epic_captcha_required', False)
                for frame in self.page.frames:
                    if 'hcaptcha' in (frame.url or '').lower():
                        element = await frame.frame_element()
                        if element is not None and await element.is_visible():
                            required = True
                if required:
                    raise EpicCaptchaRequiredError(
                        'CAPTCHA requires manual action; automatic solving is disabled. '
                        'This run did not solve the challenge or confirm a claim.'
                    )
                await asyncio.sleep(0.25)
        # Upstream recursively retries exceptions inside _solve_captcha, regardless
        # of RETRY_ON_FAILURE. Re-entry returns the error to our bounded caller.
        if getattr(self, '_epic_solving', False):
            error = sys.exception()
            if error is not None:
                raise_if_non_retryable(error)
                raise error
            raise RuntimeError('Internal challenge retry deferred to the bounded caller')
        self._epic_solving = True
        try:
            return await super()._solve_captcha()
        except Exception as error:
            raise_if_non_retryable(error)
            raise
        finally:
            self._epic_solving = False


async def wait_for_challenge_signal(
    agent: AgentV, *, context: str, timeout_seconds: float, budget: CaptchaBudget
) -> ChallengeSignal:
    timeout_seconds = budget.begin_attempt(timeout_seconds)
    logger.info(
        'CAPTCHA shared budget | scope={} | context={} | attempt={}/{}',
        budget.scope,
        context,
        budget.attempts,
        budget.max_attempts,
    )
    try:
        signal = await asyncio.wait_for(agent.wait_for_challenge(), timeout=timeout_seconds)
    except Exception as err:
        raise_if_non_retryable(err)
        budget.remaining()
        if llm_failure_kind(err) == 'rate_limit':
            delay = min(llm_retry_delay(err), budget.remaining())
            logger.warning('LLM temporary rate limit | context={} | backoff={}s', context, delay)
            await asyncio.sleep(delay)
            budget.remaining()
        logger.warning(
            "hCaptcha challenge wait failed | context={} | timeout={}s | error_type={}",
            context,
            timeout_seconds,
            type(err).__name__,
        )
        raise

    budget.remaining()
    logger.info("hCaptcha challenge result | context={} | signal={}", context, signal.value)
    return signal
