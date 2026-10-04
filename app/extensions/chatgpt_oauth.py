"""App-owned Sign in with ChatGPT credentials; no Codex credentials or API keys."""

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import time
import uuid
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from extensions.runtime_failures import EpicLlmConfigurationError, EpicLlmQuotaExhaustedError

ISSUER = 'https://auth.openai.com'
AUTHORIZE = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
DISCOVERY = ISSUER + '/.well-known/openid-configuration'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
PLAN_SCOPES = {'resource.invoke', 'chatgpt.tokens.use.direct'}
USAGE_URL = 'https://chatgpt.com/settings/usage'
TOKEN_FIELDS = ('access_token', 'refresh_token', 'id_token', 'scopes', 'expires_at')
INVALID_REFRESH = {
    'invalid_grant',
    'invalid_refresh_token',
    'token_expired',
    'refresh_token_expired',
    'refresh_token_invalidated',
    'refresh_token_reused',
}


def require_local_runtime():
    if os.environ.get('GITHUB_ACTIONS', '').lower() == 'true':
        raise EpicLlmConfigurationError(
            'ChatGPT OAuth is enabled only for local/self-hosted processes outside Actions. '
            'Do not upload OAuth credentials to GitHub.'
        )


def _unb64(value):
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


class CredentialStore:
    """0600 atomic records, 0700 directory, and per-profile cross-process locking."""

    def __init__(self, root=None):
        self.root = Path(root) if root else Path.home() / '.config/epic-freebies-helper/chatgpt'

    def _prepare(self):
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise EpicLlmConfigurationError('Unsafe ChatGPT credential directory.')
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise EpicLlmConfigurationError('ChatGPT credential directory must have mode 0700.')

    def _path(self, profile):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', profile):
            raise EpicLlmConfigurationError('Use a short alphanumeric ChatGPT profile label.')
        return self.root / (profile + '.json')

    def _open(self, path, flags):
        fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise EpicLlmConfigurationError('Unsafe ChatGPT credential file permissions.')
        return fd

    @contextmanager
    def locked(self, profile):
        self._prepare()
        path = self._path(profile).with_suffix('.lock')
        fd = self._open(path, os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + 5
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise EpicLlmConfigurationError('ChatGPT profile is busy; try again later.')
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def read(self, profile):
        try:
            fd = self._open(self._path(profile), os.O_RDONLY)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, 'r', encoding='utf-8') as source:
            try:
                content = source.read(65537)
                if len(content) > 65536:
                    raise ValueError
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError
                return data
            except (ValueError, UnicodeError):
                raise EpicLlmConfigurationError('Invalid ChatGPT credential record.') from None

    def write(self, profile, record):
        self._prepare()
        path = self._path(profile)
        if path.exists() or path.is_symlink():
            fd = self._open(path, os.O_RDONLY)
            os.close(fd)
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as target:
                json.dump(record, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def host_id(self):
        with self.locked('host'):
            record = self.read('host')
            if not record:
                record = {'ext_agent_host_id': 'urn:uuid:' + str(uuid.uuid4())}
                self.write('host', record)
            host_id = record.get('ext_agent_host_id', '')
            if not host_id.startswith('urn:uuid:'):
                raise EpicLlmConfigurationError('Invalid ChatGPT host ID.')
            try:
                if uuid.UUID(host_id[9:]).version != 4:
                    raise ValueError
            except ValueError:
                raise EpicLlmConfigurationError('Invalid ChatGPT host ID.') from None
            return host_id


def _auth_endpoint(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'auth.openai.com' or parsed.fragment:
        raise EpicLlmConfigurationError('Untrusted OpenAI discovery endpoint.')
    return url


def discovery(client):
    response = client.get(DISCOVERY)
    if response.status_code != 200:
        raise EpicLlmConfigurationError('Cannot load OpenAI OIDC discovery.')
    data = response.json()
    if data.get('issuer') != ISSUER:
        raise EpicLlmConfigurationError('Unexpected OpenAI token issuer.')
    for key in ('jwks_uri', 'revocation_endpoint'):
        _auth_endpoint(data.get(key, ''))
    return data


def verify_identity(id_token, client_id, nonce, client, *, now=None):
    """Validate the documented RS256 ID token with issuer JWKS before trusting claims."""
    try:
        if not isinstance(id_token, str) or len(id_token) > 16384:
            raise ValueError
        header64, payload64, signature64 = id_token.split('.')
        header, claims = json.loads(_unb64(header64)), json.loads(_unb64(payload64))
        metadata = discovery(client)
        if header.get('alg') != 'RS256' or 'RS256' not in metadata.get(
            'id_token_signing_alg_values_supported', []
        ):
            raise ValueError
        response = client.get(metadata['jwks_uri'])
        if response.status_code != 200:
            raise ValueError
        keys = [key for key in response.json()['keys'] if key.get('kid') == header.get('kid')]
        if len(keys) != 1 or not header.get('kid'):
            raise ValueError
        key = keys[0]
        if key.get('kty') != 'RSA' or key.get('use', 'sig') != 'sig':
            raise ValueError
        if key.get('alg', 'RS256') != 'RS256' or 'verify' not in key.get('key_ops', ['verify']):
            raise ValueError
        public_key = rsa.RSAPublicNumbers(
            int.from_bytes(_unb64(key['e']), 'big'), int.from_bytes(_unb64(key['n']), 'big')
        ).public_key()
        if public_key.key_size < 2048:
            raise ValueError
        public_key.verify(
            _unb64(signature64),
            (header64 + '.' + payload64).encode('ascii'),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        timestamp = time.time() if now is None else now
        audience = claims.get('aud')
        if claims.get('iss') != ISSUER or client_id not in (
            audience if isinstance(audience, list) else [audience]
        ):
            raise ValueError
        if claims.get('azp', client_id) != client_id:
            raise ValueError
        for field in ('exp', 'iat'):
            if type(claims.get(field)) is not int:
                raise ValueError
        if claims['exp'] <= timestamp - 5 or claims['iat'] > timestamp + 5:
            raise ValueError
        if 'nbf' in claims and (type(claims['nbf']) is not int or claims['nbf'] > timestamp + 5):
            raise ValueError
        if nonce is not None and not hmac.compare_digest(str(claims.get('nonce', '')), nonce):
            raise ValueError
        if not isinstance(claims.get('sub'), str) or not claims['sub']:
            raise ValueError
        return {'issuer': ISSUER, 'subject': claims['sub'], 'email': claims.get('email', '')}
    except Exception:
        raise EpicLlmConfigurationError('OpenAI ID token validation failed.') from None


def authorization_parameters(host_id, redirect_uri, *, saved=None, request_plan_consent=False):
    saved = saved or {}
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    )
    client_id = saved.get('client_id') or 'dynamic_agent_client'
    params = dict(
        client_id=client_id,
        ext_agent_host_id=host_id,
        response_type='code',
        redirect_uri=redirect_uri,
        scope=SCOPES,
        resource=RESOURCE,
        state=state,
        nonce=nonce,
        code_challenge_method='S256',
        code_challenge=challenge,
    )
    if client_id == 'dynamic_agent_client':
        params['agent_name_hint'] = 'Epic Freebies Helper'
    elif saved.get('id_token'):
        params['id_token_hint'] = saved['id_token']
    if request_plan_consent:
        params['prompt'] = 'consent'  # OAuth consent parameter, never a Responses body field.
    return params, verifier


def validate_callback(query, params):
    def one(key):
        values = query.get(key, [])
        if len(values) > 1:
            raise EpicLlmConfigurationError('Invalid OAuth callback.')
        return values[0] if values else ''

    if not hmac.compare_digest(one('state'), params['state']):
        raise EpicLlmConfigurationError('OAuth callback state did not match.')
    if one('error'):
        raise EpicLlmConfigurationError('ChatGPT authorization was declined or interrupted.')
    issued = one('client_id')
    original = params['client_id']
    if original == 'dynamic_agent_client':
        if not issued or issued == original:
            raise EpicLlmConfigurationError('ChatGPT registration is incomplete.')
    elif issued and issued != original:
        raise EpicLlmConfigurationError('OAuth callback changed the selected registration.')
    if not one('code'):
        raise EpicLlmConfigurationError('OAuth callback has no authorization code.')
    return one('code'), issued or original


def _error_code(response):
    try:
        data = response.json()
        error = data.get('error')
        code = error.get('code', '') if isinstance(error, dict) else error
        return code if isinstance(code, str) else ''
    except (ValueError, AttributeError):
        return ''


class OAuthSession:
    def __init__(self, profile='default', *, store=None, client_factory=None, clock=time.time):
        require_local_runtime()
        if profile.lower() == 'host':
            raise EpicLlmConfigurationError('The ChatGPT profile label "host" is reserved.')
        self.profile, self.store, self.clock = profile.lower(), store or CredentialStore(), clock
        self.store._path(self.profile)
        self.client_factory = client_factory or (
            lambda: httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
        )

    def _clear(self, record):
        record = {key: value for key, value in record.items() if key not in TOKEN_FIELDS}
        self.store.write(self.profile, record)
        return record

    def _token_record(self, data, old, client, *, nonce=None, initial=False):
        try:
            expiry = data['expires_in']
            if type(expiry) not in (int, float) or not 0 < expiry <= 86400:
                raise ValueError
            if data.get('token_type', '').lower() != 'bearer':
                raise ValueError
            if not isinstance(data.get('access_token'), str) or not data['access_token']:
                raise ValueError
            if not isinstance(data.get('scope'), str):
                raise ValueError
            scopes = data['scope'].split()
            refresh = data.get('refresh_token', '')
            if not isinstance(refresh, str) or ('offline_access' in scopes and not refresh):
                raise ValueError
            identity = {key: old.get(key, '') for key in ('issuer', 'subject', 'email')}
            if initial or data.get('id_token'):
                identity = verify_identity(
                    data.get('id_token'), old['client_id'], nonce, client, now=self.clock()
                )
                if old.get('subject') and identity['subject'] != old['subject']:
                    raise ValueError
            return dict(
                old,
                **identity,
                access_token=data['access_token'],
                refresh_token=refresh,
                id_token=data.get('id_token') or old.get('id_token', ''),
                scopes=scopes,
                expires_at=self.clock() + expiry,
            )
        except EpicLlmConfigurationError:
            raise
        except (KeyError, TypeError, ValueError):
            raise EpicLlmConfigurationError('Invalid OpenAI OAuth token response.') from None

    def sign_in(self, *, opener=webbrowser.open, timeout=300, request_plan_consent=False):
        host_id = self.store.host_id()
        with self.store.locked(self.profile):
            old = self.store.read(self.profile)
            result = {}

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # Callback URLs contain authorization codes; never log them.

                def do_GET(self):
                    url = urlsplit(self.path)
                    if url.path != '/auth/callback':
                        self.send_error(404)
                        return
                    query = parse_qs(url.query, keep_blank_values=True)
                    if query.get('state') != [params['state']]:
                        self.send_error(400)
                        return
                    try:
                        result['callback'] = validate_callback(query, params)
                    except EpicLlmConfigurationError as error:
                        result['error'] = error
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(b'Return to Epic Freebies Helper to see the sign-in result.')

            class LoopbackServer(HTTPServer):
                def get_request(self):
                    connection, address = super().get_request()
                    connection.settimeout(2)
                    return connection, address

            with LoopbackServer(('127.0.0.1', 0), Callback) as server:
                server.timeout = 0.5
                uri = f'http://127.0.0.1:{server.server_port}/auth/callback'
                params, verifier = authorization_parameters(
                    host_id,
                    uri,
                    saved=old,
                    request_plan_consent=request_plan_consent,
                )
                if not opener(AUTHORIZE + '?' + urlencode(params)):
                    raise EpicLlmConfigurationError('Could not open the system browser.')
                deadline = time.monotonic() + timeout
                while not result and time.monotonic() < deadline:
                    server.handle_request()
            if 'error' in result:
                raise result['error']
            if 'callback' not in result:
                raise EpicLlmConfigurationError('ChatGPT sign-in timed out.')
            code, issued = result['callback']
            old.update(client_id=issued, ext_agent_host_id=host_id)
            self.store.write(self.profile, old)  # Retain issued registration if exchange fails.
            with self.client_factory() as client:
                response = client.post(
                    TOKEN,
                    data=dict(
                        grant_type='authorization_code',
                        client_id=issued,
                        code=code,
                        code_verifier=verifier,
                        redirect_uri=uri,
                        resource=RESOURCE,
                    ),
                )
                if response.status_code != 200:
                    raise EpicLlmConfigurationError('ChatGPT code exchange failed; sign in again.')
                record = self._token_record(
                    response.json(), old, client, nonce=params['nonce'], initial=True
                )
            self.store.write(self.profile, record)
        return self.status()

    def status(self):
        with self.store.locked(self.profile):
            record = self.store.read(self.profile)
        return dict(
            profile=self.profile,
            connected=bool(record.get('access_token')),
            plan_usage_enabled=PLAN_SCOPES <= set(record.get('scopes', [])),
            access_token_expired=self.clock() >= record.get('expires_at', 0),
            manage_usage=USAGE_URL,
        )

    def access_token(self):
        with self.store.locked(self.profile):
            record = self.store.read(self.profile)
            if not record.get('access_token') or not record.get('subject'):
                raise EpicLlmConfigurationError('Sign in with ChatGPT before using this provider.')
            if not PLAN_SCOPES <= set(record.get('scopes', [])):
                raise EpicLlmConfigurationError('ChatGPT plan usage permission was not granted.')
            if self.clock() + 60 >= record.get('expires_at', 0):
                if not record.get('refresh_token'):
                    self._clear(record)
                    raise EpicLlmConfigurationError('ChatGPT session expired; sign in again.')
                with self.client_factory() as client:
                    response = client.post(
                        TOKEN,
                        data=dict(
                            grant_type='refresh_token',
                            client_id=record['client_id'],
                            refresh_token=record['refresh_token'],
                            resource=RESOURCE,
                        ),
                    )
                    if response.status_code != 200:
                        if _error_code(response) in INVALID_REFRESH:
                            self._clear(record)
                            raise EpicLlmConfigurationError(
                                'ChatGPT session revoked or expired; sign in again.'
                            )
                        raise EpicLlmConfigurationError(
                            'ChatGPT refresh failed; request stopped. Try later.'
                        )
                    record = self._token_record(response.json(), record, client)
                    self.store.write(self.profile, record)
                if not PLAN_SCOPES <= set(record['scopes']):
                    raise EpicLlmConfigurationError(
                        'ChatGPT plan usage permission was not granted.'
                    )
            return record['access_token']

    def sign_out(self):
        confirmed = False
        with self.store.locked(self.profile):
            record = self.store.read(self.profile)
            try:
                if record.get('refresh_token'):
                    with self.client_factory() as client:
                        endpoint = discovery(client)['revocation_endpoint']
                        for attempt in range(2):
                            try:
                                response = client.post(
                                    endpoint,
                                    data=dict(
                                        token=record['refresh_token'],
                                        token_type_hint='refresh_token',
                                        client_id=record['client_id'],
                                    ),
                                )
                                confirmed = response.status_code == 200
                                if confirmed or response.status_code < 500:
                                    break
                            except httpx.TransportError:
                                pass
                            if attempt == 0:
                                time.sleep(1)
            except (httpx.HTTPError, EpicLlmConfigurationError, ValueError):
                pass
            finally:
                self._clear(record)
        return {
            'profile': self.profile,
            'remote_revocation_confirmed': confirmed,
            'message': 'Local tokens cleared. If revocation was not confirmed, disconnect '
            'the app in ChatGPT Settings.',
            'manage_usage': USAGE_URL,
        }


def error_details(response):
    try:
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get('error'), dict):
            return {
                'code': _error_code(response),
                'body_shape': 'error_object',
                'param': data['error'].get('param') or '',
            }
        return {
            'code': _error_code(response),
            'body_shape': 'detail' if isinstance(data, dict) and 'detail' in data else 'other_json',
        }
    except ValueError:
        return {'body_shape': 'non_json'}


def response_error(
    *, status=0, code='', request_id='', retry_after=None, param='', body_shape='SSE'
):
    """Expose safe machine metadata without response bodies or bearer credentials."""
    known = (
        code
        if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,80}', code)
        else 'unknown_error'
    )
    request_id = (
        request_id
        if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,120}', request_id)
        else ''
    )
    param = param if isinstance(param, str) and re.fullmatch(r'[a-z0-9_.\[\]]{1,80}', param) else ''
    message = (
        f'ChatGPT request stopped (HTTP {status}, code={known}, param={param}, '
        f'body_shape={body_shape}, request_id={request_id}).'
    )
    if code in {'subscription_sharing_usage_limit_exceeded', 'insufficient_quota'}:
        raise EpicLlmQuotaExhaustedError(message + ' Manage usage: ' + USAGE_URL)
    if status in (400, 401, 403) or code in {
        'subscription_sharing_user_not_eligible',
        'subscription_sharing_unsupported_capability',
        'subscription_sharing_route_not_supported',
        'subscription_sharing_invalid_user',
        'chatpass_v2_scope_not_authorized',
        'chatpass_v2_invalid_authorization_context',
    }:
        raise EpicLlmConfigurationError(message + ' Review permissions/model; no billing fallback.')
    error = RuntimeError(message)
    error.code = status
    if retry_after:
        from types import SimpleNamespace

        error.response = SimpleNamespace(status_code=status, headers={'retry-after': retry_after})
    raise error
