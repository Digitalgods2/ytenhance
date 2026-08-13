from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import customtkinter as ctk

import app_config
from app_config import SettingsStore
from model_clients import _error_message, _gemini_output_text, _openai_output_text, parse_model_choice
from prompt_loader import build_user_input, load_task_prompt
from transcripts import extract_video_id, filter_duplicate_transcript_lines, format_transcript_items
from youtube_enhance import ContextMenu, YouTubeEnhanceApp, parse_titles, split_summary, titlecase_chapters


class SettingsTests(unittest.TestCase):
    def test_secrets_round_trip_without_plaintext_storage(self) -> None:
        # macOS Keychain writes require a GUI security session. The dedicated
        # Keychain test below supplies a fake backend for deterministic CI.
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app_config.sys,
            "platform",
            "linux" if app_config.sys.platform == "darwin" else app_config.sys.platform,
        ):
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            store.load()
            store.save(
                {
                    "OPENAI_API_KEY": "test-openai-secret",
                    "GEMINI_API_KEY": "test-gemini-secret",
                    "RAPIDAPI_KEY": "test-rapid-secret",
                    "LAST_MODEL": "Gemini · gemini-3.6-flash",
                }
            )

            on_disk = path.read_text(encoding="utf-8")
            self.assertNotIn("test-openai-secret", on_disk)
            self.assertNotIn("test-gemini-secret", on_disk)
            self.assertNotIn("test-rapid-secret", on_disk)
            self.assertIsInstance(json.loads(on_disk), dict)

            loaded = SettingsStore(path)
            loaded.load()
            self.assertEqual(loaded.get("OPENAI_API_KEY"), "test-openai-secret")
            self.assertEqual(loaded.get("GEMINI_API_KEY"), "test-gemini-secret")
            self.assertEqual(loaded.get("RAPIDAPI_KEY"), "test-rapid-secret")
            self.assertEqual(loaded.get("LAST_MODEL"), "Gemini · gemini-3.6-flash")

    def test_macos_secrets_use_keychain_references(self) -> None:
        class FakeKeyring:
            def __init__(self) -> None:
                self.values: dict[tuple[str, str], str] = {}

            def set_password(self, service: str, key: str, value: str) -> None:
                self.values[(service, key)] = value

            def get_password(self, service: str, key: str) -> str | None:
                return self.values.get((service, key))

            def delete_password(self, service: str, key: str) -> None:
                self.values.pop((service, key), None)

        fake_keyring = FakeKeyring()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app_config.sys, "platform", "darwin"
        ), mock.patch.object(app_config, "_load_keyring", return_value=fake_keyring):
            path = Path(directory) / "settings.json"
            store = SettingsStore(path, keychain_service="YouTube Enhance Tests")
            store.load()
            store.save({"OPENAI_API_KEY": "mac-secret"})

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["OPENAI_API_KEY"], "keychain:OPENAI_API_KEY")
            self.assertNotIn("mac-secret", path.read_text(encoding="utf-8"))

            loaded = SettingsStore(path, keychain_service="YouTube Enhance Tests")
            loaded.load()
            self.assertEqual(loaded.get("OPENAI_API_KEY"), "mac-secret")


class PromptTests(unittest.TestCase):
    def test_all_task_prompts_load(self) -> None:
        for task in ("titles", "summary", "chapters"):
            system_prompt, user_prompt = load_task_prompt(task)
            self.assertGreater(len(system_prompt), 100)
            self.assertIsInstance(user_prompt, str)

    def test_user_prompt_and_transcript_are_joined(self) -> None:
        self.assertEqual(build_user_input("Extra directions", "Transcript"), "Extra directions\n\nTranscript")
        self.assertEqual(build_user_input("", "Transcript"), "Transcript")


class TranscriptTests(unittest.TestCase):
    def test_extracts_common_youtube_urls(self) -> None:
        video_id = "dQw4w9WgXcQ"
        values = [
            video_id,
            f"https://www.youtube.com/watch?v={video_id}&t=10",
            f"https://youtu.be/{video_id}",
            f"https://youtube.com/shorts/{video_id}",
            f"https://www.youtube.com/live/{video_id}",
        ]
        for value in values:
            self.assertEqual(extract_video_id(value), video_id)
        self.assertIsNone(extract_video_id("https://example.com/not-youtube"))

    def test_transcript_items_use_timestamp_ranges(self) -> None:
        output = format_transcript_items(
            [
                {"text": "Hello &amp; welcome", "start": 0.0, "duration": 1.5},
                {"text": "Next idea", "start": 61.25, "duration": 2.0},
            ]
        )
        self.assertIn("[00:00:00 --> 00:00:01.500] Hello & welcome", output)
        self.assertIn("[00:01:01.250 --> 00:01:03.250] Next idea", output)

    def test_adjacent_duplicate_lines_are_filtered(self) -> None:
        source = "[00:00:01] Repeat\n[00:00:02] Repeat\n[00:00:03] New"
        result = filter_duplicate_transcript_lines(source)
        self.assertEqual(result.count("Repeat"), 1)
        self.assertIn("New", result)


class ModelClientTests(unittest.TestCase):
    def test_model_choice_parsing(self) -> None:
        self.assertEqual(parse_model_choice("OpenAI · gpt-5.6-terra").provider, "openai")
        self.assertEqual(parse_model_choice("Gemini · gemini-3.6-flash").provider, "gemini")
        self.assertEqual(parse_model_choice("google/gemini-pro-latest").model, "gemini-pro-latest")

    def test_openai_text_extraction_scans_all_messages(self) -> None:
        payload = {
            "output": [
                {"type": "reasoning", "content": []},
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "First"},
                        {"type": "output_text", "text": "Second"},
                    ],
                },
            ]
        }
        self.assertEqual(_openai_output_text(payload), "First\nSecond")

    def test_gemini_text_extraction_scans_model_steps(self) -> None:
        payload = {
            "steps": [
                {"type": "thought", "content": []},
                {"type": "model_output", "content": [{"type": "text", "text": "Result"}]},
            ]
        }
        self.assertEqual(_gemini_output_text(payload), "Result")

    def test_api_errors_redact_keys(self) -> None:
        class FakeResponse:
            status_code = 401

            @staticmethod
            def json() -> dict:
                return {"error": {"message": "Incorrect API key: sk-proj-************abcd"}}

        message = _error_message(FakeResponse(), "OpenAI")
        self.assertIn("[redacted OpenAI key]", message)
        self.assertNotIn("sk-proj", message)


class OutputFormattingTests(unittest.TestCase):
    def test_titles_remove_numbering(self) -> None:
        self.assertEqual(parse_titles("1. First Title\n- Second Title"), ["First Title", "Second Title"])

    def test_chapter_titlecase_preserves_timestamp(self) -> None:
        self.assertEqual(titlecase_chapters("00:01:20 systems thinking"), "00:01:20 Systems Thinking")

    def test_summary_hashtags_are_split_into_chips(self) -> None:
        body, tags = split_summary("First paragraph.\n\nSecond paragraph.\n\n#one,#two,#three")
        self.assertEqual(body, "First paragraph.\n\nSecond paragraph.")
        self.assertEqual(tags, ["#one", "#two", "#three"])


class _NoThreadYouTubeEnhanceApp(YouTubeEnhanceApp):
    def load_models_async(self) -> None:
        pass


@unittest.skipUnless(os.name == "nt", "CustomTkinter UI checks require Windows")
class UiControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        store = SettingsStore(Path(cls.temporary_directory.name) / "settings.json")
        store.load()
        store.update({"OPENAI_API_KEY": "preserved-key"})
        cls.app = _NoThreadYouTubeEnhanceApp(store)
        cls.app.withdraw()
        cls.app.update_idletasks()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.app.clear_all()

    def test_paste_copy_and_clear_controls(self) -> None:
        self.app._read_clipboard = lambda: "https://youtu.be/dQw4w9WgXcQ"
        self.app.paste_url()
        self.assertEqual(self.app.url_entry.get(), "https://youtu.be/dQw4w9WgXcQ")

        self.app._read_clipboard = lambda: "00:01 A test transcript"
        self.app.paste_transcript()
        self.assertEqual(self.app.transcript_box.get("1.0", "end").strip(), "00:01 A test transcript")

        copied: list[str] = []
        self.app.clipboard_clear = copied.clear
        self.app.clipboard_append = copied.append
        self.app.copy_transcript()
        self.assertEqual(copied, ["00:01 A test transcript"])

        self.app.clear_url()
        self.app.clear_transcript()
        self.assertEqual(self.app.url_entry.get(), "")
        self.assertEqual(self.app.transcript_box.get("1.0", "end").strip(), "")

    def test_context_menu_pastes_directly_into_customtkinter_fields(self) -> None:
        self.app.url_entry.delete(0, "end")
        self.app.url_entry.insert(0, "old-key")
        self.app.url_entry._entry.selection_range(0, "end")
        entry_menu = ContextMenu(self.app.url_entry)
        self.app.url_entry.clipboard_get = lambda: "new-openai-key"
        entry_menu._paste()
        self.assertEqual(self.app.url_entry.get(), "new-openai-key")

        self.app.transcript_box.delete("1.0", "end")
        self.app.transcript_box.insert("1.0", "old transcript")
        self.app.transcript_box._textbox.tag_add("sel", "1.0", "end-1c")
        textbox_menu = ContextMenu(self.app.transcript_box)
        self.app.transcript_box.clipboard_get = lambda: "new transcript"
        textbox_menu._paste()
        self.assertEqual(self.app.transcript_box.get("1.0", "end").strip(), "new transcript")

    def test_clear_all_restores_startup_state_and_preserves_keys(self) -> None:
        self.app.url_entry.insert(0, "https://youtu.be/dQw4w9WgXcQ")
        self.app.transcript_box.insert("1.0", "00:01 Transcript")
        self.app.current_transcript = "cached transcript"
        self.app.transcript_cache["dQw4w9WgXcQ"] = "cached transcript"
        self.app.results = {"titles": "A Title", "summary": "A Summary", "chapters": "00:00:00 Start"}
        self.app.title_var.set("A Title")
        self.app.full_list_var.set(True)
        previous_generation = self.app.reset_generation

        self.app.clear_all()

        self.assertEqual(self.app.reset_generation, previous_generation + 1)
        self.assertEqual(self.app.url_entry.get(), "")
        self.assertEqual(self.app.transcript_box.get("1.0", "end").strip(), "")
        self.assertEqual(self.app.current_transcript, "")
        self.assertEqual(self.app.transcript_cache, {})
        self.assertEqual(self.app.results, {"titles": "", "summary": "", "chapters": ""})
        self.assertFalse(self.app.full_list_var.get())
        self.assertEqual(self.app.settings.get("OPENAI_API_KEY"), "preserved-key")
        self.assertEqual(self.app.status_label.cget("text"), "Ready")

    def test_reference_output_layout_renders_populated_results(self) -> None:
        self.app.results = {
            "titles": "First Title\nSecond Title",
            "summary": "First paragraph.\n\nSecond paragraph.\n\n#one,#two",
            "chapters": "00:00:00 Start Here\n00:01:00 Next Step",
        }

        self.app._render_results()

        self.assertEqual(self.app.output_badges["titles"].cget("text"), "2 OF 5")
        self.assertEqual(self.app.output_badges["chapters"].cget("text"), "2 CH")
        self.assertEqual(self.app.summary_tags, ["#one", "#two"])
        self.assertIsInstance(self.app.output_frames["summary"].winfo_children()[0], ctk.CTkTextbox)


if __name__ == "__main__":
    unittest.main()
