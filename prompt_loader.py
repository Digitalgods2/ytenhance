"""Load the three direct-prompt templates in development and packaged builds."""

from __future__ import annotations

import sys
from pathlib import Path


PROMPT_DIRECTORIES = {
    "titles": "create_video_titles",
    "summary": "create_video_summary",
    "chapters": "create_video_chapters",
}


def resource_root() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        return Path(bundled_root)
    return Path(__file__).resolve().parent


def load_task_prompt(task: str) -> tuple[str, str]:
    directory_name = PROMPT_DIRECTORIES.get(task)
    if not directory_name:
        raise ValueError(f"Unknown generation task: {task}")

    prompt_dir = resource_root() / directory_name
    system_path = prompt_dir / "system.md"
    user_path = prompt_dir / "user.md"
    if not system_path.is_file():
        raise FileNotFoundError(f"Missing prompt file: {system_path}")

    system_prompt = system_path.read_text(encoding="utf-8-sig").strip()
    user_prompt = user_path.read_text(encoding="utf-8-sig").strip() if user_path.is_file() else ""
    return system_prompt, user_prompt


def build_user_input(user_prompt: str, transcript: str) -> str:
    transcript = (transcript or "").strip()
    if user_prompt.strip():
        return f"{user_prompt.strip()}\n\n{transcript}"
    return transcript
