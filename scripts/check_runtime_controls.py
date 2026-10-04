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
    EpicCaptchaRequiredError,
    EpicLlmQuotaExhaustedError,
    EpicNonRetryableError,
    llm_failure_kind,
    llm_retry_delay,
)
from models import PromotionGame  # noqa: E402


class Signal(Enum):
    SUCCESS = 'success'


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


def extract(path, names, namespace):
    """Load selected real functions without the service's settings/import side effects."""
    tree = ast.parse((ROOT / path).read_text())
    nodes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
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


async def main():
    passed = []
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
        wait_for_challenge_signal(agent, context='offline', timeout_seconds=1),
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
