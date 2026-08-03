"""Direct OpenAI and Gemini prompting clients."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

import requests

from prompt_loader import build_user_input, load_task_prompt


logger = logging.getLogger("YouTubeEnhance")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

DEFAULT_MODEL_CHOICES = [
    "OpenAI · gpt-5.6-terra",
    "OpenAI · gpt-5.6-luna",
    "OpenAI · gpt-5.6-sol",
    "Gemini · gemini-3.6-flash",
    "Gemini · gemini-flash-latest",
    "Gemini · gemini-pro-latest",
]


class ModelAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    model: str


def parse_model_choice(value: str) -> ModelChoice:
    value = (value or "").strip()
    if "·" in value:
        provider, model = (part.strip() for part in value.split("·", 1))
    elif "/" in value:
        provider, model = (part.strip() for part in value.split("/", 1))
    else:
        provider = "Gemini" if value.lower().startswith(("gemini", "gemma")) else "OpenAI"
        model = value
    normalized = provider.lower()
    if normalized in {"google", "gemini"}:
        provider = "gemini"
    elif normalized == "openai":
        provider = "openai"
    else:
        raise ModelAPIError(f"Unsupported model provider: {provider}")
    if not model:
        raise ModelAPIError("Select a model before analyzing.")
    return ModelChoice(provider, model.removeprefix("models/"))


def _error_message(response: requests.Response, provider: str) -> str:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif error:
                detail = str(error)
    except ValueError:
        pass
    detail = detail.strip().replace("\n", " ")
    detail = re.sub(r"\bsk-[A-Za-z0-9_.*-]+", "[redacted OpenAI key]", detail)
    detail = re.sub(r"\bAIza[A-Za-z0-9_.*-]+", "[redacted Gemini key]", detail)
    detail = detail[:300]
    suffix = f": {detail}" if detail else ""
    return f"{provider} API returned HTTP {response.status_code}{suffix}"


def _openai_output_text(payload: Mapping[str, Any]) -> str:
    pieces: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                text = str(content.get("text") or "").strip()
                if text:
                    pieces.append(text)
    return "\n".join(pieces).strip()


def _gemini_output_text(payload: Mapping[str, Any]) -> str:
    pieces: list[str] = []
    for step in payload.get("steps", []) or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for content in step.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") == "text":
                text = str(content.get("text") or "").strip()
                if text:
                    pieces.append(text)
    return "\n".join(pieces).strip()


class DirectModelClient:
    def __init__(self, settings: Any, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    def generate(self, selection: str, task: str, transcript: str) -> tuple[str | None, str | None]:
        try:
            choice = parse_model_choice(selection)
            system_prompt, user_prompt = load_task_prompt(task)
            user_input = build_user_input(user_prompt, transcript)
            if not user_input:
                raise ModelAPIError("The transcript is empty.")
            if choice.provider == "openai":
                output = self._generate_openai(choice.model, system_prompt, user_input)
            else:
                output = self._generate_gemini(choice.model, system_prompt, user_input)
            if not output:
                raise ModelAPIError(f"{choice.provider.title()} returned no text.")
            logger.info("Generated %s with %s/%s (%d characters)", task, choice.provider, choice.model, len(output))
            return output, None
        except (ModelAPIError, OSError, requests.RequestException, ValueError) as exc:
            message = str(exc).strip() or type(exc).__name__
            logger.error("Direct generation failed for %s: %s", task, message)
            return None, message

    def _generate_openai(self, model: str, system_prompt: str, user_input: str) -> str:
        api_key = self.settings.get("OPENAI_API_KEY")
        if not api_key:
            raise ModelAPIError("Add an OpenAI API key in Settings to use this model.")
        response = self.session.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "instructions": system_prompt,
                "input": user_input,
                "store": False,
                "max_output_tokens": 4096,
            },
            timeout=240,
        )
        if response.status_code != 200:
            raise ModelAPIError(_error_message(response, "OpenAI"))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelAPIError("OpenAI returned invalid JSON.") from exc
        return _openai_output_text(payload)

    def _generate_gemini(self, model: str, system_prompt: str, user_input: str) -> str:
        api_key = self.settings.get("GEMINI_API_KEY")
        if not api_key:
            raise ModelAPIError("Add a Gemini API key in Settings to use this model.")
        response = self.session.post(
            GEMINI_INTERACTIONS_URL,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "system_instruction": system_prompt,
                "input": user_input,
                "store": False,
                "generation_config": {"max_output_tokens": 4096},
            },
            timeout=240,
        )
        if response.status_code != 200:
            raise ModelAPIError(_error_message(response, "Gemini"))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelAPIError("Gemini returned invalid JSON.") from exc
        return _gemini_output_text(payload)

    def list_model_choices(self, full_list: bool = False) -> tuple[list[str], list[str]]:
        if not full_list:
            return list(DEFAULT_MODEL_CHOICES), []

        choices: list[str] = []
        warnings: list[str] = []
        openai_key = self.settings.get("OPENAI_API_KEY")
        gemini_key = self.settings.get("GEMINI_API_KEY")

        if openai_key:
            try:
                choices.extend(self._list_openai_models(openai_key))
            except (ModelAPIError, requests.RequestException, ValueError) as exc:
                warnings.append(str(exc))
        else:
            warnings.append("OpenAI key is not configured.")

        if gemini_key:
            try:
                choices.extend(self._list_gemini_models(gemini_key))
            except (ModelAPIError, requests.RequestException, ValueError) as exc:
                warnings.append(str(exc))
        else:
            warnings.append("Gemini key is not configured.")

        combined = list(dict.fromkeys(choices + DEFAULT_MODEL_CHOICES))
        combined.sort(key=lambda value: value.lower())
        return combined, warnings

    def _list_openai_models(self, api_key: str) -> list[str]:
        response = self.session.get(
            OPENAI_MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=45,
        )
        if response.status_code != 200:
            raise ModelAPIError(_error_message(response, "OpenAI model list"))
        payload = response.json()
        models: list[str] = []
        excluded = ("audio", "realtime", "transcribe", "tts", "image", "embedding", "moderation", "search", "codex")
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            model = str(item.get("id") or "") if isinstance(item, dict) else ""
            lower = model.lower()
            if model and lower.startswith(("gpt-", "o1", "o3", "o4")) and not any(word in lower for word in excluded):
                models.append(f"OpenAI · {model}")
        return sorted(set(models), key=str.lower)

    def _list_gemini_models(self, api_key: str) -> list[str]:
        response = self.session.get(
            GEMINI_MODELS_URL,
            headers={"x-goog-api-key": api_key},
            params={"pageSize": 1000},
            timeout=45,
        )
        if response.status_code != 200:
            raise ModelAPIError(_error_message(response, "Gemini model list"))
        payload = response.json()
        models: list[str] = []
        excluded = ("image", "embedding", "aqa", "tts", "live", "robotics", "lyria", "banana")
        for item in payload.get("models", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods", []) or []
            model = str(item.get("name") or "").removeprefix("models/")
            lower = model.lower()
            if model.startswith(("gemini", "gemma")) and "generateContent" in methods and not any(word in lower for word in excluded):
                models.append(f"Gemini · {model}")
        return sorted(set(models), key=str.lower)
