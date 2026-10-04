"""Targeted offline verification. Never loads settings/.env or existing credentials."""

import ast
import asyncio
import base64
import io
import json
import os
import socket
import stat
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

from extensions import chatgpt_oauth as oauth  # noqa: E402
from extensions.chatgpt_provider import ChatGPTModels, LocalFiles, completed_response  # noqa: E402
from extensions.runtime_failures import (  # noqa: E402
    EpicLlmConfigurationError,
    EpicLlmQuotaExhaustedError,
    llm_failure_kind,
)


def blocked(*args, **kwargs):
    raise AssertionError('Network is forbidden during offline verification.')


socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked
checks = []


def expect(kind, operation):
    try:
        operation()
    except kind:
        return
    raise AssertionError('Expected fail-closed outcome.')


def encode(data):
    if not isinstance(data, bytes):
        data = json.dumps(data).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
numbers = private_key.public_key().public_numbers()
jwk = {
    'kty': 'RSA',
    'kid': 'offline-key',
    'alg': 'RS256',
    'n': encode(numbers.n.to_bytes(256, 'big')),
    'e': encode(numbers.e.to_bytes(3, 'big')),
}
timestamp = 1800000000
metadata = {
    'issuer': oauth.ISSUER,
    'jwks_uri': oauth.ISSUER + '/.well-known/jwks.json',
    'revocation_endpoint': oauth.ISSUER + '/api/accounts/oauth/revoke',
    'id_token_signing_alg_values_supported': ['RS256'],
}
requests = []
behavior = {'refresh': 'ok', 'subject': 'offline-subject', 'revoke_status': 200}
pending = {}


def id_token(**changes):
    claims = {
        'iss': oauth.ISSUER,
        'aud': 'oaiapp_offline',
        'sub': behavior['subject'],
        'iat': timestamp,
        'exp': timestamp + 600,
        'nonce': pending.get('nonce', 'offline-nonce'),
    }
    claims.update(changes)
    signed = encode({'alg': 'RS256', 'kid': 'offline-key'}) + '.' + encode(claims)
    signature = private_key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signed + '.' + encode(signature)


def token_data():
    return {
        'access_token': 'offline-access',
        'refresh_token': 'offline-replacement',
        'id_token': id_token(),
        'token_type': 'Bearer',
        'expires_in': 600,
        'scope': behavior.get('scope', oauth.SCOPES),
    }


def mock_http(request):
    requests.append((request.method, str(request.url)))
    if str(request.url) == oauth.DISCOVERY:
        return httpx.Response(200, json=metadata)
    if str(request.url) == metadata['jwks_uri']:
        return httpx.Response(200, json={'keys': [jwk]})
    if str(request.url) == oauth.TOKEN:
        data = parse_qs(request.content.decode())
        assert data['client_id'] == ['oaiapp_offline'] and data['resource'] == [oauth.RESOURCE]
        if data['grant_type'] == ['refresh_token']:
            assert 'scope' not in data
            if behavior['refresh'] == 'revoked':
                return httpx.Response(400, json={'error': 'invalid_grant'})
            if behavior['refresh'] == 'temporary':
                return httpx.Response(503, json={'detail': 'offline failure'})
        else:
            assert data['redirect_uri'] == [pending['redirect_uri']]
            challenge = encode(
                __import__('hashlib').sha256(data['code_verifier'][0].encode()).digest()
            )
            assert challenge == pending['code_challenge']
            if behavior.get('exchange_fail'):
                return httpx.Response(400, json={'error': 'invalid_grant'})
        return httpx.Response(200, json=token_data())
    if str(request.url) == metadata['revocation_endpoint']:
        data = parse_qs(request.content.decode())
        assert data['token_type_hint'] == ['refresh_token']
        return httpx.Response(behavior['revoke_status'])
    raise AssertionError('Unexpected endpoint.')


def client_factory():
    return httpx.Client(transport=httpx.MockTransport(mock_http))


class FakeListener:
    def __init__(self, address, callback):
        assert address == ('127.0.0.1', 0)
        self.server_port, self.callback = 12345, callback

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def handle_request(self):
        callback = object.__new__(self.callback)
        callback.path = '/auth/callback?' + urlencode(
            {
                'code': 'offline-code',
                'state': pending['state'],
                'client_id': 'oaiapp_offline',
            }
        )
        callback.wfile = io.BytesIO()
        callback.send_response = callback.send_header = callback.end_headers = lambda *args: None
        callback.do_GET()


def fake_browser(url):
    assert url.startswith(oauth.AUTHORIZE + '?')
    pending.clear()
    pending.update({key: values[0] for key, values in parse_qs(urlsplit(url).query).items()})
    return True


def settings_class():
    # Preserve the upstream requirement that a Gemini compatibility field is nonempty.
    class AgentConfig(BaseSettings):
        GEMINI_API_KEY: SecretStr

        @classmethod
        def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
        ):
            return (init_settings,)  # Never inspect real environment/.env/secret files.

        @field_validator('GEMINI_API_KEY', mode='before')
        @classmethod
        def key_required(cls, value):
            if not isinstance(value, str) or not value:
                raise ValueError('Upstream compatibility field is empty.')
            return value

    tree = ast.parse((ROOT / 'app/settings.py').read_text())
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'EpicSettings'
    )
    namespace = dict(
        AgentConfig=AgentConfig,
        SettingsConfigDict=SettingsConfigDict,
        Field=Field,
        SecretStr=SecretStr,
        model_validator=model_validator,
        Path=Path,
        os=os,
        _env=lambda name, default=None: default,
        _coerce_secret_input=lambda value: value,
        HCAPTCHA_DIR=Path('/offline'),
        USER_DATA_DIR=Path('/offline'),
    )
    exec(compile(ast.Module(body=[cls], type_ignores=[]), 'offline-settings', 'exec'), namespace)
    result = namespace['EpicSettings']
    result.model_config['env_file'] = None
    return result


class Answer(BaseModel):
    points: list[list[int]]


async def lines(events):
    for event in events:
        yield 'data: ' + json.dumps(event)
        yield ''


def completed(text='{"points": [[10, 20]]}'):
    return {
        'type': 'response.completed',
        'response': {
            'status': 'completed',
            'output': [
                {
                    'type': 'message',
                    'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': text}],
                },
            ],
        },
    }


async def verify_provider(store):
    files = LocalFiles()
    image = store.root / 'synthetic.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n')
    upload = await files.upload(image)
    contents = [
        SimpleNamespace(
            role='user',
            parts=[
                SimpleNamespace(
                    file_data=SimpleNamespace(file_uri=upload.uri), text=None, inline_data=None
                ),
                SimpleNamespace(text='Inspect this synthetic fixture.'),
            ],
        )
    ]
    config = SimpleNamespace(
        response_schema=Answer,
        system_instruction='Offline fixture',
        temperature=0.5,
        thinking_config=object(),
    )
    seen = []

    def transport(request):
        assert request.url == oauth.RESOURCE + '/responses'
        data = json.loads(request.content)
        assert request.headers['authorization'] == 'Bearer offline-access'
        assert set(data) == {'model', 'instructions', 'input', 'store', 'stream'}
        assert data['stream'] is True and data['store'] is False
        assert data['input'][0]['content'][0]['type'] == 'input_image'
        assert data['input'][0]['content'][0]['image_url'].startswith('data:image/png;base64,')
        seen.append(data)
        content = 'data: ' + json.dumps(completed()) + '\n\n'
        return httpx.Response(
            200, text=content, headers={'content-type': 'Text/Event-Stream; Charset=UTF-8'}
        )

    models = ChatGPTModels(
        SimpleNamespace(access_token=lambda: 'offline-access'),
        files,
        5,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    response = await models.generate_content('offline-model', contents, config=config)
    assert response.parsed.points == [[10, 20]] and response.model_dump()['parsed']['points'] == [
        [10, 20]
    ]
    assert len(seen) == 1
    checks.append('local_images_public_responses_allowlist_and_schema')
    checks.append('sse_media_type_is_case_insensitive_and_accepts_parameters')

    def unexpected_json(request):
        return httpx.Response(
            200, json={'detail': 'private-offline-fixture'}, headers={'x-request-id': 'req_offline'}
        )

    bad_models = ChatGPTModels(
        SimpleNamespace(access_token=lambda: 'offline-access'),
        files,
        5,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(unexpected_json)),
    )
    try:
        await bad_models.generate_content('offline-model', contents, config=config)
    except EpicLlmConfigurationError as error:
        assert 'body_shape=detail' in str(error) and 'req_offline' in str(error)
        assert 'private-offline-fixture' not in str(error) and 'offline-access' not in str(error)
    else:
        raise AssertionError('Unexpected JSON response was accepted.')
    checks.append('non_sse_json_stops_with_safe_diagnostics_and_never_exposes_body_or_token')

    def missing_media_type(request):
        content = 'event: response.completed\ndata: ' + json.dumps(completed()) + '\n\n'
        return httpx.Response(200, content=content.encode())

    missing_header_models = ChatGPTModels(
        SimpleNamespace(access_token=lambda: 'offline-access'),
        files,
        5,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(missing_media_type)),
    )
    recovered = await missing_header_models.generate_content(
        'offline-model', contents, config=config
    )
    assert recovered.parsed.points == [[10, 20]]
    checks.append('framed_sse_without_media_type_requires_completed_and_valid_schema')

    def missing_header_incomplete(request):
        content = (
            'event: response.output_text.delta\ndata: '
            + json.dumps(
                {
                    'type': 'response.output_text.delta',
                    'delta': 'partial',
                }
            )
            + '\n\n'
        )
        return httpx.Response(200, content=content.encode())

    partial_models = ChatGPTModels(
        SimpleNamespace(access_token=lambda: 'offline-access'),
        files,
        5,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(missing_header_incomplete)
        ),
    )
    try:
        await partial_models.generate_content('offline-model', contents, config=config)
    except RuntimeError:
        pass
    else:
        raise AssertionError('A headerless partial stream was accepted.')
    checks.append('headerless_sse_partial_output_still_fails_closed')
    for events, expected in [
        ([{'type': 'response.output_text.delta', 'delta': 'partial'}], RuntimeError),
        ([{'type': 'response.incomplete'}], RuntimeError),
        (
            [
                {
                    'type': 'response.failed',
                    'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}},
                }
            ],
            EpicLlmQuotaExhaustedError,
        ),
        (
            [
                {
                    'type': 'response.failed',
                    'response': {'error': {'code': 'subscription_sharing_user_not_eligible'}},
                }
            ],
            EpicLlmConfigurationError,
        ),
    ]:
        try:
            await completed_response(lines(events))
        except expected as error:
            if isinstance(error, EpicLlmQuotaExhaustedError):
                assert llm_failure_kind(error) == 'daily_quota' and oauth.USAGE_URL in str(error)
        else:
            raise AssertionError('Partial/failed stream was accepted.')
    checks.append('interrupted_incomplete_failed_and_quota_streams_stop')
    assert (await completed_response(lines([completed()])))[0].startswith('{')


def main():
    original_server, original_actions = oauth.HTTPServer, os.environ.pop('GITHUB_ACTIONS', None)
    try:
        with tempfile.TemporaryDirectory(prefix='epic-chatgpt-offline-') as temporary:
            store = oauth.CredentialStore(Path(temporary) / 'credentials')
            session = oauth.OAuthSession(
                'personal', store=store, client_factory=client_factory, clock=lambda: timestamp
            )
            oauth.HTTPServer = FakeListener
            result = session.sign_in(opener=fake_browser)
            assert result['connected'] and result['plan_usage_enabled']
            assert pending['client_id'] == 'dynamic_agent_client'
            host_id = pending['ext_agent_host_id']
            assert pending['agent_name_hint'] == 'Epic Freebies Helper'
            assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
            assert stat.S_IMODE(store._path('personal').stat().st_mode) == 0o600
            session.sign_in(opener=fake_browser)
            assert pending['client_id'] == 'oaiapp_offline' and 'agent_name_hint' not in pending
            assert pending['ext_agent_host_id'] == host_id and pending['id_token_hint']
            checks.append(
                'pkce_loopback_issued_registration_stable_host_and_private_atomic_storage'
            )
            for changes in [
                {'nonce': 'wrong'},
                {'aud': 'wrong'},
                {'iss': 'wrong'},
                {'exp': timestamp - 10},
                {'sub': ''},
                {'iat': timestamp + 10},
            ]:
                expect(
                    EpicLlmConfigurationError,
                    lambda: oauth.verify_identity(
                        id_token(**changes),
                        'oaiapp_offline',
                        pending['nonce'],
                        client_factory(),
                        now=timestamp,
                    ),
                )
            corrupted = id_token().rsplit('.', 1)[0] + '.' + encode(b'bad-signature')
            expect(
                EpicLlmConfigurationError,
                lambda: oauth.verify_identity(
                    corrupted, 'oaiapp_offline', pending['nonce'], client_factory(), now=timestamp
                ),
            )
            checks.append('id_token_signature_issuer_audience_expiry_nonce_subject_validated')
            for query in [
                dict(state=['bad'], code=['fixture']),
                dict(state=[pending['state']], error=['access_denied']),
                dict(state=[pending['state']], client_id=['other'], code=['fixture']),
            ]:
                expect(EpicLlmConfigurationError, lambda: oauth.validate_callback(query, pending))
            checks.append('callback_state_decline_and_registration_mismatch_rejected')
            behavior['subject'] = 'other-subject'
            expect(EpicLlmConfigurationError, lambda: session.sign_in(opener=fake_browser))
            behavior['subject'] = 'offline-subject'
            assert store.read('personal')['subject'] == 'offline-subject'
            checks.append('reauthorization_cannot_replace_selected_account_identity')
            saved = store.read('personal')
            saved['expires_at'] = timestamp - 1
            store.write('personal', saved)
            assert session.access_token() == 'offline-access'
            assert store.read('personal')['refresh_token'] == 'offline-replacement'
            checks.append('expired_access_token_refresh_rotates_whole_record')
            store.write('personal', saved)
            before = len([request for request in requests if request[1] == oauth.TOKEN])
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: session.access_token(), range(2)))
            assert results == ['offline-access', 'offline-access']
            assert len([request for request in requests if request[1] == oauth.TOKEN]) == before + 1
            checks.append('concurrent_refresh_uses_one_rotating_refresh_token_exchange')
            for mode in ['temporary', 'revoked']:
                record = dict(saved)
                store.write('personal', record)
                behavior['refresh'] = mode
                expect(EpicLlmConfigurationError, session.access_token)
                assert bool(store.read('personal').get('refresh_token')) == (mode == 'temporary')
            checks.append(
                'temporary_refresh_preserves_credentials_terminal_revocation_clears_tokens'
            )
            behavior['refresh'] = 'ok'
            saved['expires_at'] = timestamp + 600
            saved['scopes'] = ['openid']
            store.write('personal', saved)
            before = len(requests)
            expect(EpicLlmConfigurationError, session.access_token)
            assert len(requests) == before
            checks.append('identity_only_scope_never_sends_inference_or_refresh')
            behavior['scope'] = 'openid profile email'
            result = session.sign_in(opener=fake_browser)
            assert result['connected'] and not result['plan_usage_enabled']
            expect(EpicLlmConfigurationError, session.access_token)
            behavior.pop('scope')
            checks.append('identity_only_sign_in_is_retained_with_plan_usage_disabled')
            assert 'prompt' not in pending
            session.sign_in(opener=fake_browser, request_plan_consent=True)
            assert pending['prompt'] == 'consent' and pending['scope'] == oauth.SCOPES
            checks.append('plan_reconsent_is_explicit_and_ordinary_login_does_not_force_consent')
            store.write('personal', dict(saved, scopes=oauth.SCOPES.split()))
            result = session.sign_out()
            assert result['remote_revocation_confirmed'] and not store.read('personal').get(
                'access_token'
            )
            store.write('personal', dict(saved, scopes=oauth.SCOPES.split()))
            behavior['revoke_status'] = 503
            result = session.sign_out()
            assert not result['remote_revocation_confirmed'] and not store.read('personal').get(
                'refresh_token'
            )
            assert (
                store.read('personal')['client_id'] == 'oaiapp_offline'
                and store.host_id() == host_id
            )
            checks.append(
                'logout_attempts_remote_revocation_clears_local_tokens_retains_registration'
            )
            target = store.root / 'unsafe.json'
            target.symlink_to(store._path('personal'))
            expect(OSError, lambda: store.read('unsafe'))
            target.unlink()
            target.write_text('{}')
            target.chmod(0o644)
            expect(EpicLlmConfigurationError, lambda: store.read('unsafe'))
            target.unlink()
            checks.append('credential_symlinks_and_public_file_permissions_rejected')
            asyncio.run(verify_provider(store))
            model = settings_class()
            configured = model(
                _env_file=None,
                LLM_PROVIDER='chatgpt',
                CHATGPT_MODEL='offline-model',
                GEMINI_API_KEY='unused-fixture',
                GLM_API_KEY='unused-fixture',
            )
            assert (
                configured.LLM_PROVIDER == 'chatgpt'
                and configured.GEMINI_API_KEY.get_secret_value() == 'chatgpt-oauth-adapter'
            )
            assert (
                configured.IMAGE_CLASSIFIER_MODEL == 'offline-model'
                and not configured.llm_configuration_error
            )
            default_chatgpt = model(_env_file=None, LLM_PROVIDER='chatgpt')
            assert default_chatgpt.CHATGPT_MODEL == 'gpt-6.1-sol'
            routes = (
                'CHALLENGE_CLASSIFIER_MODEL',
                'IMAGE_CLASSIFIER_MODEL',
                'SPATIAL_POINT_REASONER_MODEL',
                'SPATIAL_PATH_REASONER_MODEL',
            )
            assert all(getattr(default_chatgpt, route) == 'gpt-6.1-sol' for route in routes)
            assert not default_chatgpt.llm_configuration_error
            for route in routes:
                overridden = model(
                    _env_file=None,
                    LLM_PROVIDER='chatgpt',
                    CHATGPT_MODEL='offline-other-model',
                    **{route: 'offline-task-model'},
                )
                assert getattr(overridden, route) == 'offline-task-model'
                assert all(
                    getattr(overridden, other) == 'offline-other-model'
                    for other in routes
                    if other != route
                )
            checks.append('subscription_default_and_explicit_task_model_overrides_preserved')
            assert model(_env_file=None, GEMINI_API_KEY='offline-fixture').LLM_PROVIDER == 'gemini'
            assert (
                model(
                    _env_file=None, LLM_PROVIDER='glm', GLM_API_KEY='offline-fixture'
                ).LLM_PROVIDER
                == 'glm'
            )
            os.environ['GITHUB_ACTIONS'] = 'true'
            expect(EpicLlmConfigurationError, lambda: oauth.OAuthSession(store=store))
            assert configured.llm_configuration_error
            checks.append('explicit_chatgpt_routing_no_api_key_fallback_and_actions_rejected')
            assert 'settings' not in sys.modules and 'google.genai' not in sys.modules
        print(
            json.dumps(
                {
                    'passed': checks,
                    'real_network_requests': 0,
                    'oauth_authorizations': 0,
                    'llm_requests': 0,
                    'epic_requests': 0,
                    'captcha_solves': 0,
                },
                indent=2,
            )
        )
    finally:
        oauth.HTTPServer = original_server
        if original_actions is None:
            os.environ.pop('GITHUB_ACTIONS', None)
        else:
            os.environ['GITHUB_ACTIONS'] = original_actions


if __name__ == '__main__':
    main()
