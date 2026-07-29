import asyncio
from types import SimpleNamespace

import cv2
import numpy as np
from hcaptcha_challenger.models import PointCoordinate, SpatialPath

import extensions.hcaptcha_adapter as hcaptcha_adapter
from extensions.numbered_line_solver import NumberedDragSolution
from extensions.hcaptcha_adapter import (
    _correct_drag_source_points,
    _decode_entity_contour,
    _detect_task_canvas_origin,
    _is_line_completion_question,
    _match_outline_contours,
    _queue_empty_checkcaptcha_response,
    _select_line_gap_markers,
)


def _write_challenge_screenshot(path, *, canvas_y: int, canvas_height: int):
    image = np.full((470, 500, 3), 245, dtype=np.uint8)
    image[:108] = (143, 131, 0)
    image[canvas_y : canvas_y + canvas_height, 10:490] = (80, 120, 160)
    assert cv2.imwrite(str(path), image)


def test_drag_canvas_origin_supports_multi_shape_layout(tmp_path):
    screenshot = tmp_path / "challenge.png"
    _write_challenge_screenshot(screenshot, canvas_y=130, canvas_height=330)

    assert _detect_task_canvas_origin(screenshot) == (10, 130)


def test_payload_entity_centers_replace_invalid_model_sources(tmp_path):
    screenshot = tmp_path / "challenge.png"
    _write_challenge_screenshot(screenshot, canvas_y=130, canvas_height=330)
    payload = SimpleNamespace(
        tasklist=[
            SimpleNamespace(
                entities=[SimpleNamespace(coords=[416, 55]), SimpleNamespace(coords=[406, 219])]
            )
        ]
    )
    paths = [
        SpatialPath(
            start_point=PointCoordinate(x=819, y=323), end_point=PointCoordinate(x=533, y=323)
        ),
        SpatialPath(
            start_point=PointCoordinate(x=819, y=623), end_point=PointCoordinate(x=461, y=422)
        ),
    ]

    corrected = _correct_drag_source_points(
        paths,
        captcha_payload=payload,
        crumb_id=0,
        challenge_screenshot=screenshot,
        challenge_bbox={"x": 390, "y": 100, "width": 500, "height": 470},
    )

    assert [(path.start_point.x, path.start_point.y) for path in corrected] == [
        (816, 285),
        (806, 449),
    ]
    assert [(path.end_point.x, path.end_point.y) for path in corrected] == [(533, 323), (461, 422)]


def test_source_correction_requires_one_entity_per_model_path(tmp_path):
    screenshot = tmp_path / "challenge.png"
    _write_challenge_screenshot(screenshot, canvas_y=135, canvas_height=320)
    payload = SimpleNamespace(
        tasklist=[SimpleNamespace(entities=[SimpleNamespace(coords=[414, 60])])]
    )
    paths = [
        SpatialPath(
            start_point=PointCoordinate(x=800, y=300), end_point=PointCoordinate(x=500, y=300)
        ),
        SpatialPath(
            start_point=PointCoordinate(x=800, y=450), end_point=PointCoordinate(x=500, y=450)
        ),
    ]

    corrected = _correct_drag_source_points(
        paths,
        captcha_payload=payload,
        crumb_id=0,
        challenge_screenshot=screenshot,
        challenge_bbox={"x": 390, "y": 100, "width": 500, "height": 470},
    )

    assert [(path.start_point.x, path.start_point.y) for path in corrected] == [
        (800, 300),
        (800, 450),
    ]


def _contour_from_mask(mask):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return max(contours, key=cv2.contourArea)


def test_outline_topology_matching_ignores_candidate_position():
    square = np.zeros((80, 80), dtype=np.uint8)
    cv2.rectangle(square, (15, 15), (55, 55), 255, -1)
    triangle = np.zeros((80, 80), dtype=np.uint8)
    cv2.fillPoly(triangle, [np.array([[40, 10], [70, 65], [10, 65]])], 255)
    source_contours = [_contour_from_mask(square), _contour_from_mask(triangle)]
    targets = [
        (_contour_from_mask(triangle), (100.0, 100.0)),
        (_contour_from_mask(square), (200.0, 200.0)),
    ]

    matched = _match_outline_contours(source_contours, targets)

    assert matched is not None
    assert matched[0] == [1, 0]


def test_entity_contour_uses_png_alpha_channel():
    image = np.zeros((40, 40, 4), dtype=np.uint8)
    cv2.circle(image, (20, 20), 10, (100, 150, 200, 255), -1)
    encoded, content = cv2.imencode(".png", image)

    contour = _decode_entity_contour(content.tobytes())

    assert encoded
    assert contour is not None
    assert cv2.contourArea(contour) > 250


def test_line_gap_markers_use_cyan_three_and_yellow_five():
    markers = [
        ((320.0, 290.0), (128.0, 104.0, 137.0)),
        ((275.0, 220.0), (117.0, 84.0, 110.0)),
        ((280.0, 325.0), (107.0, 79.0, 81.0)),
        ((158.0, 243.0), (75.0, 132.0, 149.0)),
    ]

    assert _select_line_gap_markers(markers) == ((280.0, 325.0), (158.0, 243.0))


def test_line_question_detection_tolerates_confusable_words():
    assert _is_line_completion_question("Please ԁrag the segment on the right to сomplete the line")


def test_line_path_uses_numbered_circle_target_for_source_three(monkeypatch, tmp_path):
    payload = SimpleNamespace(
        get_requester_question=lambda: "Please drag the segment on the right to complete the line"
    )
    monkeypatch.setattr(hcaptcha_adapter, "_payload_source_points", lambda **_kwargs: [(810, 310)])
    monkeypatch.setattr(
        hcaptcha_adapter,
        "solve_numbered_line_drag",
        lambda *_args: NumberedDragSolution(
            start=(830, 300), end=(520, 410), source_label=3, digit_count=6, score=0.1
        ),
    )

    paths = hcaptcha_adapter._resolve_line_path(
        captcha_payload=payload,
        crumb_id=0,
        challenge_screenshot=tmp_path / "challenge.png",
        challenge_bbox={"x": 390.0, "y": 100.0, "width": 500.0, "height": 470.0},
    )

    assert paths is not None
    assert paths[0].start_point.model_dump() == {"x": 810, "y": 310}
    assert paths[0].end_point.model_dump() == {"x": 520, "y": 410}


def test_empty_checkcaptcha_response_is_queued_after_grace_period():
    class Response:
        url = "https://api.hcaptcha.com/checkcaptcha/example"
        status = 200

        @staticmethod
        async def body():
            return b""

    async def scenario():
        agent = SimpleNamespace(_captcha_response_queue=asyncio.Queue())
        handled = await _queue_empty_checkcaptcha_response(agent, Response(), grace_seconds=0)
        queued = await asyncio.wait_for(agent._captcha_response_queue.get(), timeout=0.1)
        return handled, queued

    handled, queued = asyncio.run(scenario())

    assert handled is True
    assert queued.is_pass is False
    assert queued.error == "empty_checkcaptcha_response"


def test_nonempty_checkcaptcha_response_cancels_pending_failure():
    class EmptyResponse:
        url = "https://api.hcaptcha.com/checkcaptcha/example"
        status = 200

        @staticmethod
        async def body():
            return b""

    class ValidResponse(EmptyResponse):
        @staticmethod
        async def body():
            return b'{"pass": true}'

    async def scenario():
        agent = SimpleNamespace(_captcha_response_queue=asyncio.Queue())
        assert await _queue_empty_checkcaptcha_response(agent, EmptyResponse(), grace_seconds=0.01)
        assert not await _queue_empty_checkcaptcha_response(agent, ValidResponse())
        await asyncio.sleep(0.02)
        return agent._captcha_response_queue.empty()

    assert asyncio.run(scenario()) is True
