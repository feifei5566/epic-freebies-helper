"""Targeted offline probe verification; no test framework, OAuth or real sockets."""

import argparse
import asyncio
import copy
import hashlib
import json
import stat
import sys
from pathlib import Path

import httpx

import probe_chatgpt_vision as probe

from extensions import chatgpt_oauth as oauth


def forbidden(*args, **kwargs):
    raise AssertionError('OAuth/credential access is forbidden during this verification.')


oauth.OAuthSession.__init__ = forbidden
oauth.OAuthSession.access_token = forbidden
oauth.CredentialStore.__init__ = forbidden
probe.forbid_network()

ANSWER = {
    'shapes': [
        {'color': 'red', 'shape': 'square', 'center_x': 65, 'center_y': 63},
        {'color': 'blue', 'shape': 'circle', 'center_x': 183, 'center_y': 129},
    ]
}
TEXT = json.dumps(ANSWER, separators=(',', ':'))
USAGE = {
    'input_tokens': 377,
    'output_tokens': 212,
    'total_tokens': 589,
    'input_tokens_details': {'cached_tokens': 0},
    'output_tokens_details': {'reasoning_tokens': 164},
}


def completed(text=TEXT, *, content=None, **extra):
    return {
        'type': 'response.completed',
        'response': {
            'status': 'completed',
            'output': [
                {
                    'type': 'message',
                    'role': 'assistant',
                    'content': (
                        content if content is not None else [{'type': 'output_text', 'text': text}]
                    ),
                }
            ],
            'usage': USAGE,
            **extra,
        },
    }


def sse(*events, done=False):
    data = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events)
    return (data + ('data: [DONE]\n\n' if done else '')).encode()


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, body, *, hang=False):
        self.body, self.hang = body, hang

    async def __aiter__(self):
        # Deliberately split JSON, UTF-8 and SSE delimiters across transport chunks.
        for offset in range(0, len(self.body), 17):
            yield self.body[offset : offset + 17]
        if self.hang:
            await asyncio.Event().wait()


async def verify(directory):
    checks = []
    production = probe.ROOT / 'app/extensions/chatgpt_provider.py'
    original_hash = hashlib.sha256(production.read_bytes()).hexdigest()

    async def case(
        name,
        body,
        *,
        headers=None,
        status=200,
        hang=False,
        connect_timeout=False,
        replay_expected=True,
        model=probe.MODEL,
    ):
        attempt = probe.Attempt(directory / name, source='offline_fixture', model=model)
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            assert calls == 1 and str(request.url) == probe.ENDPOINT
            payload = json.loads(request.content)
            assert payload['model'] == model
            assert payload['reasoning'] == {'effort': 'low'}
            assert payload['input'][0]['content'][0]['detail'] == 'low'
            if connect_timeout:
                raise httpx.ConnectTimeout('Synthetic connection timeout.')
            return httpx.Response(
                status,
                headers=(
                    headers
                    if headers is not None
                    else {
                        'content-type': 'text/event-stream; charset=utf-8',
                    }
                ),
                stream=FixtureStream(body, hang=hang),
            )

        await probe.execute(
            attempt,
            probe.OfflineSession(),
            httpx.MockTransport(handler),
            # Include local input/checkpoint work before the stalled-stream deadline.
            timeout=2,
        )
        # Replay persisted evidence, not the in-process response or an invented answer.
        saved = json.loads((attempt.directory / 'record.json').read_text())
        assert calls == saved['post_attempts'] == 1
        assert saved['phase'] == 'finished' and saved['retries'] == 0
        assert stat.S_IMODE(attempt.directory.stat().st_mode) == 0o700
        for filename in ('attempted.json', 'record.json'):
            assert stat.S_IMODE((attempt.directory / filename).stat().st_mode) == 0o600
        if replay_expected:
            result = await probe.replay(saved)
            assert result['mock_post_attempts'] == 1 and result['retries'] == 0
            assert result['adapter'] == saved['adapter'], (
                name,
                result['adapter'],
                saved['adapter'],
            )
            assert result['diagnostics'] == saved['diagnostics'], name
            probe.write_json(attempt.directory / 'replay.json', result)
        else:
            try:
                await probe.replay(saved)
            except ValueError:
                pass
            else:
                raise AssertionError('Modified/truncated content was accepted for replay.')
        checks.append(
            {
                'case': name,
                'passed': True,
                'adapter_outcome': saved['adapter']['outcome'],
                'saved_record': name + '/record.json',
                'mock_post_attempts': calls,
                'retries': 0,
                'replay': 'matched' if replay_expected else 'refused',
            }
        )
        return saved

    valid = await case('valid_schema', sse(completed(), done=True))
    assert valid['adapter']['parsed'] == ANSWER
    assert valid['diagnostics']['schema']['validated']
    assert valid['diagnostics']['coordinates']['matched']
    assert valid['diagnostics']['coordinates']['actual'][0]['center_x'] == 65
    assert valid['diagnostics']['usage'][0] == USAGE
    assert valid['diagnostics']['done_marker']

    record = await case(
        'gpt_6_1_exact_slug_no_fallback', sse(completed(model='gpt-6.1-sol')), model='gpt-6.1-sol'
    )
    assert record['contract']['model'] == 'gpt-6.1-sol'
    assert record['adapter']['parsed'] == ANSWER

    record = await case(
        'utf16_sse_decoder_parity',
        sse(completed()).decode().encode('utf-16-le'),
        headers={'content-type': 'text/event-stream; charset=utf-16-le'},
    )
    assert record['adapter']['parsed'] == ANSWER and record['diagnostics']['schema']['validated']

    record = await case(
        'structured_json_preserved',
        sse(
            completed(
                content=[
                    {'type': 'output_json', 'json': ANSWER},
                ]
            )
        ),
    )
    assert record['frames'][0]['payload']['response']['output'][0]['content'][0]['json'] == ANSWER
    assert record['adapter']['outcome'] == 'failed'  # Adapter requires output_text.

    tampered = copy.deepcopy(valid)
    tampered['frames'][0]['payload']['response']['output'][0]['content'][0][
        'text'
    ] = 'Bearer fake-tampered-output'
    try:
        await probe.replay(tampered)
    except ValueError:
        pass
    else:
        raise AssertionError('Tampered unsanitized output was accepted for replay.')
    checks.append({'case': 'unsanitized_replay_input_rejected', 'passed': True})

    invalid = copy.deepcopy(ANSWER)
    invalid['shapes'][0]['center_x'] = '64'
    invalid_text = json.dumps(invalid)
    record = await case('invalid_integer_string', sse(completed(invalid_text)))
    assert record['adapter']['outcome'] == 'failed'
    assert record['diagnostics']['output_text'] == invalid_text
    assert record['diagnostics']['output_json'] == invalid
    error = record['diagnostics']['schema']['errors'][0]
    assert error['loc'] == ['shapes', 0, 'center_x']
    assert error['expected'] == {'type': 'integer', 'minimum': 0, 'exclusiveMaximum': 256}
    assert error['actual'] == '64' and error['type'] == 'int_type'

    for name, value, kind in (
        ('coordinate_out_of_bounds', 256, 'less_than'),
        ('fractional_coordinate', 64.5, 'int_type'),
        ('boolean_coordinate', True, 'int_type'),
    ):
        data = copy.deepcopy(ANSWER)
        data['shapes'][0]['center_x'] = value
        record = await case(name, sse(completed(json.dumps(data))))
        error = record['diagnostics']['schema']['errors'][0]
        assert error['type'] == kind and error['actual'] == value

    fenced = '```json\n' + TEXT + '\n```'
    record = await case('invalid_json_fence', sse(completed(fenced)))
    assert record['diagnostics']['output_text'] == fenced
    assert record['diagnostics']['schema']['errors'][0]['type'] == 'json_invalid'

    data = copy.deepcopy(ANSWER)
    del data['shapes'][0]['center_y']
    record = await case('missing_required_field', sse(completed(json.dumps(data))))
    assert record['diagnostics']['schema']['errors'][0]['actual'] == {'missing': True}
    data = copy.deepcopy(ANSWER)
    data['shapes'][0]['explanation'] = 'extra'
    record = await case('extra_field_forbidden', sse(completed(json.dumps(data))))
    assert record['diagnostics']['schema']['errors'][0]['expected'] == {'allowed': False}

    wrong = copy.deepcopy(ANSWER)
    wrong['shapes'][0]['center_x'] = 150
    record = await case('valid_schema_wrong_coordinates', sse(completed(json.dumps(wrong))))
    assert record['diagnostics']['schema']['validated']
    assert not record['diagnostics']['coordinates']['matched']
    assert record['adapter']['parsed'] == wrong  # Never replace it with groundtruth.

    delta = {
        'type': 'response.output_text.delta',
        'delta': TEXT,
        'output_index': 0,
        'content_index': 0,
    }
    record = await case('missing_completed', sse(delta))
    assert (
        not record['diagnostics']['completed_marker']
        and record['diagnostics']['delta_text'] == TEXT
    )
    assert 'interrupted' in record['adapter']['error']
    record = await case('done_without_completed', sse(delta, done=True))
    assert 'without response.completed' in record['adapter']['error']
    record = await case('unterminated_completed_frame', sse(completed()).rstrip(b'\n'))
    assert record['adapter']['outcome'] == 'failed' and not record['frames'][0]['terminated']

    incomplete = {
        'type': 'response.incomplete',
        'response': {
            'status': 'incomplete',
            'incomplete_details': {'reason': 'max_output_tokens'},
            'usage': USAGE,
        },
    }
    record = await case('incomplete_details', sse(delta, incomplete))
    assert record['diagnostics']['terminal_events'][0]['response']['incomplete_details'] == {
        'reason': 'max_output_tokens'
    }
    assert 'incomplete' in record['adapter']['error']
    refused = '拒絕提供這張合成圖的輸出。'
    record = await case(
        'refusal', sse(completed(content=[{'type': 'refusal', 'refusal': refused}]))
    )
    assert record['diagnostics']['refusals'] == [refused]
    assert record['adapter']['error_type'] == 'EpicLlmConfigurationError'
    record = await case(
        'unicode_charset_replay_parity',
        sse(
            completed(
                content=[
                    {'type': 'refusal', 'refusal': refused},
                ]
            )
        ),
        headers={'content-type': 'text/event-stream; charset=iso-8859-1'},
    )
    assert record['http']['text_encoding'] == 'iso-8859-1'
    assert record['diagnostics']['refusals'][0] != refused  # Preserve actual decoder result.
    failure = {
        'type': 'response.failed',
        'response': {
            'status': 'failed',
            'error': {
                'code': 'model_not_found',
                'param': 'model',
                'message': 'Requested model unavailable.',
            },
        },
    }
    record = await case('failed_details', sse(failure))
    assert (
        record['diagnostics']['terminal_events'][0]['response']['error']['code']
        == 'model_not_found'
    )

    record = await case(
        'delta_completed_overlap',
        sse(
            {**delta, 'delta': TEXT[:50]},
            {**delta, 'delta': TEXT[50:]},
            {'type': 'response.output_text.done', 'text': TEXT},
            {'type': 'response.content_part.done', 'part': {'type': 'output_text', 'text': TEXT}},
            completed(),
            done=True,
        ),
    )
    assert record['diagnostics']['delta_equals_completed'] is True
    assert record['adapter']['text'] == TEXT  # Not TEXT+TEXT or done+completed.

    final_empty = completed()
    final_empty['response']['output'] = []
    done_item = {
        'type': 'response.output_item.done',
        'output_index': 0,
        'item': {
            'type': 'message',
            'role': 'assistant',
            'status': 'completed',
            'content': [{'type': 'output_text', 'text': TEXT}],
        },
    }
    done_part = {
        'type': 'response.content_part.done',
        'output_index': 0,
        'content_index': 0,
        'part': {'type': 'output_text', 'text': TEXT},
    }
    done_text = {
        'type': 'response.output_text.done',
        'output_index': 0,
        'content_index': 0,
        'text': TEXT,
    }
    record = await case(
        'empty_aggregate_verified_done_items',
        sse(delta, done_text, done_part, done_item, final_empty),
    )
    assert record['adapter']['parsed'] == ANSWER
    assert record['diagnostics']['delta_equals_completed']
    assert 'DONE events' in record['diagnostics']['answer_source']
    for name, events in (
        ('empty_aggregate_missing_item_done', [done_text, done_part, final_empty]),
        ('empty_aggregate_missing_part_done', [done_text, done_item, final_empty]),
        ('empty_aggregate_missing_text_done', [done_part, done_item, final_empty]),
        ('done_items_without_completed', [done_text, done_part, done_item]),
        ('done_items_incomplete_terminal', [done_text, done_part, done_item, incomplete]),
    ):
        record = await case(name, sse(*events))
        assert record['adapter']['outcome'] == 'failed'
    changed = copy.deepcopy(done_part)
    changed['part']['text'] = '{}'
    record = await case(
        'empty_aggregate_mismatched_markers', sse(done_text, changed, done_item, final_empty)
    )
    assert record['adapter']['outcome'] == 'failed'
    changed = copy.deepcopy(done_item)
    changed['item']['status'] = 'in_progress'
    record = await case(
        'empty_aggregate_unfinished_item', sse(done_text, done_part, changed, final_empty)
    )
    assert record['adapter']['outcome'] == 'failed'
    changed = copy.deepcopy(done_item)
    changed['item']['content'][0]['text'] = '{}'
    record = await case(
        'empty_aggregate_conflicting_duplicate_item',
        sse(done_text, done_part, done_item, changed, final_empty),
    )
    assert record['adapter']['outcome'] == 'failed'
    refused_item = copy.deepcopy(done_item)
    refused_item['item']['content'] = [{'type': 'refusal', 'refusal': refused}]
    refused_part = {**done_part, 'part': {'type': 'refusal', 'refusal': refused}}
    record = await case('empty_aggregate_refusal', sse(refused_part, refused_item, final_empty))
    assert record['adapter']['error_type'] == 'EpicLlmConfigurationError'
    added = {
        'type': 'response.output_item.added',
        'output_index': 1,
        'item': {'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []},
    }
    record = await case(
        'empty_aggregate_other_unfinished_item',
        sse(done_text, done_part, done_item, added, final_empty),
    )
    assert record['adapter']['outcome'] == 'failed'

    record = await case('missing_content_type_sse', sse(completed()), headers={})
    assert record['adapter']['outcome'] == 'succeeded'
    assert record['http']['content_type'] is None and not record['http']['content_type_present']
    record = await case(
        'non_sse_json',
        json.dumps(
            {
                'error': {
                    'code': 'unsupported_model',
                    'param': 'model',
                    'message': 'Unsupported route.',
                }
            }
        ).encode(),
        headers={'content-type': 'application/json'},
    )
    assert record['body_kind'] == 'non_sse' and record['adapter']['outcome'] == 'failed'
    assert record['non_sse']['json']['error']['code'] == 'unsupported_model'

    record = await case(
        'non_sse_private_html_omitted',
        b'<html>private-server-name</html>',
        headers={'content-type': 'text/html', 'x-request-id': 'private-trace-id'},
    )
    assert 'private-server-name' not in json.dumps(record)
    assert 'private-trace-id' not in json.dumps(record)
    assert record['replay_scope'] == 'non_sse_format_error_only'
    assert record['adapter']['outcome'] == 'failed'

    record = await case(
        'http_429_quota',
        json.dumps(
            {
                'error': {
                    'code': 'subscription_sharing_usage_limit_exceeded',
                    'message': 'Plan limit reached.',
                }
            }
        ).encode(),
        status=429,
        headers={'content-type': 'application/json'},
    )
    assert record['http']['status'] == 429
    assert record['adapter']['error_type'] == 'EpicLlmQuotaExhaustedError'
    record = await case('stream_timeout_no_retry', sse(delta), hang=True)
    assert record['stream_error_type'] == 'CancelledError'
    assert record['failure_phase'] == 'response_stream'
    assert not record['stream_exhausted'] and record['elapsed_seconds'] < 3
    record = await case('connect_timeout_no_retry', b'', connect_timeout=True)
    assert record['http'] == {} and record['post_attempts'] == 1
    assert record['transport_error_type'] == 'ConnectTimeout'
    assert record['failure_phase'] == 'before_response_headers'

    private = ['private-metadata-value', 'private-header-value', 'private-cookie-value']
    record = await case(
        'metadata_and_headers_omitted',
        sse(completed(id=private[0], metadata={'access_token': private[0]}, account_id=private[0])),
        headers={
            'content-type': 'Text/Event-Stream; Charset=UTF-8',
            'authorization': private[1],
            'set-cookie': private[2],
            'x-account-id': private[1],
        },
    )
    assert record['adapter']['outcome'] == 'succeeded'
    assert not any(value in json.dumps(record) for value in private)

    secret = 'fake-sensitive-output'
    record = await case(
        'output_redaction_refuses_replay',
        sse(
            completed(
                content=[
                    {'type': 'refusal', 'refusal': 'Bearer ' + secret + ' person@example.invalid'}
                ]
            )
        ),
        replay_expected=False,
    )
    assert record['content_redacted'] and secret not in json.dumps(record)
    assert 'person@example.invalid' not in json.dumps(record)
    record = await case(
        'split_delta_redaction',
        sse({**delta, 'delta': 'Bearer '}, {**delta, 'delta': secret}),
        replay_expected=False,
    )
    assert secret not in json.dumps(record)

    record = await case('recording_size_limit', b'x' * (probe.LIMIT + 1), replay_expected=False)
    assert record['capture_limit_exceeded'] and not record['replay_complete']

    used = directory / 'valid_schema'
    previous = (used / 'record.json').read_bytes()
    try:
        probe.Attempt(used, source='offline_fixture')
    except FileExistsError:
        pass
    else:
        raise AssertionError('A used attempt was overwritten.')
    assert (used / 'record.json').read_bytes() == previous
    checks.append({'case': 'exclusive_attempt_marker_and_permissions', 'passed': True})

    used_attempt = probe.Attempt(directory / 'same_object_no_resend', source='offline_fixture')
    used_attempt.record['phase'] = 'request_started'

    class ForbiddenSession:
        def access_token(self):
            raise AssertionError('Reuse touched a session before stopping.')

    try:
        await probe.execute(used_attempt, ForbiddenSession(), httpx.MockTransport(forbidden))
    except RuntimeError as error:
        assert 'already started' in str(error)
    else:
        raise AssertionError('The same attempt object was reused.')
    checks.append({'case': 'same_attempt_object_stops_before_session', 'passed': True})

    assert hashlib.sha256(production.read_bytes()).hexdigest() == original_hash
    assert not any(
        name in sys.modules for name in ('settings', 'deploy', 'services.epic_games_service')
    )
    result = {
        'passed': True,
        'case_count': len(checks),
        'checks': checks,
        'real_network_requests': 0,
        'oauth_calls': 0,
        'credential_reads': 0,
        'live_model_requests': 0,
        'test_suite_executed': False,
        'production_provider_unchanged_during_verification': True,
        'image_sha256': probe.IMAGE_HASH,
        'adapter_parser': 'ChatGPTModels.generate_content -> completed_response -> ImageReport.model_validate_json',
        'scope': 'Synthetic fixtures only; no inference accuracy or live-adapter claim.',
    }
    probe.write_json(directory / 'offline-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    args.output_directory.mkdir(mode=0o700)
    result = asyncio.run(verify(args.output_directory))
    print(
        json.dumps(
            {
                'passed': result['passed'],
                'case_count': result['case_count'],
                'real_network_requests': 0,
                'oauth_calls': 0,
                'credential_reads': 0,
                'result': str(args.output_directory / 'offline-result.json'),
            }
        )
    )


if __name__ == '__main__':
    main()
