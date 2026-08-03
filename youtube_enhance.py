"""Desktop YouTube analyzer powered by direct model prompting."""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from app_config import SettingsStore, get_user_data_dir
from model_clients import DEFAULT_MODEL_CHOICES, DirectModelClient
from prompt_loader import load_task_prompt
from transcripts import extract_video_id, fetch_transcript, filter_duplicate_transcript_lines


APP_TITLE = "YouTube Enhance"
CORNER_RADIUS = 8


def configure_logging() -> Path:
    log_dir = get_user_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "youtube_enhance.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )
    return log_path


logger = logging.getLogger("YouTubeEnhance")


def parse_titles(raw: str | None) -> list[str]:
    titles: list[str] = []
    for line in (raw or "").splitlines():
        cleaned = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s*)", "", line).strip()
        if cleaned and cleaned not in {"```", "```text"}:
            titles.append(cleaned)
    return titles[:5]


def _titlecase(value: str) -> str:
    if not value:
        return ""
    small_words = {"a", "an", "the", "and", "but", "or", "for", "nor", "on", "at", "to", "from", "by", "of", "in", "with", "as", "vs", "via"}
    tokens = re.split(r"(\s+)", value)
    word_indices = [index for index, token in enumerate(tokens) if token and not token.isspace()]
    first = word_indices[0] if word_indices else -1
    last = word_indices[-1] if word_indices else -1

    for index, token in enumerate(tokens):
        if not token or token.isspace():
            continue
        parts: list[str] = []
        for part in token.split("-"):
            if not part:
                parts.append(part)
            elif any(char.isdigit() for char in part) or (any(char.islower() for char in part) and any(char.isupper() for char in part)):
                parts.append(part)
            elif part.isupper() and len(part) <= 3:
                parts.append(part)
            else:
                parts.append(part[:1].upper() + part[1:].lower())
        transformed = "-".join(parts)
        if index not in {first, last} and transformed.lower() in small_words and "-" not in transformed:
            transformed = transformed.lower()
        tokens[index] = transformed
    return "".join(tokens)


def titlecase_chapters(text: str | None) -> str:
    if not text:
        return ""
    pattern = re.compile(
        r"^(?P<prefix>\s*(?:[-*+]|\d+[.)])?\s*)"
        r"(?P<timestamp>\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*)?"
        r"(?P<body>.*)$"
    )
    output: list[str] = []
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            output.append(_titlecase(line))
            continue
        prefix = match.group("prefix") or ""
        timestamp = match.group("timestamp") or ""
        body = (match.group("body") or "").strip()
        output.append(f"{prefix}{timestamp}{_titlecase(body)}")
    return "\n".join(output)


class ContextMenu:
    def __init__(self, widget: tk.Widget):
        self.widget = widget
        self.native_widget = getattr(widget, "_entry", None) or getattr(widget, "_textbox", None) or widget
        self.menu = tk.Menu(widget, tearoff=0)
        self.menu.add_command(label="Cut", command=self._cut)
        self.menu.add_command(label="Copy", command=self._copy)
        self.menu.add_command(label="Paste", command=self._paste)
        self.menu.add_separator()
        self.menu.add_command(label="Select All", command=self._select_all)
        widget.bind("<Button-3>", self._show, add="+")

    def _show(self, event: tk.Event) -> None:
        try:
            self.widget.focus_set()
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _selected_text(self) -> str:
        try:
            if isinstance(self.native_widget, tk.Text):
                ranges = self.native_widget.tag_ranges(tk.SEL)
                if not ranges:
                    return ""
                return self.native_widget.get(ranges[0], ranges[1])
            if self.native_widget.selection_present():
                return self.native_widget.get()[
                    self.native_widget.index(tk.SEL_FIRST):self.native_widget.index(tk.SEL_LAST)
                ]
        except tk.TclError:
            pass
        return ""

    def _delete_selection(self) -> None:
        try:
            if isinstance(self.native_widget, tk.Text):
                ranges = self.native_widget.tag_ranges(tk.SEL)
                if ranges:
                    self.native_widget.delete(ranges[0], ranges[1])
            elif self.native_widget.selection_present():
                self.native_widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            pass

    def _copy(self) -> None:
        selected = self._selected_text()
        if not selected:
            return
        self.widget.clipboard_clear()
        self.widget.clipboard_append(selected)

    def _cut(self) -> None:
        selected = self._selected_text()
        if not selected:
            return
        self.widget.clipboard_clear()
        self.widget.clipboard_append(selected)
        self._delete_selection()

    def _paste(self) -> None:
        try:
            clipboard_text = self.widget.clipboard_get()
        except tk.TclError:
            return
        self.native_widget.focus_set()
        self._delete_selection()
        try:
            self.native_widget.insert(tk.INSERT, str(clipboard_text))
        except tk.TclError:
            pass

    def _select_all(self) -> None:
        try:
            self.native_widget.focus_set()
            if isinstance(self.native_widget, tk.Text):
                self.native_widget.tag_add(tk.SEL, "1.0", tk.END)
                self.native_widget.mark_set(tk.INSERT, tk.END)
            else:
                self.native_widget.selection_range(0, tk.END)
                self.native_widget.icursor(tk.END)
        except tk.TclError:
            pass


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent: "YouTubeEnhanceApp"):
        super().__init__(parent)
        self.parent_app = parent
        self.store = parent.settings
        self.title("Settings")
        self.geometry("760x430")
        self.minsize(650, 400)
        self.transient(parent)
        self.grab_set()
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Provider Keys",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(18, 8))

        self.entries: dict[str, ctk.CTkEntry] = {}
        fields = [
            ("OPENAI_API_KEY", "OpenAI API key", "sk-..."),
            ("GEMINI_API_KEY", "Gemini API key", "AI..."),
            ("RAPIDAPI_KEY", "RapidAPI key (optional)", "Fallback transcript service"),
        ]
        for row, (key, label, placeholder) in enumerate(fields, start=1):
            ctk.CTkLabel(self, text=f"{label}:").grid(row=row, column=0, sticky="w", padx=18, pady=9)
            entry = ctk.CTkEntry(self, show="•", placeholder_text=placeholder, corner_radius=CORNER_RADIUS)
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 18), pady=9)
            current = self.store.get(key)
            if current:
                entry.insert(0, current)
            ContextMenu(entry)
            self.entries[key] = entry

        self.show_keys = ctk.CTkCheckBox(self, text="Show keys", command=self._toggle_keys)
        self.show_keys.grid(row=4, column=1, sticky="w", padx=8, pady=(3, 12))

        storage_text = (
            "Keys are encrypted for this Windows account and saved at:\n"
            f"{self.store.path}"
        )
        ctk.CTkLabel(
            self,
            text=storage_text,
            justify="left",
            anchor="w",
            text_color=("gray35", "gray70"),
            wraplength=690,
        ).grid(row=5, column=0, columnspan=2, sticky="ew", padx=18, pady=(5, 15))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", padx=18, pady=(5, 18))
        ctk.CTkButton(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(buttons, text="Save", fg_color="#2ea043", command=self._save).pack(side="right")

    def _toggle_keys(self) -> None:
        show = "" if self.show_keys.get() else "•"
        for entry in self.entries.values():
            entry.configure(show=show)

    def _save(self) -> None:
        updates = {key: entry.get().strip() for key, entry in self.entries.items()}
        try:
            self.store.save(updates)
        except OSError as exc:
            messagebox.showerror("Settings Error", f"Could not save settings:\n{exc}", parent=self)
            return
        logger.info("Settings saved: %s", self.store.redacted_summary())
        self.parent_app.on_settings_saved()
        self.destroy()


class YouTubeEnhanceApp(ctk.CTk):
    def __init__(self, settings: SettingsStore):
        super().__init__()
        self.settings = settings
        self.client = DirectModelClient(settings)
        self.current_transcript = ""
        self.transcript_cache: dict[str, str] = {}
        self.results = {"titles": "", "summary": "", "chapters": ""}
        self.title_var = tk.StringVar()
        self.reset_generation = 0

        self.title(APP_TITLE)
        self.geometry("1160x790")
        self.minsize(950, 650)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=3)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_input_panel()
        self._build_output_panel()
        self._build_status_bar()
        self.load_models_async()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.pack(fill="x")
        ctk.CTkLabel(
            title_group,
            text="YouTube Enhance",
            font=ctk.CTkFont(size=25, weight="bold"),
        ).pack(pady=(10, 2))
        ctk.CTkLabel(
            title_group,
            text="Direct OpenAI and Gemini analysis",
            text_color=("gray35", "gray70"),
        ).pack(pady=(0, 10))

        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.place(relx=1.0, rely=0.5, x=-12, anchor="e")
        self.theme_menu = ctk.CTkOptionMenu(
            header_actions,
            width=92,
            values=["Dark", "Light", "System"],
            command=ctk.set_appearance_mode,
        )
        self.theme_menu.pack(side="left", padx=(0, 6))
        self.theme_menu.set("Dark")
        ctk.CTkButton(
            header_actions,
            text="Clear All",
            width=84,
            command=self.clear_all,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            header_actions,
            text="Settings",
            width=84,
            command=self.open_settings,
        ).pack(side="left")

    def _build_input_panel(self) -> None:
        left = ctk.CTkFrame(self, border_width=1, border_color="gray30", corner_radius=CORNER_RADIUS)
        left.grid(row=1, column=0, sticky="nsew", padx=(10, 5), pady=10)
        left.grid_columnconfigure(0, weight=1)
        left.grid_columnconfigure(1, weight=3)
        left.grid_rowconfigure(6, weight=1)

        ctk.CTkLabel(left, text="Select model:").grid(row=0, column=0, sticky="w", padx=10, pady=(18, 6))
        self.model_combobox = ctk.CTkComboBox(
            left,
            values=["Loading..."],
            state="disabled",
            corner_radius=CORNER_RADIUS,
        )
        self.model_combobox.grid(row=0, column=1, sticky="ew", padx=10, pady=(18, 6))

        self.full_list_var = tk.BooleanVar(value=False)
        self.full_list_checkbox = ctk.CTkCheckBox(
            left,
            text="Load full model list",
            variable=self.full_list_var,
            command=self.load_models_async,
            corner_radius=CORNER_RADIUS,
        )
        self.full_list_checkbox.grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 13))

        url_header = ctk.CTkFrame(left, fg_color="transparent")
        url_header.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 0))
        ctk.CTkLabel(url_header, text="YouTube URL or video ID:").pack(side="left")
        ctk.CTkButton(url_header, text="Clear", width=62, height=26, command=self.clear_url).pack(side="right")
        ctk.CTkButton(url_header, text="Paste", width=62, height=26, command=self.paste_url).pack(side="right", padx=(0, 6))
        self.url_entry = ctk.CTkEntry(left, placeholder_text="https://www.youtube.com/watch?v=...", corner_radius=CORNER_RADIUS)
        self.url_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(5, 13))
        ContextMenu(self.url_entry)

        transcript_header = ctk.CTkFrame(left, fg_color="transparent")
        transcript_header.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 0))
        ctk.CTkLabel(transcript_header, text="Transcript (optional):").pack(side="left")
        ctk.CTkButton(transcript_header, text="Clear", width=62, height=26, command=self.clear_transcript).pack(side="right")
        ctk.CTkButton(transcript_header, text="Copy", width=62, height=26, command=self.copy_transcript).pack(side="right", padx=(0, 6))
        ctk.CTkButton(transcript_header, text="Paste", width=62, height=26, command=self.paste_transcript).pack(side="right", padx=(0, 6))
        ctk.CTkLabel(
            left,
            text="Paste a transcript here to skip automatic retrieval.",
            text_color=("gray35", "gray70"),
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 4))
        self.transcript_box = ctk.CTkTextbox(left, height=260, corner_radius=CORNER_RADIUS)
        self.transcript_box.grid(row=6, column=0, columnspan=2, sticky="nsew", padx=10, pady=(0, 10))
        ContextMenu(self.transcript_box)

        self.analyze_button = ctk.CTkButton(
            left,
            text="Analyze",
            height=38,
            font=ctk.CTkFont(weight="bold"),
            command=self.analyze_async,
        )
        self.analyze_button.grid(row=7, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 8))
        self.progress_bar = ctk.CTkProgressBar(left, orientation="horizontal", mode="indeterminate", corner_radius=CORNER_RADIUS)

    def _build_output_panel(self) -> None:
        right = ctk.CTkScrollableFrame(self, corner_radius=CORNER_RADIUS)
        self.output_panel = right
        right.grid(row=1, column=1, sticky="nsew", padx=(5, 10), pady=10)
        right.grid_columnconfigure(0, weight=1)

        self.output_frames: dict[str, ctk.CTkFrame] = {}
        names = [("titles", "Titles"), ("summary", "Summary"), ("chapters", "Chapters")]
        for index, (task, label) in enumerate(names):
            row = index * 2
            header = ctk.CTkFrame(right, corner_radius=CORNER_RADIUS)
            header.grid(row=row, column=0, sticky="ew", padx=10, pady=(10 if index == 0 else 7, 5))
            header.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(header, text=label, font=ctk.CTkFont(size=17, weight="bold")).grid(row=0, column=0, sticky="w", padx=10, pady=6)
            actions = ctk.CTkFrame(header, fg_color="transparent")
            actions.grid(row=0, column=1, sticky="e", padx=(0, 5))
            ctk.CTkButton(actions, text="Regen", width=72, command=lambda current=task: self.regenerate(current)).pack(side="left", padx=(0, 5))
            ctk.CTkButton(actions, text="Copy", width=72, command=lambda current=task: self.copy_result(current)).pack(side="left")

            frame = ctk.CTkFrame(right, corner_radius=CORNER_RADIUS)
            frame.grid(row=row + 1, column=0, sticky="nsew", padx=10, pady=(0, 10))
            self.output_frames[task] = frame

        self._render_results()

    def _build_status_bar(self) -> None:
        status = ctk.CTkFrame(self, height=34, corner_radius=0)
        status.grid(row=2, column=0, columnspan=2, sticky="ew")
        status.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(status, text="Ready", anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew", padx=10, pady=3)

    def _set_status(self, message: str) -> None:
        normalized = " ".join(str(message or "").split())
        display = normalized if len(normalized) <= 160 else normalized[:157] + "..."
        self.status_label.configure(text=display or "Ready")

    def _set_generation_status(self, generation: int, message: str) -> None:
        if generation == self.reset_generation:
            self._set_status(message)

    def _read_clipboard(self) -> str | None:
        try:
            value = self.clipboard_get()
        except tk.TclError:
            self._set_status("The clipboard does not contain text.")
            return None
        text = str(value)
        if not text:
            self._set_status("The clipboard does not contain text.")
            return None
        return text

    def paste_url(self) -> None:
        text = self._read_clipboard()
        if text is None:
            return
        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, text.strip())
        self.url_entry.focus_set()
        self.url_entry.icursor(tk.END)
        self._set_status("URL pasted.")

    def clear_url(self) -> None:
        self.url_entry.delete(0, tk.END)
        self.url_entry.focus_set()
        self._set_status("URL cleared.")

    def paste_transcript(self) -> None:
        text = self._read_clipboard()
        if text is None:
            return
        self.transcript_box.delete("1.0", tk.END)
        self.transcript_box.insert("1.0", text)
        self.transcript_box.focus_set()
        self.transcript_box.mark_set("insert", tk.END)
        self.current_transcript = ""
        self._set_status("Transcript pasted.")

    def copy_transcript(self) -> None:
        text = self.transcript_box.get("1.0", tk.END).strip()
        if not text:
            self._set_status("There is no transcript to copy.")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status("Transcript copied.")

    def clear_transcript(self) -> None:
        self.transcript_box.delete("1.0", tk.END)
        self.transcript_box.focus_set()
        self.current_transcript = ""
        self._set_status("Transcript cleared.")

    def clear_all(self) -> None:
        """Restore the transient UI state while preserving saved settings."""
        self.reset_generation += 1
        self.current_transcript = ""
        self.transcript_cache.clear()
        self.results = {"titles": "", "summary": "", "chapters": ""}
        self.title_var.set("")

        self.url_entry.delete(0, tk.END)
        self.transcript_box.delete("1.0", tk.END)
        self.full_list_var.set(False)
        self._set_busy(False)
        self._render_results()

        ctk.set_appearance_mode("Dark")
        self.theme_menu.set("Dark")
        canvas = getattr(self.output_panel, "_parent_canvas", None)
        if canvas is not None:
            canvas.yview_moveto(0)

        self.url_entry.focus_set()
        self._set_status("Ready")
        self.load_models_async()

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.analyze_button.configure(text="Analyzing...", state="disabled")
            self.progress_bar.grid(row=8, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
            self.progress_bar.start()
        else:
            self.progress_bar.stop()
            self.progress_bar.grid_forget()
            self.analyze_button.configure(text="Analyze", state="normal")

    def open_settings(self) -> None:
        SettingsDialog(self)

    def on_settings_saved(self) -> None:
        self._set_status("Settings saved securely.")
        self.load_models_async()

    def load_models_async(self) -> None:
        self.model_combobox.configure(values=["Loading..."], state="disabled")
        self.model_combobox.set("Loading...")
        full_list = bool(self.full_list_var.get())
        threading.Thread(target=self._load_models, args=(full_list,), daemon=True).start()

    def _load_models(self, full_list: bool) -> None:
        models, warnings = self.client.list_model_choices(full_list)
        self.after(0, self._finish_model_load, models, warnings)

    def _finish_model_load(self, models: list[str], warnings: list[str]) -> None:
        models = models or list(DEFAULT_MODEL_CHOICES)
        preferred = self.settings.get("LAST_MODEL")
        self.model_combobox.configure(values=models, state="normal")
        self.model_combobox.set(preferred if preferred in models else models[0])
        if warnings and self.full_list_var.get():
            self._set_status("Using defaults plus available models: " + " ".join(warnings))
        else:
            self._set_status("Ready")

    def analyze_async(self) -> None:
        selection = self.model_combobox.get().strip()
        url = self.url_entry.get().strip()
        transcript_input = self.transcript_box.get("1.0", tk.END).strip()
        if not transcript_input and not url:
            self._set_status("Enter a YouTube URL or paste a transcript.")
            return
        generation = self.reset_generation
        self._set_busy(True)
        threading.Thread(
            target=self._analyze,
            args=(selection, url, transcript_input, generation),
            daemon=True,
        ).start()

    def _analyze(self, selection: str, url: str, transcript_input: str, generation: int) -> None:
        if generation != self.reset_generation:
            return
        transcript = ""
        if transcript_input:
            transcript = filter_duplicate_transcript_lines(transcript_input)
            self.after(0, self._set_generation_status, generation, "Using pasted transcript...")
        else:
            video_id = extract_video_id(url)
            if video_id and video_id in self.transcript_cache:
                transcript = self.transcript_cache[video_id]
                self.after(0, self._set_generation_status, generation, "Using cached transcript...")
            else:
                self.after(0, self._set_generation_status, generation, "Retrieving transcript...")
                transcript_result, error = fetch_transcript(url, self.settings.get("RAPIDAPI_KEY"))
                if generation != self.reset_generation:
                    return
                if error or not transcript_result:
                    self.after(0, self._finish_generation, generation, error or "Transcript is empty.", True)
                    return
                transcript = transcript_result
                if video_id:
                    self.transcript_cache[video_id] = transcript

        errors: list[str] = []
        generated: dict[str, str] = {}
        for task in ("titles", "summary", "chapters"):
            if generation != self.reset_generation:
                return
            self.after(0, self._set_generation_status, generation, f"Generating {task}...")
            result, error = self.client.generate(selection, task, transcript)
            if generation != self.reset_generation:
                return
            if result:
                generated[task] = titlecase_chapters(result) if task == "chapters" else result
            else:
                generated[task] = ""
            if error:
                errors.append(f"{task}: {error}")

        if errors:
            status = "Analysis finished with errors: " + " | ".join(errors)
        else:
            status = "Analysis complete."
        self.after(0, self._apply_analysis_results, generation, transcript, generated, status)

    def _apply_analysis_results(
        self,
        generation: int,
        transcript: str,
        generated: dict[str, str],
        status: str,
    ) -> None:
        if generation != self.reset_generation:
            return
        self.current_transcript = transcript
        self.results.update(generated)
        self._render_results()
        self._finish_analysis(status, False)

    def _finish_generation(self, generation: int, status: str, failed: bool) -> None:
        if generation == self.reset_generation:
            self._finish_analysis(status, failed)

    def _finish_analysis(self, status: str, failed: bool) -> None:
        self._set_status(status)
        self._set_busy(False)
        if failed:
            logger.warning("Analysis stopped: %s", status)

    def regenerate(self, task: str) -> None:
        if not self.current_transcript:
            self._set_status("Analyze a transcript before regenerating output.")
            return
        self._sync_editable_results()
        selection = self.model_combobox.get().strip()
        generation = self.reset_generation
        transcript = self.current_transcript
        self._set_status(f"Regenerating {task}...")
        threading.Thread(
            target=self._regenerate,
            args=(selection, task, transcript, generation),
            daemon=True,
        ).start()

    def _regenerate(self, selection: str, task: str, transcript: str, generation: int) -> None:
        result, error = self.client.generate(selection, task, transcript)
        if generation != self.reset_generation:
            return
        if error or not result:
            self.after(
                0,
                self._set_generation_status,
                generation,
                f"Could not regenerate {task}: {error or 'No output'}",
            )
            return
        self.after(0, self._apply_regenerated_result, generation, task, result)

    def _apply_regenerated_result(self, generation: int, task: str, result: str) -> None:
        if generation != self.reset_generation:
            return
        self.results[task] = titlecase_chapters(result) if task == "chapters" else result
        self._render_results()
        self._set_status(f"{task.capitalize()} regenerated.")

    def _sync_editable_results(self) -> None:
        """Preserve user edits when another result section is re-rendered."""
        for task in ("summary", "chapters"):
            frame = self.output_frames[task]
            children = frame.winfo_children()
            if children and isinstance(children[0], ctk.CTkTextbox):
                self.results[task] = children[0].get("1.0", tk.END).strip()

    def _render_results(self) -> None:
        titles_frame = self.output_frames["titles"]
        for widget in titles_frame.winfo_children():
            widget.destroy()
        titles = parse_titles(self.results["titles"])
        if titles:
            selected = self.title_var.get()
            self.title_var.set(selected if selected in titles else titles[0])
            for title in titles:
                ctk.CTkRadioButton(
                    titles_frame,
                    text=title,
                    variable=self.title_var,
                    value=title,
                ).pack(anchor="w", padx=10, pady=3)
        else:
            ctk.CTkLabel(titles_frame, text="No titles generated yet.").pack(padx=10, pady=12)

        for task in ("summary", "chapters"):
            frame = self.output_frames[task]
            for widget in frame.winfo_children():
                widget.destroy()
            value = self.results[task]
            if value:
                text = ctk.CTkTextbox(frame, height=225, corner_radius=CORNER_RADIUS)
                text.pack(fill="both", expand=True, padx=10, pady=10)
                text.insert("1.0", value)
                ContextMenu(text)
            else:
                ctk.CTkLabel(frame, text=f"No {task} generated yet.").pack(padx=10, pady=12)

    def copy_result(self, task: str) -> None:
        if task == "titles":
            content = self.title_var.get().strip()
        else:
            self._sync_editable_results()
            content = self.results.get(task, "").strip()
        if not content:
            self._set_status(f"No {task} available to copy.")
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self._set_status(f"{task.capitalize()} copied.")
        self.after(3000, self._set_status, "Ready")


def main() -> None:
    if "--self-test" in sys.argv or os.environ.get("YOUTUBE_ENHANCE_SELF_TEST") == "1":
        run_self_test()
        return
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    log_path = configure_logging()
    settings = SettingsStore()
    settings.load()
    logger.info("Application starting; settings=%s log=%s", settings.redacted_summary(), log_path)
    app = YouTubeEnhanceApp(settings)
    app.mainloop()


def run_self_test() -> None:
    """Verify packaged resources and encrypted settings without network calls."""
    for task in ("titles", "summary", "chapters"):
        system_prompt, _ = load_task_prompt(task)
        if not system_prompt:
            raise RuntimeError(f"Packaged prompt is empty: {task}")
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        store = SettingsStore(settings_path)
        store.load()
        store.save({"OPENAI_API_KEY": "self-test-secret"})
        reloaded = SettingsStore(settings_path)
        reloaded.load()
        if reloaded.get("OPENAI_API_KEY") != "self-test-secret":
            raise RuntimeError("Encrypted settings round-trip failed")


if __name__ == "__main__":
    main()
