#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import math
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
UTC = timezone.utc


def _request_json(url: str, token: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github.star+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "epic-freebies-helper-star-history",
            "X-GitHub-Api-Version": API_VERSION,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API returned HTTP {error.code}: {message}") from error
    except URLError as error:
        raise RuntimeError(f"GitHub API request failed: {error.reason}") from error


def _repository_path(repository: str) -> str:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must use the OWNER/REPO format")
    return "/".join(quote(part, safe="") for part in parts)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _fetch_star_history(repository: str, token: str) -> tuple[date, list[datetime]]:
    repository_path = _repository_path(repository)
    metadata = _request_json(f"{API_ROOT}/repos/{repository_path}", token)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("created_at"), str):
        raise RuntimeError("GitHub repository metadata did not contain created_at")

    stars: list[datetime] = []
    page = 1
    while True:
        url = f"{API_ROOT}/repos/{repository_path}/stargazers" f"?per_page={PER_PAGE}&page={page}"
        payload = _request_json(url, token)
        if not isinstance(payload, list):
            raise RuntimeError("GitHub stargazers response was not a list")

        for item in payload:
            timestamp = item.get("starred_at") if isinstance(item, dict) else None
            if not isinstance(timestamp, str):
                raise RuntimeError("GitHub stargazers response omitted starred_at")
            stars.append(_parse_timestamp(timestamp))

        if len(payload) < PER_PAGE:
            break
        page += 1

    stars.sort()
    return _parse_timestamp(metadata["created_at"]).date(), stars


def _nice_maximum(value: int) -> int:
    value = max(value, 1)
    magnitude = 10 ** math.floor(math.log10(value))
    for factor in (1, 2, 5, 10):
        candidate = factor * magnitude
        if candidate >= value:
            return candidate
    return 10 * magnitude


def _daily_points(
    created_at: date, stars: list[datetime]
) -> tuple[date, date, list[tuple[date, int]]]:
    start = min(created_at, stars[0].date()) if stars else created_at
    end = stars[-1].date() if stars else datetime.now(UTC).date()
    if end <= start:
        end = start + timedelta(days=1)

    daily_counts = Counter(starred_at.date() for starred_at in stars)
    points = [(start, 0)]
    cumulative = 0
    for day in sorted(daily_counts):
        if day > start:
            points.append((day, cumulative))
        cumulative += daily_counts[day]
        points.append((day, cumulative))
    if points[-1][0] < end:
        points.append((end, cumulative))
    return start, end, points


def _render_svg(repository: str, created_at: date, stars: list[datetime], theme: str) -> str:
    palettes = {
        "light": {
            "background": "#ffffff",
            "border": "#e2e8f0",
            "grid": "#e2e8f0",
            "text": "#0f172a",
            "muted": "#64748b",
            "line": "#d97706",
            "area": "#fef3c7",
            "badge": "#fff7ed",
        },
        "dark": {
            "background": "#0b1220",
            "border": "#263449",
            "grid": "#263449",
            "text": "#f8fafc",
            "muted": "#94a3b8",
            "line": "#fbbf24",
            "area": "#78350f",
            "badge": "#1e293b",
        },
    }
    palette = palettes[theme]

    width = 960
    height = 520
    left = 82
    right = 42
    top = 108
    bottom = 72
    plot_width = width - left - right
    plot_height = height - top - bottom

    start, end, points = _daily_points(created_at, stars)
    day_span = max((end - start).days, 1)
    y_max = _nice_maximum(len(stars))

    def x_position(day: date) -> float:
        return left + ((day - start).days / day_span) * plot_width

    def y_position(value: int) -> float:
        return top + plot_height - (value / y_max) * plot_height

    path_commands = [
        f"{'M' if index == 0 else 'L'} {x_position(day):.1f} {y_position(value):.1f}"
        for index, (day, value) in enumerate(points)
    ]
    line_path = " ".join(path_commands)
    area_path = (
        f"{line_path} L {x_position(points[-1][0]):.1f} {top + plot_height:.1f} "
        f"L {x_position(points[0][0]):.1f} {top + plot_height:.1f} Z"
    )

    y_grid: list[str] = []
    for index in range(6):
        value = round((y_max * index) / 5)
        y = y_position(value)
        y_grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" '
            f'stroke="{palette["grid"]}" stroke-width="1" />'
        )
        y_grid.append(
            f'<text x="{left - 14}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="{palette["muted"]}" font-size="12">{value}</text>'
        )

    x_grid: list[str] = []
    seen_days: set[date] = set()
    for index in range(6):
        day = start + timedelta(days=round(day_span * index / 5))
        if day in seen_days:
            continue
        seen_days.add(day)
        x = x_position(day)
        x_grid.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" '
            f'stroke="{palette["grid"]}" stroke-width="1" />'
        )
        x_grid.append(
            f'<text x="{x:.1f}" y="{top + plot_height + 28}" text-anchor="middle" '
            f'fill="{palette["muted"]}" font-size="12">{day.isoformat()}</text>'
        )

    total = len(stars)
    latest = stars[-1].date().isoformat() if stars else "no stars yet"
    escaped_repository = html.escape(repository)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">
  <title>GitHub Star History for {escaped_repository}</title>
  <desc>{total} stars through {latest}, generated from the GitHub API.</desc>
  <rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="18" fill="{palette["background"]}" stroke="{palette["border"]}" stroke-width="2" />
  <g font-family="'IBM Plex Sans', 'Segoe UI', sans-serif">
    <text x="{left}" y="45" fill="{palette["text"]}" font-size="24" font-weight="700">GitHub Star History</text>
    <text x="{left}" y="73" fill="{palette["muted"]}" font-size="14">{escaped_repository}</text>
    <rect x="{width - 174}" y="28" width="132" height="50" rx="12" fill="{palette["badge"]}" stroke="{palette["border"]}" />
    <text x="{width - 108}" y="49" text-anchor="middle" fill="{palette["muted"]}" font-size="11" letter-spacing="1">TOTAL STARS</text>
    <text x="{width - 108}" y="69" text-anchor="middle" fill="{palette["text"]}" font-size="20" font-weight="700">{total}</text>
    {"".join(y_grid)}
    {"".join(x_grid)}
    <path d="{area_path}" fill="{palette["area"]}" opacity="0.55" />
    <path d="{line_path}" fill="none" stroke="{palette["line"]}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" />
    <circle cx="{x_position(points[-1][0]):.1f}" cy="{y_position(points[-1][1]):.1f}" r="5" fill="{palette["line"]}" stroke="{palette["background"]}" stroke-width="3" />
    <text x="{left}" y="{height - 22}" fill="{palette["muted"]}" font-size="12">Latest star data: {latest}</text>
    <text x="{width - right}" y="{height - 22}" text-anchor="end" fill="{palette["muted"]}" font-size="12">Source: GitHub REST API</text>
  </g>
</svg>
"""


def _write_svg(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate repository-owned star history SVGs")
    parser.add_argument(
        "--repository", required=True, help="GitHub repository in OWNER/REPO format"
    )
    parser.add_argument("--output-light", type=Path, required=True)
    parser.add_argument("--output-dark", type=Path, required=True)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")

    created_at, stars = _fetch_star_history(args.repository, token)
    _write_svg(args.output_light, _render_svg(args.repository, created_at, stars, theme="light"))
    _write_svg(args.output_dark, _render_svg(args.repository, created_at, stars, theme="dark"))
    print(f"Generated star history with {len(stars)} stars.")


if __name__ == "__main__":
    main()
