"""Verify only the Linux browser launcher, without loading account/model settings."""

import asyncio
import importlib.abc
import importlib.metadata
import json
import os
import re
import sys
import tempfile
import tomllib
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BrowserOnlyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        blocked = {'camoufox', 'browserforge', 'hcaptcha_challenger', 'deploy', 'jobs'}
        if fullname.split('.', 1)[0] in blocked:
            raise RuntimeError(f'Unexpected non-Playwright import: {fullname}')
        return None


async def check_browser_startup():
    if not sys.platform.startswith('linux') or not os.getenv('DISPLAY'):
        raise RuntimeError('Run this isolated check on Linux under Xvfb')
    workflow = (ROOT / '.github/workflows/epic-gamer.yml').read_text()
    backends = re.findall(r'^\s+BROWSER_BACKEND:\s*(\w+)\s*$', workflow, re.MULTILINE)
    if backends != ['playwright'] or os.getenv('BROWSER_BACKEND') != 'playwright':
        raise RuntimeError('The scheduled workflow and this check must select Playwright')
    if os.getenv('HEADLESS') != 'virtual':
        raise RuntimeError('Use the scheduled workflow HEADLESS=virtual mode')
    packages = tomllib.loads((ROOT / 'uv.lock').read_text())['package']
    locked_version = next(
        package['version'] for package in packages if package['name'] == 'playwright'
    )
    if locked_version != '1.53.0' or importlib.metadata.version('playwright') != locked_version:
        raise RuntimeError('Expected repository-locked Playwright 1.53.0')

    with tempfile.TemporaryDirectory(prefix='epic-browser-startup-') as temporary:
        temporary_path = Path(temporary)
        profile = temporary_path / 'profile'
        recordings = temporary_path / 'recordings'
        recordings.mkdir()
        isolated_settings = types.ModuleType('settings')
        isolated_settings.RECORD_DIR = recordings
        isolated_settings.settings = types.SimpleNamespace(
            BROWSER_BACKEND=backends[0], BROWSER_PROXY=None,
            user_data_dir_for=lambda backend: profile,
        )
        # The real settings module loads account/API configuration. It must not
        # be imported for this browser-only verification.
        sys.modules['settings'] = isolated_settings
        sys.meta_path.insert(0, BrowserOnlyImports())
        sys.path.insert(0, str(ROOT / 'app'))
        from services.browser_context import open_browser_context
        from playwright._impl import _transport

        closed = []
        print('Starting isolated Playwright Firefox with HEADLESS=virtual under Xvfb', flush=True)
        async with open_browser_context(headless='virtual') as context:
            context.on('close', lambda: closed.append(True))
            await context.set_offline(True)
            await context.route('**/*', lambda route: route.abort())
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('about:blank')
            await page.set_content(
                '<!doctype html><title>Browser startup check</title>'
                '<p id="ready">local page ready</p>'
            )
            if (
                await page.title() != 'Browser startup check'
                or await page.locator('#ready').inner_text() != 'local page ready'
            ):
                raise RuntimeError('Local page did not load correctly')
            if await page.evaluate('navigator.appCodeName') != 'Mozilla':
                raise RuntimeError('Unexpected navigator.appCodeName')
            if not getattr(_transport.get_driver_env, '_epic_frame_guard', False):
                raise RuntimeError('Repository frame guard was not installed')
            if not profile.is_dir():
                raise RuntimeError('Temporary persistent profile was not created')
        if closed != [True] or not page.is_closed():
            raise RuntimeError('Browser context did not close normally')
    if temporary_path.exists():
        raise RuntimeError('Temporary profile was not removed')
    print(json.dumps({
        'status': 'passed', 'playwright': locked_version,
        'launcher': 'services.browser_context.open_browser_context',
        'headless_mode': 'virtual', 'page': 'about:blank and local HTML',
        'frame_guard': 'installed', 'browser_context_closed': True,
        'temporary_profile_removed': True, 'account_settings_loaded': False,
    }), flush=True)


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(check_browser_startup(), timeout=90))
