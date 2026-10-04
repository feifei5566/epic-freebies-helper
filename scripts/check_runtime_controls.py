#!/usr/bin/env python3
"""Targeted offline verification; no deploy, credentials, Epic requests, or solver."""

import ast
import asyncio
import importlib
import json
import re
import socket
import sys
import tempfile
import types
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from loguru import logger
from tenacity import retry, stop_after_attempt
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))


def deny_network(*args, **kwargs):
    raise RuntimeError('Network is forbidden during runtime-control verification')


socket.create_connection = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
logger.remove()

from extensions.runtime_failures import (  # noqa: E402 — install network guard before local imports
    CaptchaBudget,
    EpicCaptchaBudgetExhaustedError,
    EpicCaptchaRequiredError,
    EpicLlmQuotaExhaustedError,
    EpicNonRetryableError,
    llm_failure_kind,
    llm_retry_delay,
    raise_if_non_retryable,
)
from models import PromotionGame  # noqa: E402


class Signal(Enum):
    SUCCESS = 'success'
    FAILURE = 'failure'


class OfflineAgent:
    def __init__(self, *, page, agent_config):
        self.page, self.config = page, agent_config
        self.handler_calls = self.solver_calls = 0

    async def _task_handler(self, response):
        self.handler_calls += 1

    async def _solve_captcha(self):
        self.solver_calls += 1
        try:
            raise RuntimeError(
                '429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier'
            )
        except Exception:
            # Reproduce the locked upstream exception recursion, without a puzzle.
            return await self._solve_captcha()

    async def wait_for_challenge(self):
        return await self._solve_captcha()


for name in ('hcaptcha_challenger', 'hcaptcha_challenger.agent', 'hcaptcha_challenger.models'):
    sys.modules[name] = types.ModuleType(name)
sys.modules['hcaptcha_challenger.agent'].AgentV = OfflineAgent
sys.modules['hcaptcha_challenger.models'].ChallengeSignal = Signal
from extensions.hcaptcha_runtime import EpicCaptchaAgent, wait_for_challenge_signal  # noqa: E402


def extract(path, names, namespace, *, class_name=None):
    """Load selected real functions without the service's settings/import side effects."""
    tree = ast.parse((ROOT / path).read_text())
    if class_name is not None:
        tree = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
        )
    nodes = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in names
        ):
            node.decorator_list = []
            nodes.append(node)
    if {node.name for node in nodes} != set(names):
        raise RuntimeError('Missing runtime control in ' + path)
    module = ast.Module(
        body=[
            ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
            *nodes,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), path, 'exec'), namespace)


class ManualAction(EpicNonRetryableError):
    pass


class Locator:
    first = property(lambda self: self)

    def __init__(self, text='', checked=True, visible=False, count=0):
        self.text, self.checked, self.visible, self.length = text, checked, visible, count

    async def inner_text(self, **kwargs):
        return self.text

    async def count(self):
        return self.length

    def nth(self, index):
        return self

    async def is_checked(self):
        return self.checked

    async def is_visible(self):
        return self.visible

    async def get_attribute(self, name, **kwargs):
        return None


class Checkout:
    def __init__(self, text, *, new_terms=False, accept=False):
        self.body = Locator(text)
        self.terms = Locator(checked=False, count=int(new_terms))
        self.accept = Locator(visible=accept)

    def locator(self, selector):
        return self.body if selector == 'body' else self.terms

    def get_by_role(self, *args, **kwargs):
        return self.accept


async def expect_failure(action, error_type):
    try:
        await action
    except error_type:
        return
    raise RuntimeError('Expected control did not stop the action: ' + error_type.__name__)


async def verify_shared_retry_budgets(passed):
    clock = [0.0]
    settings = types.SimpleNamespace(
        EXECUTION_TIMEOUT=1, RESPONSE_TIMEOUT=0, AUTH_MAX_ATTEMPTS=5, TASK_TIMEOUT_SECONDS=900
    )
    namespace = dict(
        asyncio=asyncio,
        logger=logger,
        suppress=suppress,
        settings=settings,
        time=types.SimpleNamespace(monotonic=lambda: clock[0]),
        CaptchaBudget=CaptchaBudget,
        EpicNonRetryableError=EpicNonRetryableError,
        EpicLlmQuotaExhaustedError=EpicLlmQuotaExhaustedError,
        EpicCaptchaBudgetExhaustedError=EpicCaptchaBudgetExhaustedError,
        PlaywrightTimeoutError=PlaywrightTimeoutError,
        ChallengeSignal=Signal,
        wait_for_challenge_signal=wait_for_challenge_signal,
        raise_if_non_retryable=raise_if_non_retryable,
    )
    extract(
        'app/services/epic_authorization_service.py',
        {'EpicAuthenticationFatalError', 'EpicManualActionRequiredError'},
        namespace,
    )
    methods = {
        '__init__',
        '_solve_visible_hcaptcha',
        '_require_visible_hcaptcha_cleared',
        '_wait_for_password_form',
        '_resubmit_password_form',
        'invoke',
        '_invoke_with_budget',
    }
    extract(
        'app/services/epic_authorization_service.py',
        methods,
        namespace,
        class_name='EpicAuthorization',
    )
    Auth = type('OfflineAuthorization', (), {name: namespace[name] for name in methods})
    Fatal = namespace['EpicAuthenticationFatalError']

    class OfflinePage:
        def on(self, *args):
            pass

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

        context = types.SimpleNamespace(clear_cookies=lambda: asyncio.sleep(0))

    class OfflineChallenge:
        calls = 0
        _captcha_payload = None

        def __init__(self, result=Signal.FAILURE, after_wait=None):
            self.result, self.after_wait = result, after_wait
            self._captcha_response_queue = asyncio.Queue()
            self._captcha_payload_queue = asyncio.Queue()

        async def wait_for_challenge(self):
            self.calls += 1
            if self.after_wait:
                self.after_wait()
            return self.result

    async def yes(*args, **kwargs):
        return True

    async def no(*args, **kwargs):
        return False

    async def noop(*args, **kwargs):
        return None

    def auth_subject():
        subject = Auth(OfflinePage())
        subject._has_visible_hcaptcha = yes
        subject._password_step_visible = no
        subject._has_blocking_talon_overlay = no
        subject._first_visible_locator = noop
        subject._needs_privacy_policy_correction = lambda: False
        subject._on_response_anything = lambda *args: None
        subject._goto_claim_page = noop
        subject._get_login_status = no
        subject._replace_page = noop
        return subject

    false_result = auth_subject()
    false_result._solve_visible_hcaptcha = no
    await expect_failure(false_result._wait_for_password_form(OfflineChallenge()), Fatal)
    await expect_failure(false_result._resubmit_password_form(OfflineChallenge()), Fatal)
    failed = auth_subject()
    challenge = OfflineChallenge()
    await expect_failure(failed._wait_for_password_form(challenge), Fatal)
    assert challenge.calls == failed._captcha_budget.attempts == 3
    await expect_failure(
        failed._wait_for_password_form(OfflineChallenge()), EpicCaptchaBudgetExhaustedError
    )
    passed.append(
        'False captcha result stops password wait/resubmit; exhausted attempts cannot restart a batch'
    )

    restarted = auth_subject()
    challenge = OfflineChallenge()
    original_budget = restarted._captcha_budget
    restarts = []

    async def login_retry():
        await wait_for_challenge_signal(
            challenge, context='offline_login', timeout_seconds=1, budget=restarted._captcha_budget
        )
        return None

    async def replace_page():
        restarted.page = OfflinePage()
        restarts.append(True)

    restarted._login, restarted._replace_page = login_retry, replace_page
    await expect_failure(restarted.invoke(), EpicCaptchaBudgetExhaustedError)
    assert challenge.calls == 3 and len(restarts) == 3
    assert restarted._captcha_budget is original_budget
    passed.append('all authentication/page retries share one three-attempt budget')

    clock[0] = 0
    no_form = auth_subject()
    no_form._captcha_budget = CaptchaBudget(scope='offline_auth', clock=lambda: clock[0])
    visible = [True]

    async def observed_captcha():
        return visible[0]

    no_form._has_visible_hcaptcha = observed_captcha
    successful = OfflineChallenge(Signal.SUCCESS, after_wait=lambda: visible.__setitem__(0, False))
    try:
        await no_form._wait_for_password_form(successful)
    except Fatal as error:
        assert 'password form could not be located' in str(error)
    else:
        raise RuntimeError('Captcha success without a password form was accepted')
    assert successful.calls == 1 and clock[0] <= 33
    passed.append('captcha success without password form stops after one bounded settle window')

    async def never_finishes(*args):
        await asyncio.Event().wait()

    slow_auth = auth_subject()
    slow_auth._captcha_budget.deadline = asyncio.get_running_loop().time() + 0.02
    slow_auth._invoke_with_budget = never_finishes
    await expect_failure(slow_auth.invoke(), Fatal)
    expired = CaptchaBudget(scope='offline_expired', timeout_seconds=1, clock=lambda: clock[0])
    clock[0] += 2
    unused = OfflineChallenge()
    await expect_failure(
        wait_for_challenge_signal(unused, context='expired', timeout_seconds=10, budget=expired),
        EpicCaptchaBudgetExhaustedError,
    )
    assert unused.calls == expired.attempts == 0
    cancelled = [False]

    class SlowChallenge:
        async def wait_for_challenge(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled[0] = True

    await expect_failure(
        wait_for_challenge_signal(
            SlowChallenge(),
            context='slow',
            timeout_seconds=10,
            budget=CaptchaBudget(scope='offline_slow', timeout_seconds=0.02),
        ),
        EpicCaptchaBudgetExhaustedError,
    )
    assert cancelled[0]
    passed.append(
        'shared deadlines cancel a hung authentication/challenge; expired budget starts no work'
    )

    namespace.update(TimeoutError=PlaywrightTimeoutError)
    game_methods = {
        '__init__',
        '_resolve_checkout_security_check',
        '_extended_checkout_challenge_probe',
        'collect_weekly_games',
    }
    extract('app/services/epic_games_service.py', game_methods, namespace, class_name='EpicGames')
    Games = type('OfflineGames', (), {name: namespace[name] for name in game_methods})
    checkout = Games(OfflinePage())
    clock[0] = 0
    checkout._captcha_budget = CaptchaBudget(
        scope='offline_checkout', timeout_seconds=900, clock=lambda: clock[0]
    )
    checkout._raise_if_free_game_rate_limited = noop
    checkout._is_checkout_security_check_visible = yes
    checkout._is_claimed_state = no
    checkout._capture_purchase_debug = noop
    challenge = OfflineChallenge()
    assert (
        await checkout._resolve_checkout_security_check(
            checkout.page, challenge, 'https://offline.invalid/'
        )
        is False
    )
    assert challenge.calls == checkout._captcha_budget.attempts == 3
    new_agent = OfflineChallenge()
    await expect_failure(
        checkout._resolve_checkout_security_check(
            checkout.page, new_agent, 'https://offline.invalid/'
        ),
        EpicCaptchaBudgetExhaustedError,
    )
    settings.ALLOW_CAPTCHA_SOLVING = True  # Offline fake settings only.
    await expect_failure(
        checkout._extended_checkout_challenge_probe(
            checkout.page, new_agent, 'https://offline.invalid/'
        ),
        EpicCaptchaBudgetExhaustedError,
    )
    assert new_agent.calls == 0
    slow_checkout = Games(OfflinePage())
    slow_checkout._captcha_budget.deadline = asyncio.get_running_loop().time() + 0.02
    slow_checkout._collect_weekly_games = never_finishes
    await expect_failure(slow_checkout.collect_weekly_games([]), EpicCaptchaBudgetExhaustedError)
    passed.append(
        'checkout security/reconciliation/new-agent probes share three attempts and a hard flow deadline'
    )

    for path in (
        'app/services/epic_authorization_service.py',
        'app/services/epic_games_service.py',
    ):
        tree = ast.parse((ROOT / path).read_text())
        waits = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'wait_for_challenge_signal'
        ]
        assert waits
        for call in waits:
            budget = next(keyword.value for keyword in call.keywords if keyword.arg == 'budget')
            assert isinstance(budget, ast.Attribute) and budget.attr == '_captcha_budget'
    passed.append('every production challenge wait requires the owning shared budget')


async def main():
    passed = []
    await verify_shared_retry_budgets(passed)
    workflow = yaml.safe_load((ROOT / '.github/workflows/epic-gamer.yml').read_text())
    events = workflow.get('on', workflow.get(True))  # PyYAML uses YAML 1.1 booleans.
    approval = events['workflow_dispatch']['inputs']['allow_captcha_solving']
    assert approval['type'] == 'boolean' and approval['default'] is False
    assert set(events) == {'workflow_dispatch', 'schedule'}
    env = next(
        step['env']
        for step in workflow['jobs']['epic-gamer']['steps']
        if step.get('name') == 'Run Epic Awesome Gamer'
    )
    assert (
        env['ALLOW_CAPTCHA_SOLVING']
        == "${{ github.event_name == 'workflow_dispatch' && inputs.allow_captcha_solving && 'true' || 'false' }}"
    )
    settings_tree = ast.parse((ROOT / 'app/settings.py').read_text())
    default = next(
        n.value
        for n in ast.walk(settings_tree)
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == 'ALLOW_CAPTCHA_SOLVING'
    )
    assert next(k.value.value for k in default.keywords if k.arg == 'default') is False
    passed.append(
        'CAPTCHA approval is a default-off boolean scoped to explicit workflow_dispatch input'
    )
    daily = RuntimeError('429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier')
    minute = RuntimeError(
        '429 RESOURCE_EXHAUSTED GenerateRequestsPerMinutePerProjectPerModel-FreeTier'
    )
    assert llm_failure_kind(daily) == 'daily_quota'
    assert llm_failure_kind(minute) == 'rate_limit'
    wrapper = RuntimeError('429 retry failed')
    wrapper.__cause__ = daily
    assert llm_failure_kind(wrapper) == 'daily_quota'
    minute.response = types.SimpleNamespace(status_code=429, headers={'retry-after': '90'})
    assert llm_retry_delay(minute) == 30
    passed.append('daily versus minute quota classification and bounded Retry-After')

    agent = EpicCaptchaAgent(
        page=types.SimpleNamespace(frames=[]),
        agent_config=types.SimpleNamespace(ALLOW_CAPTCHA_SOLVING=False),
    )
    response = types.SimpleNamespace(
        url='https://offline.invalid/getcaptcha/example',
        headers={'content-type': 'application/json'},
    )

    async def challenge_json():
        return {'pass': False}

    response.json = challenge_json
    await agent._task_handler(response)
    await expect_failure(
        wait_for_challenge_signal(
            agent, context='offline', timeout_seconds=1, budget=CaptchaBudget(scope='offline')
        ),
        EpicCaptchaRequiredError,
    )
    assert agent.handler_calls == agent.solver_calls == 0
    passed.append('CAPTCHA response handler and solver blocked before upstream processing')
    recursive = EpicCaptchaAgent(
        page=types.SimpleNamespace(frames=[]),
        agent_config=types.SimpleNamespace(ALLOW_CAPTCHA_SOLVING=True),
    )
    await recursive._task_handler(response)
    assert recursive.handler_calls == 1
    await expect_failure(recursive._solve_captcha(), EpicLlmQuotaExhaustedError)
    assert recursive.solver_calls == 1
    passed.append('upstream exception recursion stopped after one offline attempt')

    class OfflineProvider:
        calls = 0
        error = daily

        @retry(stop=stop_after_attempt(3))
        async def generate_with_images(self):
            self.calls += 1
            raise self.error

    name = 'hcaptcha_challenger.tools.internal.providers.gemini'
    sys.modules[name] = types.ModuleType(name)
    sys.modules[name].GeminiProvider = OfflineProvider
    namespace = dict(
        logger=logger, llm_failure_kind=llm_failure_kind, llm_retry_delay=llm_retry_delay
    )
    extract('app/extensions/llm_adapter.py', {'_limit_llm_provider_attempts'}, namespace)
    assert namespace['_limit_llm_provider_attempts']()

    async def no_wait(delay):
        return None

    OfflineProvider.generate_with_images.retry.sleep = no_wait
    provider = OfflineProvider()
    await expect_failure(provider.generate_with_images(), RuntimeError)
    assert provider.calls == 1
    provider.calls, provider.error = 0, minute
    await expect_failure(provider.generate_with_images(), RuntimeError)
    assert provider.calls == 2
    provider.calls, provider.error = 0, RuntimeError('403 PERMISSION_DENIED')
    await expect_failure(provider.generate_with_images(), RuntimeError)
    assert provider.calls == 1
    passed.append('real Tenacity policy: daily/configuration one attempt; minute limit two')

    controls = dict(re=re, EpicManualActionRequiredError=ManualAction)
    extract('app/services/epic_games_service.py', {'_assert_free_checkout'}, controls)
    check = controls['_assert_free_checkout']
    await check(Checkout('Subtotal\n$19.99\nDiscount\n-$19.99\nTotal\n$0.00'))
    await check(Checkout('Total: Free'))
    for checkout in (
        Checkout('Total\n$9.99'),
        Checkout('Subtotal\n$0.00'),
        Checkout('Total\n$0.00', new_terms=True),
        Checkout('Total\n$0.00', accept=True),
    ):
        await expect_failure(check(checkout), ManualAction)
    passed.append('checkout zero total, unknown amount, paid total, and new terms guards')

    now = datetime.now(timezone.utc)

    def offer(identifier, *, paid=False, future=False, subscription=False):
        return dict(
            title='Offline offer',
            id=identifier,
            namespace='offline-namespace',
            description='',
            offerType='SUBSCRIPTION' if subscription else 'BASE_GAME',
            productSlug='offline-game',
            price={'totalPrice': {'discountPrice': int(paid)}},
            promotions={
                'promotionalOffers': [
                    {
                        'promotionalOffers': [
                            {
                                'startDate': (
                                    now + timedelta(days=1) if future else now - timedelta(days=1)
                                ).isoformat(),
                                'endDate': (now + timedelta(days=7)).isoformat(),
                                'discountSetting': {'discountPercentage': 0},
                            }
                        ]
                    }
                ]
            },
        )

    response_data = {
        'data': {
            'Catalog': {
                'searchStore': {
                    'elements': [
                        offer('free'),
                        offer('paid', paid=True),
                        offer('future', future=True),
                        offer('subscription', subscription=True),
                    ]
                }
            }
        }
    }
    response = types.SimpleNamespace(json=lambda: response_data, raise_for_status=lambda: None)
    with tempfile.TemporaryDirectory(prefix='epic-offline-promotions-') as directory:
        namespace = dict(
            httpx=types.SimpleNamespace(get=lambda *args, **kwargs: response),
            suppress=suppress,
            datetime=datetime,
            timezone=timezone,
            JSONDecodeError=json.JSONDecodeError,
            json=json,
            logger=logger,
            RUNTIME_DIR=Path(directory),
            PromotionGame=PromotionGame,
            URL_PROMOTIONS='https://offline.invalid/',
            URL_PRODUCT_PAGE='https://offline.invalid/p/',
            URL_PRODUCT_BUNDLES='https://offline.invalid/bundles/',
        )
        extract('app/services/epic_games_service.py', {'get_promotions'}, namespace)
        assert [p.id for p in namespace['get_promotions']()] == ['free']
        response_data = {}
        try:
            namespace['get_promotions']()
        except RuntimeError:
            pass
        else:
            raise RuntimeError('Malformed promotion response was treated as an empty week')
    passed.append(
        'only current zero-price offers; paid, upcoming, subscription, malformed data blocked'
    )

    games = types.ModuleType('services.epic_games_service')
    games.EpicAgent = object
    games.EpicFreeGameRateLimitError = type('RateLimit', (RuntimeError,), {})
    promotion = PromotionGame(
        title='Offline free offer',
        id='offer-1',
        namespace='same-namespace',
        description='',
        offerType='BASE_GAME',
        url='https://offline.invalid/p/free',
    )
    games.get_promotions = lambda: [promotion]
    sys.modules['services.epic_games_service'] = games
    summary_module = importlib.import_module('services.epic_collection_summary_service')

    class OrderAgent:
        def __init__(self, before, after):
            self.snapshots, self.calls = iter([before, after]), 0

        async def refresh_order_keys(self):
            result = next(self.snapshots)
            if isinstance(result, Exception):
                raise result
            return result

        async def collect_epic_games(self):
            self.calls += 1

    verified = await summary_module.collect_epic_games_with_summary(
        OrderAgent(set(), {('same-namespace', 'offer-1')})
    )
    assert verified.newly_claimed_promotions == [promotion]
    for agent in (
        OrderAgent(set(), set()),
        OrderAgent(set(), RuntimeError('offline snapshot missing')),
        OrderAgent({('same-namespace', 'other-offer')}, {('same-namespace', 'other-offer')}),
    ):
        await expect_failure(
            summary_module.collect_epic_games_with_summary(agent),
            summary_module.EpicCollectionSummaryError,
        )
    unavailable = OrderAgent(RuntimeError('offline snapshot missing'), set())
    await expect_failure(
        summary_module.collect_epic_games_with_summary(unavailable),
        summary_module.EpicCollectionSummaryError,
    )
    assert unavailable.calls == 0
    passed.append(
        'exact offer order evidence; absent pre/post snapshots and unconfirmed claims fail'
    )

    login = dict(logger=logger, suppress=suppress, PlaywrightTimeoutError=PlaywrightTimeoutError)
    extract('app/services/epic_authorization_service.py', {'_get_login_status'}, login)

    class LoginPage:
        def locator(self, selector):
            if selector == '//egs-navigation':

                class MissingNavigation(Locator):
                    async def get_attribute(self, *args, **kwargs):
                        raise PlaywrightTimeoutError('offline navigation missing')

                return MissingNavigation()
            return Locator()

        def get_by_role(self, *args, **kwargs):
            return Locator()

        def get_by_test_id(self, *args, **kwargs):
            return Locator()

        url = 'https://offline.invalid/'
        context = types.SimpleNamespace(cookies=deny_network)

    subject = types.SimpleNamespace(
        page=LoginPage(), _needs_privacy_policy_correction=lambda: False
    )
    assert await login['_get_login_status'](subject) != 'true'
    passed.append('a stale cookie cannot establish authenticated status')

    print(
        json.dumps(
            {
                'result': 'passed',
                'offline_controls': passed,
                'epic_requests': 0,
                'llm_requests': 0,
                'captcha_solves': 0,
            },
            indent=2,
        )
    )


if __name__ == '__main__':
    asyncio.run(main())
