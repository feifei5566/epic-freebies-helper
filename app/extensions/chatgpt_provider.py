"""Google-genai interface adapter for the public OAuth Responses API."""

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import BaseModel

from extensions.chatgpt_oauth import OAuthSession, RESOURCE, error_details, response_error
from extensions.runtime_failures import EpicLlmConfigurationError, EpicLlmQuotaExhaustedError


def completed_items(events, observed):
    """Recover a terminal response's omitted aggregate from matching DONE evidence."""
    items, parts, texts = {}, {}, {}
    for event in events:
        label = event['type']
        index = event.get('output_index')
        if type(index) is not int or index < 0:
            raise RuntimeError('Invalid ChatGPT done-event output index.')
        if label == 'response.output_item.done':
            key, value, target = index, event.get('item'), items
        else:
            content_index = event.get('content_index')
            if type(content_index) is not int or content_index < 0:
                raise RuntimeError('Invalid ChatGPT done-event content index.')
            key = (index, content_index)
            value, target = (
                (event.get('part'), parts)
                if label == 'response.content_part.done'
                else (event.get('text'), texts)
            )
        if key in target and target[key] != value:
            raise RuntimeError('Conflicting ChatGPT done events; output discarded.')
        target[key] = value
    if any(index not in items for index in observed):
        raise RuntimeError('ChatGPT completed output lacked an item completion marker.')
    if any(index not in items for index, _ in set(parts) | set(texts)):
        raise RuntimeError('ChatGPT completed output contained unmatched part/text markers.')
    output = []
    for index, item in sorted(items.items()):
        if not isinstance(item, dict):
            raise RuntimeError('Invalid ChatGPT completed output item.')
        if item.get('type') != 'message' or item.get('role') != 'assistant':
            continue
        content = item.get('content')
        if item.get('status') != 'completed' or not isinstance(content, list) or not content:
            raise RuntimeError('ChatGPT output item was not completed; output discarded.')
        if {key[1] for key in parts if key[0] == index} != set(range(len(content))):
            raise RuntimeError('ChatGPT completed output lacked matching part markers.')
        expected_texts = {
            position
            for position, part in enumerate(content)
            if isinstance(part, dict) and part.get('type') == 'output_text'
        }
        if {key[1] for key in texts if key[0] == index} != expected_texts:
            raise RuntimeError('ChatGPT completed output contained unmatched text markers.')
        for content_index, part in enumerate(content):
            if not isinstance(part, dict) or part.get('type') not in ('output_text', 'refusal'):
                raise RuntimeError('Unsupported ChatGPT completed content part.')
            field = 'text' if part['type'] == 'output_text' else 'refusal'
            value = part.get(field)
            done = parts[(index, content_index)]
            if (
                not isinstance(value, str)
                or not isinstance(done, dict)
                or done.get('type') != part['type']
                or done.get(field) != value
            ):
                raise RuntimeError('ChatGPT completed item and part markers disagree.')
            if field == 'text' and texts.get((index, content_index)) != value:
                raise RuntimeError('ChatGPT completed output lacked matching text markers.')
        output.append(item)
    if not output:
        raise RuntimeError('Completed ChatGPT response contained no verified output items.')
    return output


async def completed_response(lines, *, request_id=''):
    """Discard partial text unless a valid completed terminal event arrives."""
    data_lines = []
    response = None
    terminal = False
    done_events = []
    observed_items = set()
    async for line in lines:
        if line.startswith('data:'):
            data_lines.append(line[5:].lstrip())
        elif line == '' and data_lines:
            payload = '\n'.join(data_lines)
            data_lines.clear()
            if payload == '[DONE]':
                if not terminal:
                    raise RuntimeError('ChatGPT stream ended without response.completed.')
                break
            try:
                event = json.loads(payload)
                event_type = event['type']
            except (ValueError, KeyError, TypeError):
                raise RuntimeError('Invalid ChatGPT SSE event.') from None
            if not terminal and event_type == 'response.output_item.added':
                index = event.get('output_index')
                if type(index) is int and index >= 0:
                    observed_items.add(index)
            if not terminal and event_type in (
                'response.output_item.done',
                'response.content_part.done',
                'response.output_text.done',
            ):
                done_events.append(event)
            if event_type in ('response.failed', 'error'):
                error = (event.get('response') or {}).get('error') or event.get('error') or event
                response_error(
                    code=error.get('code', ''),
                    param=error.get('param') or '',
                    request_id=request_id,
                )
            if event_type == 'response.incomplete':
                raise RuntimeError('ChatGPT response was incomplete; partial output discarded.')
            if event_type == 'response.completed':
                response = event.get('response')
                if not isinstance(response, dict) or response.get('status') != 'completed':
                    raise RuntimeError('Invalid ChatGPT completion event.')
                terminal = True
    if not terminal:
        raise RuntimeError('ChatGPT stream interrupted; partial output discarded.')
    if response.get('output') == []:
        # The direct route can omit the final aggregate while emitting full item,
        # part and text DONE events. Delta text alone is never used as an answer.
        response = {**response, 'output': completed_items(done_events, observed_items)}
    texts = []
    for item in response.get('output', []):
        if item.get('type') == 'message' and item.get('role') == 'assistant':
            for part in item.get('content', []):
                if part.get('type') == 'output_text':
                    texts.append(part.get('text', ''))
                elif part.get('type') == 'refusal':
                    raise EpicLlmConfigurationError('ChatGPT declined this request.')
    text = ''.join(texts).strip()
    if not text:
        raise RuntimeError('Completed ChatGPT response contained no output text.')
    return text, response


class LocalFiles:
    """The SIWC flow does not support the Files upload API; retain image bytes locally."""

    def __init__(self):
        self.images = {}

    async def upload(self, file, **kwargs):
        path = Path(file)
        mime = kwargs.get('mime_type') or mimetypes.guess_type(path.name)[0]
        if mime not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
            raise EpicLlmConfigurationError('Unsupported ChatGPT image input format.')
        payload = path.read_bytes()
        uri = 'chatgpt-local://' + str(len(self.images))
        self.images[uri] = (payload, mime)
        return SimpleNamespace(name=uri, uri=uri, mime_type=mime)

    def part(self, part):
        if getattr(part, 'text', None):
            return {'type': 'input_text', 'text': part.text}
        inline = getattr(part, 'inline_data', None)
        file_data = getattr(part, 'file_data', None)
        if inline and getattr(inline, 'data', None):
            payload, mime = inline.data, inline.mime_type
        elif file_data and file_data.file_uri in self.images:
            payload, mime = self.images[file_data.file_uri]
        else:
            raise EpicLlmConfigurationError('Unsupported ChatGPT content part; request stopped.')
        if mime not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
            raise EpicLlmConfigurationError('Unsupported ChatGPT image input format.')
        encoded = base64.b64encode(payload).decode('ascii')
        return {'type': 'input_image', 'image_url': f'data:{mime};base64,{encoded}'}


class ChatGPTResponse:
    def __init__(self, text, parsed):
        self.text, self.parsed = text, parsed

    def model_dump(self, mode='python'):
        return {'text': self.text, 'parsed': self.parsed.model_dump(mode=mode)}


class ChatGPTModels:
    def __init__(self, session, files, timeout, *, client_factory=None):
        self.session, self.files, self.timeout = session, files, timeout
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)
        )

    def payload(self, model, contents, config):
        schema = getattr(config, 'response_schema', None)
        if not model or not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise EpicLlmConfigurationError(
                'ChatGPT requires an explicit model and response schema.'
            )
        instructions = str(getattr(config, 'system_instruction', None) or '')
        instructions += '\nReturn only a JSON object matching this schema: ' + json.dumps(
            schema.model_json_schema(), ensure_ascii=False
        )
        inputs = []
        for content in contents:
            if getattr(content, 'role', 'user') != 'user':
                raise EpicLlmConfigurationError('Unsupported ChatGPT input role.')
            parts = [self.files.part(part) for part in content.parts]
            if parts:
                inputs.append({'role': 'user', 'content': parts})
        if not inputs:
            raise EpicLlmConfigurationError('ChatGPT input is empty.')
        # Explicit allowlist: Gemini thinking/temperature options are not forwarded.
        return {
            'model': model,
            'instructions': instructions,
            'input': inputs,
            'store': False,
            'stream': True,
        }

    async def generate_content(self, model, contents, *, config, **kwargs):
        if kwargs:
            raise EpicLlmConfigurationError('Unsupported ChatGPT generation options.')
        payload = self.payload(model, contents, config)
        try:
            token = await asyncio.to_thread(self.session.access_token)
            async with asyncio.timeout(self.timeout):
                async with self.client_factory() as client:
                    async with client.stream(
                        'POST',
                        RESOURCE + '/responses',
                        headers={'Authorization': 'Bearer ' + token},
                        json=payload,
                    ) as response:
                        if response.status_code != 200:
                            await response.aread()
                            response_error(
                                status=response.status_code,
                                **error_details(response),
                                request_id=response.headers.get('x-request-id', ''),
                                retry_after=response.headers.get('retry-after'),
                            )
                        media_type = response.headers.get('content-type', '').split(';', 1)[0]
                        if media_type.strip().lower() != 'text/event-stream':
                            await response.aread()
                            # Some direct-route responses omit a usable media type while
                            # carrying valid SSE frames. Accept only that framing; the
                            # parser still requires response.completed and valid output.
                            if not response.text.lstrip().startswith(('event:', 'data:', ':')):
                                try:
                                    response_error(
                                        status=response.status_code,
                                        **error_details(response),
                                        request_id=response.headers.get('x-request-id', ''),
                                    )
                                except EpicLlmQuotaExhaustedError:
                                    raise
                                except RuntimeError as error:
                                    raise EpicLlmConfigurationError(
                                        'ChatGPT expected an SSE stream; unexpected response format. '
                                        + str(error)
                                    ) from None
                        text, _ = await completed_response(
                            response.aiter_lines(),
                            request_id=response.headers.get('x-request-id', ''),
                        )
        except (httpx.HTTPError, TimeoutError):
            raise RuntimeError('ChatGPT transport timed out or failed; request stopped.') from None
        try:
            parsed = config.response_schema.model_validate_json(text)
        except ValueError:
            raise RuntimeError('ChatGPT output did not match the required JSON schema.') from None
        return ChatGPTResponse(text, parsed)


def apply_chatgpt_patch(settings):
    from google import genai

    class ChatGPTClient:
        def __init__(self, *args, **kwargs):
            files = LocalFiles()
            session = OAuthSession(settings.CHATGPT_PROFILE)
            self.aio = SimpleNamespace(
                files=files,
                models=ChatGPTModels(session, files, settings.CHATGPT_REQUEST_TIMEOUT_SECONDS),
            )

    genai.Client = ChatGPTClient
