"""Direct YouTube transcript extraction with an optional API fallback."""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

import requests


logger = logging.getLogger("YouTubeEnhance")
RAPIDAPI_HOST = "youtube-transcript3.p.rapidapi.com"
RAPIDAPI_URL = f"https://{RAPIDAPI_HOST}/api/transcript"
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(value: str) -> str | None:
    value = (value or "").strip()
    if VIDEO_ID_PATTERN.fullmatch(value):
        return value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    path_parts = [part for part in parsed.path.split("/") if part]

    candidate = ""
    if host == "youtu.be" and path_parts:
        candidate = path_parts[0]
    elif host.endswith("youtube.com"):
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif path_parts and path_parts[0] in {"embed", "shorts", "live", "v"} and len(path_parts) > 1:
            candidate = path_parts[1]

    return candidate if VIDEO_ID_PATTERN.fullmatch(candidate) else None


def _timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(float(seconds) * 1000)))
    total_seconds, milliseconds = divmod(total_milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if milliseconds:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _item_value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def format_transcript_items(items: Iterable[Any]) -> str:
    lines: list[str] = []
    last_text = ""
    for item in items:
        raw_text = _item_value(item, "text", "")
        text = html.unescape(str(raw_text or "")).replace("\n", " ").strip()
        if not text or text == last_text:
            continue
        try:
            start = float(_item_value(item, "start", _item_value(item, "offset", 0)) or 0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            duration = float(_item_value(item, "duration", 0) or 0)
        except (TypeError, ValueError):
            duration = 0.0
        end = max(start, start + duration)
        lines.append(f"[{_timestamp(start)} --> {_timestamp(end)}] {text}")
        last_text = text
    return "\n".join(lines).strip()


def _fetch_with_youtube_transcript_api(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
    except Exception as english_error:
        logger.info("English transcript selection failed; trying the first available language: %s", type(english_error).__name__)
        available = api.list(video_id)
        selected = next(iter(available), None)
        if selected is None:
            raise english_error
        fetched = selected.fetch()

    items = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else fetched
    transcript = format_transcript_items(items)
    if not transcript:
        raise RuntimeError("YouTube returned an empty transcript")
    return transcript


def _fetch_with_rapidapi(video_id: str, api_key: str) -> str:
    response = requests.get(
        RAPIDAPI_URL,
        headers={
            "X-Rapidapi-Key": api_key,
            "X-Rapidapi-Host": RAPIDAPI_HOST,
        },
        params={"videoId": video_id},
        timeout=45,
    )
    if response.status_code != 200:
        raise RuntimeError(f"RapidAPI returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("RapidAPI returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("transcript"), list):
        raise RuntimeError("RapidAPI response did not contain a transcript")
    transcript = format_transcript_items(payload["transcript"])
    if not transcript:
        raise RuntimeError("RapidAPI returned an empty transcript")
    return transcript


def fetch_transcript(value: str, rapidapi_key: str = "") -> tuple[str | None, str | None]:
    video_id = extract_video_id(value)
    if not video_id:
        return None, "Enter a valid YouTube URL or 11-character video ID."

    local_error = ""
    try:
        transcript = _fetch_with_youtube_transcript_api(video_id)
        logger.info("Transcript loaded directly from YouTube for video %s", video_id)
        return transcript, None
    except Exception as exc:
        local_error = str(exc).strip() or type(exc).__name__
        logger.warning("Direct YouTube transcript lookup failed: %s", local_error)

    if rapidapi_key:
        try:
            transcript = _fetch_with_rapidapi(video_id, rapidapi_key)
            logger.info("Transcript loaded through the RapidAPI fallback for video %s", video_id)
            return transcript, None
        except Exception as exc:
            rapid_error = str(exc).strip() or type(exc).__name__
            logger.warning("RapidAPI transcript lookup failed: %s", rapid_error)
            return None, f"Transcript lookup failed (YouTube: {local_error}; RapidAPI: {rapid_error})."

    return None, f"Transcript lookup failed ({local_error}). Add a RapidAPI key in Settings or paste a transcript."


def filter_duplicate_transcript_lines(transcript_text: str) -> str:
    if not transcript_text:
        return ""
    output: list[str] = []
    last_content = ""
    timestamp_pattern = re.compile(
        r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)?\]?[:\s-]*)"
    )
    for line in transcript_text.splitlines():
        content = timestamp_pattern.sub("", line).strip()
        if content and content == last_content:
            continue
        output.append(line)
        if content:
            last_content = content
    return "\n".join(output).strip()
