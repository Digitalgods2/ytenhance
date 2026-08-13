"""Per-user settings storage for YouTube Enhance.

Secrets use Windows DPAPI on Windows and the login Keychain on macOS. Other
platforms retain the restricted-file fallback for source compatibility.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Mapping


APP_NAME = "YouTubeEnhance"
SETTINGS_FILENAME = "settings.json"
SECRET_KEYS = frozenset({"OPENAI_API_KEY", "GEMINI_API_KEY", "RAPIDAPI_KEY"})
KEYCHAIN_SERVICE = "YouTube Enhance"

DEFAULT_SETTINGS = {
    "OPENAI_API_KEY": "",
    "GEMINI_API_KEY": "",
    "RAPIDAPI_KEY": "",
    "LAST_MODEL": "OpenAI · gpt-5.6-terra",
}


def get_user_data_dir() -> Path:
    """Return an OS-appropriate, per-user application-data directory."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def get_settings_path() -> Path:
    return get_user_data_dir() / SETTINGS_FILENAME


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _windows_protect(value: str) -> str:
    raw = value.encode("utf-8")
    raw_buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    input_blob = _DataBlob(len(raw), raw_buffer)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        APP_NAME,
        None,
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _windows_unprotect(value: str) -> str:
    protected = base64.b64decode(value.removeprefix("dpapi:").encode("ascii"))
    protected_buffer = (ctypes.c_ubyte * len(protected)).from_buffer_copy(protected)
    input_blob = _DataBlob(len(protected), protected_buffer)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return raw.decode("utf-8")
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _load_keyring():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - dependency is bundled on macOS
        raise OSError("macOS Keychain support is unavailable.") from exc
    return keyring


def _macos_store_secret(service: str, key: str, value: str) -> None:
    keyring = _load_keyring()
    try:
        if value:
            keyring.set_password(service, key, value)
        elif keyring.get_password(service, key) is not None:
            keyring.delete_password(service, key)
    except Exception as exc:
        raise OSError("Could not update the macOS Keychain.") from exc


def _macos_read_secret(service: str, key: str) -> str:
    try:
        return str(_load_keyring().get_password(service, key) or "")
    except Exception as exc:
        raise OSError("Could not read the macOS Keychain.") from exc


def protect_secret(
    value: str,
    *,
    key: str | None = None,
    keychain_service: str = KEYCHAIN_SERVICE,
) -> str:
    if not value:
        if sys.platform == "darwin" and key:
            _macos_store_secret(keychain_service, key, "")
        return ""
    if sys.platform == "darwin":
        if not key:
            raise ValueError("A Keychain account name is required on macOS.")
        _macos_store_secret(keychain_service, key, value)
        return f"keychain:{key}"
    if os.name == "nt":
        return _windows_protect(value)
    return "local:" + base64.b64encode(value.encode("utf-8")).decode("ascii")


def unprotect_secret(
    value: str,
    *,
    key: str | None = None,
    keychain_service: str = KEYCHAIN_SERVICE,
) -> str:
    if not value:
        return ""
    if value.startswith("dpapi:"):
        if os.name != "nt":
            return ""
        return _windows_unprotect(value)
    if value.startswith("keychain:"):
        if sys.platform != "darwin":
            return ""
        account = key or value.removeprefix("keychain:")
        return _macos_read_secret(keychain_service, account)
    if value.startswith("local:"):
        return base64.b64decode(value[6:].encode("ascii")).decode("utf-8")
    # Accept legacy plaintext settings once so saving can migrate them.
    return value


class SettingsStore:
    """Load and save application settings without exposing keys in logs."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        keychain_service: str = KEYCHAIN_SERVICE,
    ):
        self.path = Path(path) if path else get_settings_path()
        self.keychain_service = keychain_service
        self._values = dict(DEFAULT_SETTINGS)

    def load(self) -> dict[str, str]:
        values = dict(DEFAULT_SETTINGS)
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    for key in DEFAULT_SETTINGS:
                        if key not in payload:
                            continue
                        raw = str(payload[key] or "")
                        values[key] = (
                            unprotect_secret(
                                raw,
                                key=key,
                                keychain_service=self.keychain_service,
                            )
                            if key in SECRET_KEYS
                            else raw
                        )
            except (OSError, ValueError, UnicodeError):
                # A corrupt settings file must not prevent the GUI from starting.
                pass

        for key in SECRET_KEYS:
            if not values.get(key):
                values[key] = os.environ.get(key, "").strip()

        self._values = values
        return dict(values)

    def save(self, updates: Mapping[str, str] | None = None) -> None:
        if updates:
            for key, value in updates.items():
                if key in DEFAULT_SETTINGS:
                    self._values[key] = str(value or "").strip()

        payload = {
            key: (
                protect_secret(
                    value,
                    key=key,
                    keychain_service=self.keychain_service,
                )
                if key in SECRET_KEYS
                else value
            )
            for key, value in self._values.items()
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=self.path.parent,
                prefix="settings-",
                suffix=".tmp",
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def get(self, key: str, default: str = "") -> str:
        return str(self._values.get(key, default) or "").strip()

    def update(self, values: Mapping[str, str]) -> None:
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                self._values[key] = str(value or "").strip()

    def redacted_summary(self) -> dict[str, str]:
        return {
            key: ("set" if self.get(key) else "empty")
            for key in sorted(SECRET_KEYS)
        }
