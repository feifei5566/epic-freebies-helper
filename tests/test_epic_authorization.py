import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.mark.parametrize(
    "url,method,endpoint",
    [
        ("https://www.epicgames.com/id/api/login/options", "GET", "/id/api/login/options"),
        ("https://talon-service-prod.ecosec.on.epicgames.com/v1/init", "POST", "talon"),
    ],
)
@pytest.mark.parametrize("status", [200, 403])
def test_prelogin_rejections_are_observed_without_becoming_login_signals(
    monkeypatch, url, method, endpoint, status
):
    log = Mock()
    monkeypatch.setattr(authorization, "logger", log)
    code = "errors.com.epicgames.accountportal.captcha_invalid"
    response = SimpleNamespace(
        url=url,
        status=status,
        request=SimpleNamespace(method=method),
        json=AsyncMock(return_value={"errorCode": code, "token": "private-token"}),
    )
    auth = EpicAuthorization(SimpleNamespace())

    asyncio.run(auth._on_response_anything(response))

    log.warning.assert_called_once_with(
        "Epic authentication response rejected | endpoint={} | status={} | error_code={}",
        endpoint,
        status,
        code,
    )
    assert auth._is_login_success_signal.empty()
    assert auth._login_error_signal.empty()


@pytest.mark.parametrize("body", [{"errorCode": "private-token"}, ["private-token"], None])
def test_auth_diagnostics_redact_unknown_paths_and_response_content(monkeypatch, body):
    log = Mock()
    monkeypatch.setattr(authorization, "logger", log)
    response = SimpleNamespace(
        url="https://www.epicgames.com/id/api/private-token?email=private@example.test",
        status=403,
        json=AsyncMock(return_value=body),
    )
    asyncio.run(EpicAuthorization(SimpleNamespace())._log_auth_rejection(response))

    assert log.warning.call_args.args[1:3] == ("/id/api/[other]", 403)
    assert "private" not in str(log.mock_calls)


def test_non_json_auth_error_still_reports_status(monkeypatch):
    log = Mock()
    monkeypatch.setattr(authorization, "logger", log)
    response = SimpleNamespace(
        url="https://www.epicgames.com/id/api/reputation",
        status=429,
        json=AsyncMock(side_effect=ValueError("private response body")),
    )
    asyncio.run(EpicAuthorization(SimpleNamespace())._log_auth_rejection(response))
    assert log.warning.call_args.args[1:] == ("/id/api/reputation", 429, "unavailable")


@pytest.mark.parametrize(
    "url,status",
    [
        ("https://www.epicgames.com/id/api/login", 200),
        ("https://www.epicgames.com.evil.test/id/api/login", 403),
        ("https://api.hcaptcha.com/checkcaptcha/token", 403),
        ("https://www.epicgames.com/account/personal", 403),
    ],
)
def test_success_and_unrelated_responses_do_not_log_auth_failures(monkeypatch, url, status):
    log = Mock()
    monkeypatch.setattr(authorization, "logger", log)
    response = SimpleNamespace(url=url, status=status, json=AsyncMock(return_value={}))
    asyncio.run(EpicAuthorization(SimpleNamespace())._log_auth_rejection(response))
    log.warning.assert_not_called()


def test_real_login_error_is_still_queued(monkeypatch):
    monkeypatch.setattr(authorization, "logger", Mock())
    payload = {"errorCode": "errors.com.epicgames.account.invalid_account_credentials"}
    response = SimpleNamespace(
        url="https://www.epicgames.com/id/api/login",
        status=400,
        request=SimpleNamespace(method="POST"),
        json=AsyncMock(return_value=payload),
    )
    auth = EpicAuthorization(SimpleNamespace())
    asyncio.run(auth._on_response_anything(response))
    assert auth._login_error_signal.get_nowait() == payload
