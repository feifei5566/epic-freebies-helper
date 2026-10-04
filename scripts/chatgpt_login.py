#!/usr/bin/env python3
"""Explicit user-operated SIWC login; does not load Epic settings or start the app."""

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

from extensions.chatgpt_oauth import OAuthSession, RESOURCE, error_details, response_error
from extensions.runtime_failures import EpicNonRetryableError


def main():
    parser = argparse.ArgumentParser(description='Sign in with ChatGPT for local plan usage.')
    parser.add_argument('command', choices=('login', 'status', 'models', 'logout'))
    parser.add_argument('--profile', default='default', help='Separate account registration label.')
    parser.add_argument(
        '--enable-plan-usage',
        action='store_true',
        help='Explicitly request plan consent again during login.',
    )
    args = parser.parse_args()
    if args.enable_plan_usage and args.command != 'login':
        parser.error('--enable-plan-usage is only valid with login')
    try:
        session = OAuthSession(args.profile)
        if args.command == 'login':
            print(
                'Continue with ChatGPT: approve permissions in the official browser page. '
                'No Epic login or inference will run.'
            )
            result = session.sign_in(request_plan_consent=args.enable_plan_usage)
        elif args.command == 'logout':
            result = session.sign_out()
        elif args.command == 'models':
            with session.client_factory() as client:
                response = client.get(
                    RESOURCE + '/models',
                    headers={
                        'Authorization': 'Bearer ' + session.access_token(),
                    },
                )
                if response.status_code != 200:
                    response_error(
                        status=response.status_code,
                        **error_details(response),
                        request_id=response.headers.get('x-request-id', ''),
                    )
                result = {
                    'profile': args.profile,
                    'models': [
                        {'slug': model['slug'], 'display_name': model.get('display_name', '')}
                        for model in response.json()['models']
                        if model.get('visibility') == 'list'
                    ],
                    'note': 'Model discovery does not prove inference or image support.',
                }
        else:
            result = session.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (EpicNonRetryableError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        print('ChatGPT connection or local storage failed; request stopped.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
