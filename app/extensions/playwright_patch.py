# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import pathlib
from loguru import logger


def apply_playwright_driver_patch() -> bool:
    """
    Patch Playwright's Node.js driver to fix a known race-condition crash in Firefox:
    `TypeError: Cannot read properties of undefined (reading 'childFrames')` in
    `FrameManager.removeChildFramesRecursively`.

    When navigating a page whose child iframes are being detached, Firefox can emit
    frame navigation events for frames that are already unmapped in FrameManager._frames,
    causing `this.removeChildFramesRecursively(undefined)` to crash the Node process.
    """
    try:
        spec = importlib.util.find_spec("playwright")
        if not spec or not spec.origin:
            return False

        playwright_dir = pathlib.Path(spec.origin).parent
        frames_js = playwright_dir / "driver" / "package" / "lib" / "server" / "frames.js"
        if not frames_js.is_file():
            return False

        content = frames_js.read_text(encoding="utf-8")
        patched = False

        # Guard removeChildFramesRecursively against undefined frame
        if "removeChildFramesRecursively(frame) {\n    for (const child of frame.childFrames())" in content:
            content = content.replace(
                "removeChildFramesRecursively(frame) {\n    for (const child of frame.childFrames())",
                "removeChildFramesRecursively(frame) {\n    if (!frame) return;\n    for (const child of frame.childFrames())",
            )
            patched = True
        elif "for (const child of frame.childFrames())" in content and "if (!frame) return;" not in content:
            content = content.replace(
                "for (const child of frame.childFrames())",
                "if (!frame) return;\n    for (const child of frame.childFrames())",
                1,
            )
            patched = True

        # Guard frameCommittedNewDocumentNavigation against undefined frame
        if "this.removeChildFramesRecursively(frame);" in content:
            content = content.replace(
                "this.removeChildFramesRecursively(frame);",
                "if (frame) this.removeChildFramesRecursively(frame);",
            )
            patched = True

        if patched:
            frames_js.write_text(content, encoding="utf-8")
            logger.info("Successfully patched Playwright driver frames.js for Firefox frame detachment stability")
            return True

        return False
    except Exception as err:
        logger.debug(f"Playwright driver patch skipped: {err!r}")
        return False
