"""Fixed synthetic-image probe. Production never imports this recording module.

Replay is offline and uses the real ChatGPTModels adapter with a mock session/HTTP
transport. live-once requires a NEW single-request approval; it is never automatic.
"""

import argparse
import asyncio
import codecs
import hashlib
import json
import math
import os
import re
import socket
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

from extensions.chatgpt_provider import ChatGPTModels, LocalFiles, completed_response  # noqa: E402

MODEL = 'gpt-5.6-luna'
PROBE_MODELS = (MODEL, 'gpt-6.1-sol')
WIDTH, HEIGHT = 256, 192
IMAGE_HASH = '4fbb440df62760c75ee1272a30f9c8fa0fddce17f2a395ff8b17e99b0cb32636'
ENDPOINT = 'https://api.openai.com/v1/responses'
LIMIT = 256 * 1024
PROMPT = (
    'Describe all colored geometric shapes in this image, excluding the white background. '
    'For each, give its basic shape, color, and center in integer native-image pixel coordinates. '
    'The image is 256 pixels wide and 192 pixels high; origin is top-left. '
    'Return compact JSON only. No explanations.'
)
SYSTEM = 'Analyze only the supplied synthetic image.'


class Shape(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    color: Literal['red', 'blue', 'green', 'yellow', 'black', 'white']
    shape: Literal['square', 'circle', 'triangle', 'rectangle']
    center_x: int = Field(ge=0, lt=WIDTH)
    center_y: int = Field(ge=0, lt=HEIGHT)


class ImageReport(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    shapes: list[Shape] = Field(min_length=1, max_length=8)


def synthetic_png():
    def chunk(kind, data):
        return (
            struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
        )

    rows = []
    for y in range(HEIGHT):
        row = bytearray(b'\x00')
        for x in range(WIDTH):
            color = (255, 255, 255)
            if 32 <= x <= 95 and 32 <= y <= 95:
                color = (230, 30, 40)
            if (x - 184) ** 2 + (y - 128) ** 2 <= 32**2:
                color = (30, 100, 230)
            row.extend(color)
        rows.append(bytes(row))
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('!IIBBBBB', WIDTH, HEIGHT, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')
    if hashlib.sha256(png).hexdigest() != IMAGE_HASH:
        raise RuntimeError('Synthetic image changed; stop before any request.')
    return png


def contract(model=MODEL):
    if model not in PROBE_MODELS:
        raise ValueError('Unsupported explicit synthetic-probe model; no fallback.')
    return {
        'model': model,
        'image_sha256': IMAGE_HASH,
        'width': WIDTH,
        'height': HEIGHT,
        'prompt': PROMPT,
        'system_instruction': SYSTEM,
        'schema': ImageReport.model_json_schema(),
        'reasoning_effort': 'low',
        'image_detail': 'low',
        'store': False,
        'stream': True,
        'timeout_seconds': 60,
        'max_post_attempts': 1,
        'retries': 0,
    }


class Sanitizer:
    """Allowlisted response fields only; no request headers, identity or metadata."""

    patterns = (
        r'(?i)\bBearer\s+[^\s"\'<>]+',
        r'(?i)\b(?:access_token|refresh_token|id_token|api[_-]?key|authorization|cookie|'
        r'client_id|host_id|account_id|user_id|subject)\b["\s:=]+[^\s,;"\'<>]+',
        r'\bsk-[A-Za-z0-9_-]{8,}\b',
        r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b',
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
        r'\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b',
        r'(?i)\b(?:acct|user|org|oaiapp|host|client)[_-][A-Za-z0-9_-]{5,}\b',
        r'https?://[^\s"\'<>]+',
        r'(?:/Users/|/home/)[^\s"\'<>]+',
    )

    def __init__(self):
        self.redacted = False

    def text(self, value):
        for pattern in self.patterns:
            previous = str(value)
            value = re.sub(
                pattern,
                lambda match: (
                    match.group()
                    if match.group() == 'https://chatgpt.com/settings/usage'
                    else '[REDACTED]'
                ),
                previous,
            )
            self.redacted |= value != previous
        return value

    def tree(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if re.search(
                    r'(?i)(?:access|refresh|id)[_-]?token|secret|password|cookie|authorization|email|'
                    r'account.?id|user.?id|client.?id|host.?id|subject',
                    key,
                ):
                    result['[REDACTED FIELD]'] = '[REDACTED]'
                    self.redacted = True
                else:
                    result[self.text(key)] = self.tree(item)
            return result
        if type(value) is float and not math.isfinite(value):
            self.redacted = True
            return '[NON-FINITE]'
        return value

    def fields(self, value, keys):
        if not isinstance(value, dict):
            return {}
        return {key: self.tree(value[key]) for key in keys if key in value}

    def part(self, value):
        return self.fields(value, ('type', 'text', 'refusal', 'json'))

    def item(self, value):
        result = self.fields(value, ('type', 'role', 'status'))
        if isinstance(value, dict) and isinstance(value.get('content'), list):
            result['content'] = [self.part(part) for part in value['content']]
        return result

    def response(self, value):
        result = self.fields(value, ('object', 'status', 'model'))
        if not isinstance(value, dict):
            return result
        if isinstance(value.get('output'), list):
            result['output'] = [self.item(item) for item in value['output']]
        for key, fields in (
            ('error', ('type', 'code', 'param', 'message')),
            ('incomplete_details', ('reason', 'message')),
        ):
            if key in value:
                result[key] = self.fields(value[key], fields)
        if isinstance(value.get('usage'), dict):
            usage = value['usage']
            result['usage'] = {
                key: usage[key]
                for key in ('input_tokens', 'output_tokens', 'total_tokens')
                if type(usage.get(key)) is int and usage[key] >= 0
            }
            for key, field in (
                ('input_tokens_details', 'cached_tokens'),
                ('output_tokens_details', 'reasoning_tokens'),
            ):
                detail = usage.get(key) or {}
                if isinstance(detail, dict) and type(detail.get(field)) is int:
                    result['usage'][key] = {field: detail[field]}
        return result

    def event(self, value):
        result = self.fields(
            value, ('type', 'output_index', 'content_index', 'delta', 'text', 'refusal')
        )
        for key, handler in (('response', self.response), ('item', self.item), ('part', self.part)):
            if key in value:
                result[key] = handler(value[key])
        if 'error' in value:
            result['error'] = self.fields(value['error'], ('type', 'code', 'param', 'message'))
        if value.get('type') == 'error':
            result.update(self.fields(value, ('code', 'param', 'message')))
        return result


def write_json(path, data):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


class Attempt:
    def __init__(self, directory, *, source, model=MODEL):
        request_contract = contract(model)
        self.directory = Path(directory)
        # A used directory may represent an ambiguous sent request. Never overwrite it.
        self.directory.mkdir(mode=0o700)
        self.sanitizer = Sanitizer()
        self.record = {
            'format_version': 1,
            'source': source,
            'contract': request_contract,
            'phase': 'reserved',
            'post_attempts': 0,
            'retries': 0,
            'http': {},
            'frames': [],
            'content_redacted': False,
            'stream_exhausted': False,
        }
        write_json(self.directory / 'attempted.json', {'reserved': True, 'max_post_attempts': 1})
        self.checkpoint()

    def checkpoint(self):
        self.record['content_redacted'] = self.sanitizer.redacted
        write_json(self.directory / 'record.json', self.record)


class Capture:
    def __init__(self, attempt, encoding):
        self.attempt = attempt
        self.encoding = encoding
        self.decoder = codecs.getincrementaldecoder(encoding)(errors='replace')
        self.pending, self.data_lines, self.body, self.closed = '', [], bytearray(), False
        self.redacted_delta_types = set()

    def frame(self, *, terminated):
        if not self.data_lines:
            return
        payload = '\n'.join(self.data_lines)
        self.data_lines.clear()
        frame = {'terminated': terminated}
        if payload == '[DONE]':
            frame['kind'] = 'done'
        else:
            try:
                value = json.loads(payload)
            except ValueError:
                value = None
            if isinstance(value, dict):
                frame.update(kind='event', payload=self.attempt.sanitizer.event(value))
                if value.get('type') in ('response.output_text.delta', 'response.refusal.delta'):
                    label = value['type']
                    candidates = [
                        entry['payload']
                        for entry in self.attempt.record['frames']
                        if entry.get('payload', {}).get('type') == label
                    ]
                    candidates.append(frame['payload'])
                    joined = ''.join(entry.get('delta', '') for entry in candidates)
                    if self.attempt.sanitizer.text(joined) != joined or (
                        frame['payload'].get('delta') != value.get('delta')
                    ):
                        self.redacted_delta_types.add(label)
                    if label in self.redacted_delta_types:
                        for entry in candidates:
                            entry['delta'] = '[REDACTED DELTA SEQUENCE]'
            else:
                frame.update(kind='invalid', data=self.attempt.sanitizer.text(payload))
        self.attempt.record['frames'].append(frame)
        self.attempt.record['phase'] = 'streaming'
        self.attempt.checkpoint()

    def line(self, line):
        if line.startswith('data:'):
            self.data_lines.append(line[5:].lstrip())
        elif line == '':
            self.frame(terminated=True)

    def feed(self, chunk):
        if len(self.body) + len(chunk) > LIMIT:
            self.attempt.record['capture_limit_exceeded'] = True
            self.attempt.checkpoint()
            raise RuntimeError('Synthetic probe response exceeded its recording limit.')
        self.body.extend(chunk)
        self.pending += self.decoder.decode(chunk)
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            self.line(line.removesuffix('\r'))

    def finish(self, *, exhausted):
        if self.closed:
            return
        self.closed = True
        self.pending += self.decoder.decode(b'', final=True)
        if self.pending:
            self.line(self.pending.removesuffix('\r'))
        self.frame(terminated=False)
        text = self.body.decode(self.encoding, errors='replace')
        self.attempt.record['stream_exhausted'] = exhausted
        self.attempt.record['response_bytes_observed'] = len(self.body)
        if text.lstrip().startswith(('data:', 'event:', ':')):
            self.attempt.record['body_kind'] = 'sse'
        else:
            self.attempt.record['body_kind'] = 'non_sse'
            try:
                value = json.loads(text)
            except ValueError:
                # Unknown HTML/plaintext can contain private server content. The adapter
                # only reports its non-JSON shape, so replay that format error with a stub.
                self.attempt.record['non_sse'] = {
                    'text': '[non-JSON body omitted]',
                    'original_body_omitted': True,
                }
                self.attempt.record['replay_scope'] = 'non_sse_format_error_only'
            else:
                safe = self.attempt.sanitizer.response(value)
                safe.update(self.attempt.sanitizer.fields(value, ('detail',)))
                self.attempt.record['non_sse'] = {'json': safe}
        self.body.clear()
        self.attempt.checkpoint()


class CapturedStream(httpx.AsyncByteStream):
    def __init__(self, inner, capture):
        self.inner, self.capture = inner, capture

    async def __aiter__(self):
        exhausted = False
        try:
            async for chunk in self.inner:
                self.capture.feed(chunk)
                yield chunk
            exhausted = True
        except BaseException as error:
            self.capture.attempt.record['stream_error_type'] = type(error).__name__
            self.capture.attempt.record['failure_phase'] = 'response_stream'
            raise
        finally:
            self.capture.finish(exhausted=exhausted)

    async def aclose(self):
        self.capture.finish(exhausted=False)
        await self.inner.aclose()


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner, attempt):
        self.inner, self.attempt = inner, attempt

    async def handle_async_request(self, request):
        try:
            response = await self.inner.handle_async_request(request)
        except BaseException as error:
            self.attempt.record['transport_error_type'] = type(error).__name__
            self.attempt.record['failure_phase'] = 'before_response_headers'
            self.attempt.checkpoint()
            raise
        value = response.headers.get('content-type')
        self.attempt.record['http'] = {
            'status': response.status_code,
            'content_type_present': value is not None,
            'content_type': self.attempt.sanitizer.text(value) if value is not None else None,
            'text_encoding': response.encoding,
        }
        self.attempt.record['phase'] = 'headers_received'
        self.attempt.checkpoint()
        response.stream = CapturedStream(response.stream, Capture(self.attempt, response.encoding))
        return response

    async def aclose(self):
        await self.inner.aclose()


def inputs():
    png = synthetic_png()
    files = LocalFiles()
    contents = [
        SimpleNamespace(
            role='user',
            parts=[
                SimpleNamespace(
                    text=None, inline_data=SimpleNamespace(data=png, mime_type='image/png')
                ),
                SimpleNamespace(text=PROMPT),
            ],
        )
    ]
    config = SimpleNamespace(response_schema=ImageReport, system_instruction=SYSTEM)
    return files, contents, config


class ProbeClient(httpx.AsyncClient):
    def __init__(self, transport, attempt, timeout):
        self.attempt = attempt
        super().__init__(
            transport=transport, timeout=timeout, trust_env=False, follow_redirects=False
        )

    def stream(self, method, url, **kwargs):
        if method != 'POST' or url != ENDPOINT or self.attempt.record['post_attempts']:
            raise RuntimeError('Only one fixed Responses POST is permitted per probe.')
        payload = kwargs['json']
        files, contents, config = inputs()
        model = self.attempt.record['contract']['model']
        if payload != ChatGPTModels(None, files, 60).payload(model, contents, config):
            raise RuntimeError('Only the fixed synthetic image and prompt may be recorded.')
        payload['reasoning'] = {'effort': 'low'}
        payload['input'][0]['content'][0]['detail'] = 'low'
        self.attempt.record['post_attempts'] = 1
        self.attempt.record['phase'] = 'request_started'
        self.attempt.checkpoint()
        return super().stream(method, url, **kwargs)


def replay_body(record):
    encoding = record['http'].get('text_encoding', 'utf-8')
    if record.get('body_kind') != 'sse':
        body = record.get('non_sse', {})
        return (
            json.dumps(body['json']).encode(encoding)
            if 'json' in body
            else body.get('text', '').encode(encoding, errors='replace')
        )
    result = []
    for frame in record['frames']:
        if frame['kind'] == 'event':
            payload = json.dumps(frame['payload'], ensure_ascii=True)
        elif frame['kind'] == 'done':
            payload = '[DONE]'
        else:
            payload = frame['data']
        result.append('\n'.join('data: ' + line for line in payload.split('\n')))
        result.append('\n\n' if frame['terminated'] else '')
    return ''.join(result).encode(encoding, errors='replace')


def coordinate_check(parsed):
    expected = {('red', 'square'): (64, 64), ('blue', 'circle'): (184, 128)}
    actual = {
        (shape.color, shape.shape): (shape.center_x, shape.center_y) for shape in parsed.shapes
    }
    return {
        'tolerance_pixels': 8,
        'expected': [
            {'color': key[0], 'shape': key[1], 'center': list(value)}
            for key, value in expected.items()
        ],
        'actual': parsed.model_dump(mode='json')['shapes'],
        'matched': len(parsed.shapes) == 2
        and actual.keys() == expected.keys()
        and all(
            abs(actual[key][axis] - value[axis]) <= 8
            for key, value in expected.items()
            for axis in (0, 1)
        ),
    }


def schema_errors(error, text):
    schema = ImageReport.model_json_schema()
    try:
        actual = json.loads(text)
    except ValueError:
        actual = text
    errors = []
    for issue in error.errors(include_input=False, include_context=False, include_url=False):
        rule, value = schema, actual
        for segment in issue['loc']:
            if '$ref' in rule:
                rule = schema['$defs'][rule['$ref'].rsplit('/', 1)[-1]]
            if isinstance(segment, int):
                rule = rule.get('items', {})
                value = value[segment] if isinstance(value, list) and segment < len(value) else None
            else:
                rule = rule.get('properties', {}).get(segment, {})
                value = value.get(segment, {'missing': True}) if isinstance(value, dict) else None
        if '$ref' in rule:
            rule = schema['$defs'][rule['$ref'].rsplit('/', 1)[-1]]
        expected = {
            key: rule[key]
            for key in (
                'type',
                'enum',
                'minimum',
                'exclusiveMaximum',
                'minItems',
                'maxItems',
                'required',
                'additionalProperties',
            )
            if key in rule
        }
        if issue['type'] == 'extra_forbidden':
            expected = {'allowed': False}
        errors.append({**issue, 'loc': list(issue['loc']), 'expected': expected, 'actual': value})
    return errors


async def diagnose(record):
    events = [frame['payload'] for frame in record['frames'] if frame['kind'] == 'event']
    terminal = [
        event
        for event in events
        if event.get('type')
        in ('response.completed', 'response.incomplete', 'response.failed', 'error')
    ]
    delta = ''.join(
        event.get('delta', '')
        for event in events
        if event.get('type') == 'response.output_text.delta'
    )
    diagnostic = {
        'completed_event_observed': any(
            event.get('type') == 'response.completed' for event in events
        ),
        'completed_marker': any(
            frame['terminated'] and frame.get('payload', {}).get('type') == 'response.completed'
            for frame in record['frames']
        ),
        'done_marker': any(
            frame['terminated'] and frame['kind'] == 'done' for frame in record['frames']
        ),
        'terminal_events': terminal,
        'delta_text': delta,
        'schema': {'validated': False, 'errors': []},
        'usage': [
            event['response']['usage']
            for event in terminal
            if isinstance(event.get('response'), dict) and 'usage' in event['response']
        ],
    }
    diagnostic['refusals'] = [
        part['refusal']
        for event in terminal
        for item in (event.get('response') or {}).get('output', [])
        for part in item.get('content', [])
        if part.get('type') == 'refusal' and 'refusal' in part
    ]

    async def lines():
        headers = {}
        if record['http'].get('content_type_present'):
            headers['content-type'] = record['http']['content_type']
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=headers, content=replay_body(record))
            )
        ) as client:
            async with client.stream('GET', 'https://offline.invalid/') as response:
                async for line in response.aiter_lines():
                    yield line

    try:
        text, _ = await completed_response(lines())
    except Exception as error:
        diagnostic['parser_error'] = {'type': type(error).__name__, 'message': str(error)}
        return diagnostic
    diagnostic['output_text'] = text
    try:
        parsed = ImageReport.model_validate_json(text)
    except ValidationError as error:
        diagnostic['schema']['errors'] = schema_errors(error, text)
    else:
        diagnostic['schema'] = {
            'validated': True,
            'errors': [],
            'parsed': parsed.model_dump(mode='json'),
        }
        diagnostic['coordinates'] = coordinate_check(parsed)
    try:
        diagnostic['output_json'] = json.loads(text)
    except ValueError:
        diagnostic['output_json'] = None
    diagnostic['delta_equals_completed'] = delta == text if delta else None
    final_output = next(
        (
            event['response'].get('output')
            for event in reversed(terminal)
            if event.get('type') == 'response.completed'
        ),
        None,
    )
    diagnostic['answer_source'] = (
        'completed.response.output; deltas are not concatenated'
        if final_output
        else 'matching item/part/text DONE events after response.completed; no delta fallback'
    )
    return diagnostic


async def execute(attempt, session, transport, *, timeout=60):
    if attempt.record['phase'] != 'reserved':
        raise RuntimeError('This attempt was already started; never resend it.')
    model = attempt.record['contract']['model']
    if attempt.record['contract'] != contract(model):
        raise RuntimeError('Synthetic request contract changed; stop before any session access.')
    started = time.monotonic()
    deadline = asyncio.get_running_loop().time() + timeout
    files, contents, config = inputs()
    attempt.record['total_timeout_seconds'] = timeout
    attempt.record['phase'] = 'adapter_started'
    attempt.checkpoint()
    models = ChatGPTModels(
        session,
        files,
        timeout,
        client_factory=lambda: ProbeClient(
            RecordingTransport(transport, attempt), attempt, timeout
        ),
    )
    try:
        # Include the opaque existing-session operation, not only HTTP inference,
        # in the one-attempt deadline. Cancellation never starts another POST.
        async with asyncio.timeout_at(deadline):
            response = await models.generate_content(model, contents, config=config)
    except TimeoutError:
        attempt.record['total_deadline_exceeded'] = True
        attempt.record['adapter'] = {
            'outcome': 'failed',
            'error_type': 'RuntimeError',
            'error': 'ChatGPT transport timed out or failed; request stopped.',
        }
    except Exception as error:
        attempt.record['adapter'] = {
            'outcome': 'failed',
            'error_type': type(error).__name__,
            'error': safe_error(error),
        }
    else:
        attempt.record['adapter'] = {
            'outcome': 'succeeded',
            'text': response.text,
            'parsed': response.parsed.model_dump(mode='json'),
        }
    attempt.record['elapsed_seconds'] = round(time.monotonic() - started, 4)
    # Joined deltas/errors can introduce sensitive strings across chunk boundaries.
    attempt.record['diagnostics'] = attempt.sanitizer.tree(await diagnose(attempt.record))
    attempt.record['adapter'] = attempt.sanitizer.tree(attempt.record['adapter'])
    attempt.record['phase'] = 'finished'
    attempt.record['replay_complete'] = (
        (attempt.record['stream_exhausted'] or attempt.record['diagnostics']['done_marker'])
        and not attempt.sanitizer.redacted
        and not attempt.record.get('capture_limit_exceeded', False)
        and not attempt.record.get('non_sse', {}).get('original_body_omitted', False)
    )
    if attempt.sanitizer.redacted:
        observed = attempt.record['adapter']['outcome']
        attempt.record['adapter'].update(
            outcome='unverifiable_after_redaction', observed_outcome=observed
        )
    attempt.checkpoint()
    return attempt.record


class OfflineSession:
    def access_token(self):
        return 'offline-placeholder'


def safe_error(error):
    # Request IDs are transport metadata, unnecessary for this replay and never persisted.
    return re.sub(r'request_id=[A-Za-z0-9_-]{0,120}', 'request_id=', str(error))


def forbid_network():
    def blocked(*args, **kwargs):
        raise RuntimeError('Real network is forbidden during synthetic-probe replay.')

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked
    socket.getaddrinfo = blocked


class ReplayStream(httpx.AsyncByteStream):
    def __init__(self, record):
        self.record = record

    async def __aiter__(self):
        yield replay_body(self.record)
        if self.record.get('stream_error_type'):
            raise httpx.ReadTimeout('Offline replay of interrupted transport.')


async def replay(record):
    forbid_network()
    model = record.get('contract', {}).get('model')
    if record.get('format_version') != 1 or record.get('contract') != contract(model):
        raise ValueError('Unsupported or changed synthetic probe contract.')
    if record.get('content_redacted') or record.get('capture_limit_exceeded'):
        raise ValueError('Redacted or truncated content cannot prove original parser behavior.')
    sanitizer = Sanitizer()
    for frame in record['frames']:
        if frame['kind'] == 'event':
            if sanitizer.event(frame['payload']) != frame['payload']:
                raise ValueError('Replay payload is outside the safe recording field allowlist.')
        elif frame['kind'] == 'invalid':
            sanitizer.text(frame['data'])
    sanitizer.text(record['http'].get('content_type') or '')
    if record.get('non_sse'):
        sanitizer.tree(record['non_sse'])
    if sanitizer.redacted:
        raise ValueError('Replay content requires redaction; original behavior is unverifiable.')
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls != 1 or request.method != 'POST' or str(request.url) != ENDPOINT:
            raise AssertionError('Replay attempted an unexpected request.')
        if json.loads(request.content)['model'] != model:
            raise AssertionError('Replay changed the explicitly requested model.')
        metadata = record['http']
        if 'status' not in metadata:
            raise httpx.ConnectTimeout('Offline replay: no headers received.')
        headers = {}
        if metadata['content_type_present']:
            headers['content-type'] = metadata['content_type']
        return httpx.Response(metadata['status'], headers=headers, stream=ReplayStream(record))

    files, contents, config = inputs()
    models = ChatGPTModels(
        OfflineSession(),
        files,
        1,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
        ),
    )
    try:
        response = await models.generate_content(model, contents, config=config)
    except Exception as error:
        result = {
            'outcome': 'failed',
            'error_type': type(error).__name__,
            'error': safe_error(error),
        }
    else:
        result = {
            'outcome': 'succeeded',
            'text': response.text,
            'parsed': response.parsed.model_dump(mode='json'),
        }
    diagnostics = await diagnose(record)
    result = sanitizer.tree(result)
    diagnostics = sanitizer.tree(diagnostics)
    if sanitizer.redacted:
        raise ValueError('Combined replay output requires redaction; do not save private content.')
    return {
        'adapter': result,
        'diagnostics': diagnostics,
        'mock_post_attempts': calls,
        'real_network_requests': 0,
        'oauth_calls': 0,
        'credential_reads': 0,
        'retries': 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('replay', help='Offline; mock session and transport only.')
    command.add_argument('record', type=Path)
    command.add_argument('--output', type=Path, required=True)
    command = commands.add_parser('live-once', help='Requires NEW single-request approval.')
    command.add_argument('--model', choices=PROBE_MODELS, default=MODEL)
    command.add_argument('--attempt-directory', type=Path, required=True)
    command.add_argument('--approve-single-synthetic-request', action='store_true', required=True)
    args = parser.parse_args()
    if args.command == 'replay':
        forbid_network()
        result = asyncio.run(replay(json.loads(args.record.read_text())))
        if args.output.exists():
            parser.error('Replay output exists; choose a new output file.')
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    'replay_saved': str(args.output),
                    'outcome': result['adapter']['outcome'],
                    'real_network_requests': 0,
                    'oauth_calls': 0,
                }
            )
        )
    else:
        # The only live entry point; never reached by replay/offline checks.
        from extensions.chatgpt_oauth import OAuthSession, require_local_runtime

        require_local_runtime()
        attempt = Attempt(args.attempt_directory, source='live_synthetic', model=args.model)
        result = asyncio.run(
            execute(attempt, OAuthSession('personal'), httpx.AsyncHTTPTransport(retries=0))
        )
        print(
            json.dumps(
                {
                    'record': str(attempt.directory / 'record.json'),
                    'outcome': result['adapter']['outcome'],
                    'post_attempts': result['post_attempts'],
                    'retries': 0,
                }
            )
        )


if __name__ == '__main__':
    main()
