import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.epic_authorization_service as authorization
from extensions.runtime_failures import CaptchaBudget, EpicCaptchaBudgetExhaustedError
from services.epic_authorization_service import (
    EpicAuthorization,
    EpicLoginChallengeRejectedError,
)


@pytest.mark.parametrize(
    "message",
    [
        "Incorrect response. Please refresh the page.",
        "INCORRECT RESPONSE.\n Please refresh the page.",
    ],
)
def test_epic_challenge_rejection_does_not_wait_for_a_missing_password(message):
    page = SimpleNamespace(wait_for_timeout=AsyncMock())
    auth = EpicAuthorization(page)
    auth._page_body_text = AsyncMock(return_value=message)
    auth._password_step_visible = AsyncMock(return_value=False)

    with pytest.raises(EpicLoginChallengeRejectedError, match="Epic rejected"):
        asyncio.run(auth._wait_for_password_form(object()))

    page.wait_for_timeout.assert_not_awaited()
    auth._password_step_visible.assert_not_awaited()


def test_epic_rejection_is_detected_after_challenge_success():
    page = SimpleNamespace(wait_for_timeout=AsyncMock())
    auth = EpicAuthorization(page)
    auth._page_body_text = AsyncMock(
        side_effect=["Sign in to Epic Games", "Incorrect response. Please refresh the page."]
    )
    auth._password_step_visible = AsyncMock(return_value=False)
    auth._has_visible_hcaptcha = AsyncMock(return_value=True)
    auth._require_visible_hcaptcha_cleared = AsyncMock()

    with pytest.raises(EpicLoginChallengeRejectedError):
        asyncio.run(auth._wait_for_password_form(object()))

    auth._require_visible_hcaptcha_cleared.assert_awaited_once()
    page.wait_for_timeout.assert_not_awaited()


def test_password_form_is_still_returned_when_epic_accepts_the_challenge():
    password = object()
    auth = EpicAuthorization(SimpleNamespace())
    auth._page_body_text = AsyncMock(return_value="Enter your password")
    auth._password_step_visible = AsyncMock(return_value=True)
    auth._first_visible_locator = AsyncMock(return_value=password)

    assert asyncio.run(auth._wait_for_password_form(object())) is password


def test_login_returns_to_existing_retry_loop_for_rejected_challenge(monkeypatch):
    page = SimpleNamespace(
        goto=AsyncMock(),
        screenshot=AsyncMock(),
        locator=lambda *_: SimpleNamespace(evaluate_all=AsyncMock()),
    )
    auth = EpicAuthorization(page)
    auth._wait_for_login_form = AsyncMock(side_effect=EpicLoginChallengeRejectedError("rejected"))
    monkeypatch.setattr(authorization, "AgentV", lambda **_: object())
    monkeypatch.setattr(authorization, "redact_totp_inputs", AsyncMock())

    assert asyncio.run(auth._login()) is None


def test_login_refreshes_cannot_reset_shared_captcha_budget(monkeypatch):
    page = SimpleNamespace(
        on=lambda *_: None,
        context=SimpleNamespace(clear_cookies=AsyncMock()),
    )
    auth = EpicAuthorization(page)
    auth._captcha_budget = CaptchaBudget(scope="authentication")
    original_budget = auth._captcha_budget
    auth._goto_claim_page = AsyncMock()
    auth._needs_privacy_policy_correction = lambda: False
    auth._get_login_status = AsyncMock(return_value="false")
    auth._replace_page = AsyncMock()
    auth._page_body_text = AsyncMock(return_value="Incorrect response. Please refresh the page.")
    monkeypatch.setattr(authorization.settings, "AUTH_MAX_ATTEMPTS", 5)

    async def rejected_login():
        auth._captcha_budget.begin_attempt(1)
        try:
            await auth._wait_for_password_form(object())
        except EpicLoginChallengeRejectedError:
            return None

    auth._login = rejected_login
    with pytest.raises(EpicCaptchaBudgetExhaustedError):
        asyncio.run(auth.invoke())

    assert auth._captcha_budget is original_budget
    assert original_budget.attempts == 3
    assert auth._replace_page.await_count == 3
