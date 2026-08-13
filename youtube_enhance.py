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
WINDOW_TITLE = "YOUTUBE ENHANCE"
CORNER_RADIUS = 5

# Light/dark tuples keep the reference's dark palette exact while preserving
# the existing Light and System appearance choices.
COLOR_BG = ("#F2F4F7", "#07090D")
COLOR_SHELL = ("#F8F9FB", "#0B0D12")
COLOR_CARD = ("#FFFFFF", "#0F1218")
COLOR_FIELD = ("#F5F6F8", "#12151C")
COLOR_BORDER = ("#D8DDE6", "#202630")
COLOR_TEXT = ("#171B23", "#E6E9F0")
COLOR_BODY = ("#343A46", "#CFD4DE")
COLOR_MUTED = ("#667085", "#8B93A3")
COLOR_SUBTLE = ("#8A94A6", "#5B6270")
COLOR_HOVER = ("#E7EAF0", "#191E26")
COLOR_MINT = "#7EF0C9"
COLOR_MINT_TEXT = "#6FE0BD"
COLOR_CYAN = "#6EE7F9"
COLOR_CYAN_TEXT = "#7FDCEE"
COLOR_PINK = "#FF9ECB"
COLOR_PINK_TEXT = "#F79FC6"
COLOR_VIOLET = "#B39CFF"
COLOR_VIOLET_TEXT = "#CDBEFF"
COLOR_YELLOW = "#FFD479"
COLOR_DARK_MINT = "#06120E"
COLOR_DARK_CYAN = "#04141A"
COLOR_DARK_PINK = "#1D0713"
COLOR_DARK_VIOLET = "#100A1F"

TASK_ACCENTS = {
    "titles": (COLOR_VIOLET, COLOR_VIOLET_TEXT, COLOR_DARK_VIOLET),
    "summary": (COLOR_CYAN, "#A5E9F7", COLOR_DARK_CYAN),
    "chapters": (COLOR_PINK, "#FFC2DD", COLOR_DARK_PINK),
}


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


def split_summary(raw: str | None) -> tuple[str, list[str]]:
    """Split a generated summary into readable prose and hashtag chips."""
    lines = (raw or "").strip().splitlines()
    tags: list[str] = []
    body_lines: list[str] = []
    for line in lines:
        found = re.findall(r"#[\w-]+", line)
        if found and not re.sub(r"#[\w-]+|[\s,]", "", line):
            tags.extend(found)
        else:
            body_lines.append(line)
    return "\n".join(body_lines).strip(), list(dict.fromkeys(tags))


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
        self.title(f"{WINDOW_TITLE} · SETTINGS")
        self.geometry("720x450")
        self.minsize(640, 420)
        self.configure(fg_color=COLOR_BG)
        self.transient(parent)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(
            self,
            fg_color=COLOR_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
            corner_radius=CORNER_RADIUS,
        )
        card.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            card,
            text="PROVIDER KEYS",
            text_color=COLOR_MINT_TEXT,
            font=ctk.CTkFont(family="Arial", size=13, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(20, 2))
        secure_storage_name = "macOS Keychain" if sys.platform == "darwin" else "your Windows account"
        ctk.CTkLabel(
            card,
            text=f"Credentials stay on this device and are protected by {secure_storage_name}.",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(family="Arial", size=12),
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 15))

        self.entries: dict[str, ctk.CTkEntry] = {}
        fields = [
            ("OPENAI_API_KEY", "OpenAI API key", "sk-..."),
            ("GEMINI_API_KEY", "Gemini API key", "AI..."),
            ("RAPIDAPI_KEY", "RapidAPI key (optional)", "Fallback transcript service"),
        ]
        for row, (key, label, placeholder) in enumerate(fields, start=2):
            ctk.CTkLabel(
                card,
                text=label,
                text_color=COLOR_BODY,
                font=ctk.CTkFont(family="Arial", size=12, weight="bold"),
            ).grid(row=row, column=0, sticky="w", padx=(20, 12), pady=9)
            entry = ctk.CTkEntry(
                card,
                show="•",
                placeholder_text=placeholder,
                corner_radius=CORNER_RADIUS,
                height=36,
                fg_color=COLOR_FIELD,
                border_color=COLOR_BORDER,
                text_color=COLOR_TEXT,
                placeholder_text_color=COLOR_SUBTLE,
            )
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 20), pady=9)
            current = self.store.get(key)
            if current:
                entry.insert(0, current)
            ContextMenu(entry)
            self.entries[key] = entry

        self.show_keys = ctk.CTkCheckBox(
            card,
            text="Show keys",
            command=self._toggle_keys,
            width=18,
            checkbox_width=17,
            checkbox_height=17,
            corner_radius=3,
            fg_color=COLOR_MINT,
            hover_color=COLOR_MINT_TEXT,
            border_color=COLOR_BORDER,
            text_color=COLOR_MUTED,
        )
        self.show_keys.grid(row=5, column=1, sticky="w", padx=0, pady=(3, 12))

        if sys.platform == "darwin":
            storage_text = "SECURE STORAGE\n~/Library/Application Support/YouTubeEnhance"
        else:
            storage_text = "SECURE STORAGE\n%LOCALAPPDATA%\\YouTubeEnhance\\settings.json"
        ctk.CTkLabel(
            card,
            text=storage_text,
            justify="left",
            anchor="w",
            text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Consolas", size=11),
            wraplength=650,
        ).grid(row=6, column=0, columnspan=2, sticky="ew", padx=20, pady=(4, 15))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", padx=20, pady=(0, 20))
        ctk.CTkButton(
            buttons,
            text="Cancel",
            width=86,
            height=34,
            command=self.destroy,
            fg_color=COLOR_FIELD,
            hover_color=COLOR_HOVER,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_MUTED,
            corner_radius=CORNER_RADIUS,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            buttons,
            text="Save securely",
            width=116,
            height=34,
            command=self._save,
            fg_color=COLOR_MINT,
            hover_color=COLOR_MINT_TEXT,
            text_color=COLOR_DARK_MINT,
            corner_radius=CORNER_RADIUS,
            font=ctk.CTkFont(family="Arial", size=12, weight="bold"),
        ).pack(side="right")

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
        self.summary_tags: list[str] = []
        self.title_var = tk.StringVar()
        self.reset_generation = 0

        self.title(WINDOW_TITLE)
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        window_width = max(1100, min(1440, screen_width - 40))
        window_height = max(720, min(820, screen_height - 120))
        self.geometry(f"{window_width}x{window_height}")
        self.minsize(1100, 720)
        self.configure(fg_color=COLOR_BG)
        self.grid_columnconfigure(0, weight=31, minsize=370)
        self.grid_columnconfigure(1, weight=69, minsize=650)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_input_panel()
        self._build_output_panel()
        self._build_status_bar()
        self._update_transcript_meta("")
        self._refresh_key_status()
        self._refresh_footer_meta()
        self.load_models_async()

    def _utility_button(
        self,
        parent: tk.Misc,
        text: str,
        accent: str,
        command: object,
        *,
        solid: bool = False,
        dark_text: str = COLOR_DARK_MINT,
        width: int = 68,
        height: int = 30,
    ) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            width=width,
            height=height,
            command=command,
            corner_radius=CORNER_RADIUS,
            fg_color=accent if solid else COLOR_FIELD,
            hover_color=accent if not solid else COLOR_MINT_TEXT,
            border_width=0 if solid else 1,
            border_color=accent,
            text_color=dark_text if solid else accent,
            font=ctk.CTkFont(family="Arial", size=12, weight="bold"),
        )

    @staticmethod
    def _divider(parent: tk.Misc) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, height=1, fg_color=COLOR_BORDER, corner_radius=0)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(
            self,
            height=76,
            corner_radius=0,
            fg_color=COLOR_SHELL,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        logo = ctk.CTkFrame(
            header,
            width=38,
            height=38,
            fg_color=COLOR_FIELD,
            border_width=1,
            border_color=COLOR_MINT_TEXT,
            corner_radius=CORNER_RADIUS,
        )
        logo.grid(row=0, column=0, padx=(18, 12), pady=17)
        logo.grid_propagate(False)
        ctk.CTkLabel(
            logo,
            text="YE",
            text_color=COLOR_MINT,
            font=ctk.CTkFont(family="Arial", size=14, weight="bold"),
        ).place(relx=0.5, rely=0.5, anchor="center")

        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            title_group,
            text="YouTube Enhance",
            text_color=COLOR_TEXT,
            font=ctk.CTkFont(family="Arial", size=20, weight="bold"),
        ).pack(anchor="w")
        subtitle = ctk.CTkFrame(title_group, fg_color="transparent")
        subtitle.pack(anchor="w")
        ctk.CTkLabel(
            subtitle,
            text="Direct OpenAI and Gemini analysis",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(family="Arial", size=12),
        ).pack(side="left")
        ctk.CTkLabel(
            subtitle,
            text="  |  local · no telemetry",
            text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Consolas", size=11),
        ).pack(side="left")

        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.grid(row=0, column=2, padx=18, pady=17, sticky="e")
        self.theme_menu = ctk.CTkSegmentedButton(
            header_actions,
            values=["Dark", "Light", "System"],
            command=self._set_appearance_mode,
            height=34,
            corner_radius=CORNER_RADIUS,
            fg_color=COLOR_FIELD,
            selected_color=("#E0DAFA", "#28243A"),
            selected_hover_color=("#D7CEFA", "#322B49"),
            unselected_color=COLOR_FIELD,
            unselected_hover_color=COLOR_HOVER,
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(family="Arial", size=12),
        )
        self.theme_menu.pack(side="left", padx=(0, 10))
        self.theme_menu.set("Dark")
        self._utility_button(
            header_actions,
            "Clear All",
            COLOR_PINK_TEXT,
            self.clear_all,
            width=88,
            height=34,
        ).pack(side="left", padx=(0, 10))
        self._utility_button(
            header_actions,
            "Settings",
            COLOR_VIOLET_TEXT,
            self.open_settings,
            width=88,
            height=34,
        ).pack(side="left")

    def _set_appearance_mode(self, value: str) -> None:
        self._sync_editable_results()
        ctk.set_appearance_mode(value)
        self._render_results()

    def _on_model_selected(self, value: str) -> None:
        selection = (value or "").strip()
        if selection and selection != "Loading...":
            self.settings.update({"LAST_MODEL": selection})
        self._refresh_footer_meta()

    def _build_input_panel(self) -> None:
        left = ctk.CTkFrame(
            self,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color=COLOR_CARD,
            corner_radius=CORNER_RADIUS,
        )
        left.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=14)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(9, weight=1)

        model_header = ctk.CTkFrame(left, fg_color="transparent")
        model_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 7))
        model_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            model_header,
            text="MODEL",
            text_color=COLOR_MINT_TEXT,
            font=ctk.CTkFont(family="Arial", size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        key_status = ctk.CTkFrame(model_header, fg_color="transparent", corner_radius=0)
        key_status.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            key_status,
            text="■",
            width=12,
            text_color=COLOR_MINT,
            font=ctk.CTkFont(size=8),
        ).pack(side="left", padx=(0, 3))
        self.key_status_label = ctk.CTkLabel(
            key_status,
            text="KEYS 0/3",
            text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Consolas", size=10),
        )
        self.key_status_label.pack(side="left")

        self.model_combobox = ctk.CTkComboBox(
            left,
            values=["Loading..."],
            state="disabled",
            command=self._on_model_selected,
            corner_radius=CORNER_RADIUS,
            height=40,
            fg_color=COLOR_FIELD,
            border_color=COLOR_BORDER,
            button_color=COLOR_FIELD,
            button_hover_color=COLOR_HOVER,
            dropdown_fg_color=COLOR_CARD,
            dropdown_hover_color=COLOR_HOVER,
            text_color=COLOR_TEXT,
            dropdown_text_color=COLOR_TEXT,
            font=ctk.CTkFont(family="Arial", size=13),
            dropdown_font=ctk.CTkFont(family="Arial", size=13),
        )
        self.model_combobox.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))

        self.full_list_var = tk.BooleanVar(value=False)
        self.full_list_checkbox = ctk.CTkCheckBox(
            left,
            text="Load full model list",
            variable=self.full_list_var,
            command=self.load_models_async,
            corner_radius=3,
            checkbox_width=17,
            checkbox_height=17,
            border_color=COLOR_BORDER,
            fg_color=COLOR_VIOLET,
            hover_color=COLOR_VIOLET_TEXT,
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(family="Arial", size=12),
        )
        self.full_list_checkbox.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 13))

        self._divider(left).grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 13))

        url_header = ctk.CTkFrame(left, fg_color="transparent")
        url_header.grid(row=4, column=0, sticky="ew", padx=16)
        ctk.CTkLabel(
            url_header,
            text="SOURCE URL",
            text_color=COLOR_CYAN_TEXT,
            font=ctk.CTkFont(family="Arial", size=12, weight="bold"),
        ).pack(side="left")
        self._utility_button(url_header, "Clear", COLOR_PINK_TEXT, self.clear_url, width=58, height=28).pack(side="right")
        self._utility_button(url_header, "Paste", COLOR_CYAN_TEXT, self.paste_url, width=58, height=28).pack(side="right", padx=(0, 7))
        self.url_entry = ctk.CTkEntry(
            left,
            placeholder_text="https://www.youtube.com/watch?v=...",
            corner_radius=CORNER_RADIUS,
            height=40,
            fg_color=COLOR_FIELD,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            placeholder_text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.url_entry.grid(row=5, column=0, sticky="ew", padx=16, pady=(7, 13))
        ContextMenu(self.url_entry)

        self._divider(left).grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 13))

        transcript_header = ctk.CTkFrame(left, fg_color="transparent")
        transcript_header.grid(row=7, column=0, sticky="ew", padx=16)
        ctk.CTkLabel(
            transcript_header,
            text="TRANSCRIPT",
            text_color=COLOR_YELLOW,
            font=ctk.CTkFont(family="Arial", size=12, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            transcript_header,
            text="  OPTIONAL",
            text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Arial", size=10),
        ).pack(side="left")
        self._utility_button(transcript_header, "Clear", COLOR_PINK_TEXT, self.clear_transcript, width=58, height=28).pack(side="right")
        self._utility_button(transcript_header, "Copy", COLOR_MINT_TEXT, self.copy_transcript, width=58, height=28).pack(side="right", padx=(0, 7))
        self._utility_button(transcript_header, "Paste", COLOR_CYAN_TEXT, self.paste_transcript, width=58, height=28).pack(side="right", padx=(0, 7))
        ctk.CTkLabel(
            left,
            text="A pasted transcript takes precedence over automatic retrieval.",
            text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Arial", size=11),
        ).grid(row=8, column=0, sticky="w", padx=16, pady=(5, 6))
        self.transcript_box = ctk.CTkTextbox(
            left,
            height=250,
            corner_radius=CORNER_RADIUS,
            fg_color=COLOR_FIELD,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_BODY,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self.transcript_box.grid(row=9, column=0, sticky="nsew", padx=16)
        ContextMenu(self.transcript_box)

        self.transcript_meta_label = ctk.CTkLabel(
            left,
            text="0 lines · 0 chars",
            text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Consolas", size=10),
            anchor="w",
        )
        self.transcript_meta_label.grid(row=10, column=0, sticky="ew", padx=16, pady=(6, 10))

        self.analyze_button = ctk.CTkButton(
            left,
            text="Analyze transcript",
            height=44,
            corner_radius=CORNER_RADIUS,
            fg_color=COLOR_MINT,
            hover_color=COLOR_MINT_TEXT,
            text_color=COLOR_DARK_MINT,
            font=ctk.CTkFont(family="Arial", size=14, weight="bold"),
            command=self.analyze_async,
        )
        self.analyze_button.grid(row=11, column=0, sticky="ew", padx=16)
        ctk.CTkLabel(
            left,
            text="Titles · 5     Summary · 2¶ + 8 tags     Chapters · 15–25",
            text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Arial", size=10),
        ).grid(row=12, column=0, sticky="w", padx=16, pady=(7, 14))
        self.progress_bar = ctk.CTkProgressBar(
            left,
            orientation="horizontal",
            mode="indeterminate",
            corner_radius=2,
            height=3,
            fg_color=COLOR_FIELD,
            progress_color=COLOR_MINT,
        )

    def _build_output_panel(self) -> None:
        right = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self.output_panel = right
        right.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=14)
        right.grid_columnconfigure(0, weight=1)
        right.grid_columnconfigure(1, weight=1)
        right.grid_rowconfigure(0, weight=0, minsize=250)
        right.grid_rowconfigure(1, weight=1)

        self.output_frames: dict[str, ctk.CTkFrame] = {}
        self.output_badges: dict[str, ctk.CTkLabel] = {}
        self._build_output_card(right, "titles", "Titles", row=0, column=0, columnspan=2, pady=(0, 7))
        self._build_output_card(right, "summary", "Summary", row=1, column=0, padx=(0, 7), pady=(7, 0))
        self._build_output_card(
            right,
            "chapters",
            "Chapters",
            row=1,
            column=1,
            padx=(7, 0),
            pady=(7, 0),
            sticky="new",
        )

        self._render_results()

    def _build_output_card(
        self,
        parent: ctk.CTkFrame,
        task: str,
        label: str,
        *,
        row: int,
        column: int,
        columnspan: int = 1,
        padx: tuple[int, int] | int = 0,
        pady: tuple[int, int] | int = 0,
        sticky: str = "nsew",
    ) -> None:
        accent, title_color, dark_text = TASK_ACCENTS[task]
        card = ctk.CTkFrame(
            parent,
            fg_color=COLOR_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
            corner_radius=CORNER_RADIUS,
        )
        card.grid(row=row, column=column, columnspan=columnspan, sticky=sticky, padx=padx, pady=pady)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text="■", text_color=accent, font=ctk.CTkFont(size=10)).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkLabel(
            header,
            text=label,
            text_color=title_color,
            font=ctk.CTkFont(family="Arial", size=15, weight="bold"),
        ).grid(row=0, column=1, sticky="w")
        badge = ctk.CTkLabel(
            header,
            text="0",
            width=46,
            height=22,
            fg_color=COLOR_FIELD,
            text_color=COLOR_SUBTLE,
            corner_radius=3,
            font=ctk.CTkFont(family="Consolas", size=10),
        )
        badge.grid(row=0, column=2, padx=(10, 0))
        if task == "summary":
            badge.grid_remove()
        self.output_badges[task] = badge

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.grid(row=0, column=3, sticky="e", padx=(16, 0))
        self._utility_button(
            actions,
            "Regen",
            accent,
            lambda current=task: self.regenerate(current),
            width=66,
            height=30,
        ).pack(side="left", padx=(0, 7))
        self._utility_button(
            actions,
            "Copy",
            accent,
            lambda current=task: self.copy_result(current),
            solid=True,
            dark_text=dark_text,
            width=66,
            height=30,
        ).pack(side="left")

        self._divider(card).grid(row=1, column=0, sticky="ew")
        body = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        body.grid(row=2, column=0, sticky="nsew")
        self.output_frames[task] = body

    def _build_status_bar(self) -> None:
        status = ctk.CTkFrame(
            self,
            height=36,
            corner_radius=0,
            fg_color=COLOR_SHELL,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        status.grid(row=2, column=0, columnspan=2, sticky="ew")
        status.grid_columnconfigure(1, weight=1)
        self.status_dot = ctk.CTkLabel(status, text="■", width=12, text_color=COLOR_MINT, font=ctk.CTkFont(size=9))
        self.status_dot.grid(row=0, column=0, padx=(16, 7), pady=6)
        self.status_label = ctk.CTkLabel(
            status,
            text="Ready",
            anchor="w",
            text_color=COLOR_BODY,
            font=ctk.CTkFont(family="Arial", size=11),
        )
        self.status_label.grid(row=0, column=1, sticky="ew", pady=6)
        self.footer_meta_label = ctk.CTkLabel(
            status,
            text="",
            anchor="e",
            text_color=COLOR_SUBTLE,
            font=ctk.CTkFont(family="Consolas", size=10),
        )
        self.footer_meta_label.grid(row=0, column=2, sticky="e", padx=(12, 16), pady=6)

    def _set_status(self, message: str) -> None:
        normalized = " ".join(str(message or "").split())
        display = normalized if len(normalized) <= 160 else normalized[:157] + "..."
        self.status_label.configure(text=display or "Ready")
        failed = any(word in normalized.lower() for word in ("error", "failed", "could not", "invalid"))
        self.status_dot.configure(text_color=COLOR_PINK if failed else COLOR_MINT)

    def _refresh_key_status(self) -> None:
        keys = ("OPENAI_API_KEY", "GEMINI_API_KEY", "RAPIDAPI_KEY")
        configured = sum(bool(self.settings.get(key)) for key in keys)
        self.key_status_label.configure(text=f"KEYS {configured}/3")

    def _refresh_footer_meta(self) -> None:
        selection = ""
        if hasattr(self, "model_combobox"):
            selection = self.model_combobox.get().strip()
        if not selection or selection == "Loading...":
            selection = self.settings.get("LAST_MODEL") or "OpenAI · model"
        storage = (
            "~/Library/Application Support/YouTubeEnhance"
            if sys.platform == "darwin"
            else "%LOCALAPPDATA%\\YouTubeEnhance"
        )
        self.footer_meta_label.configure(text=f"{selection}   |   store: false   |   {storage}")

    def _update_transcript_meta(self, text: str) -> None:
        cleaned = (text or "").strip()
        line_count = len(cleaned.splitlines()) if cleaned else 0
        meta = f"{line_count} lines · {len(cleaned)} chars"
        if hasattr(self, "url_entry"):
            video_id = extract_video_id(self.url_entry.get().strip())
            if video_id and video_id in self.transcript_cache:
                meta += f"     cache: {video_id}"
        self.transcript_meta_label.configure(text=meta)

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
        self._update_transcript_meta(self.transcript_box.get("1.0", tk.END))
        self._set_status("URL pasted.")

    def clear_url(self) -> None:
        self.url_entry.delete(0, tk.END)
        self.url_entry.focus_set()
        self._update_transcript_meta(self.transcript_box.get("1.0", tk.END))
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
        self._update_transcript_meta(text)
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
        self._update_transcript_meta("")
        self._set_status("Transcript cleared.")

    def clear_all(self) -> None:
        """Restore the transient UI state while preserving saved settings."""
        self.reset_generation += 1
        self.current_transcript = ""
        self.transcript_cache.clear()
        self.results = {"titles": "", "summary": "", "chapters": ""}
        self.summary_tags = []
        self.title_var.set("")

        self.url_entry.delete(0, tk.END)
        self.transcript_box.delete("1.0", tk.END)
        self.full_list_var.set(False)
        self._set_busy(False)
        self._render_results()
        self._update_transcript_meta("")

        ctk.set_appearance_mode("Dark")
        self.theme_menu.set("Dark")
        self.url_entry.focus_set()
        self._set_status("Ready")
        self.load_models_async()

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.analyze_button.configure(text="Analyzing...", state="disabled")
            self.progress_bar.grid(row=13, column=0, sticky="ew", padx=16, pady=(0, 10))
            self.progress_bar.start()
        else:
            self.progress_bar.stop()
            self.progress_bar.grid_forget()
            self.analyze_button.configure(text="Analyze transcript", state="normal")

    def open_settings(self) -> None:
        SettingsDialog(self)

    def on_settings_saved(self) -> None:
        self._refresh_key_status()
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
        self._refresh_footer_meta()
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
            returned = sum(bool(generated.get(task)) for task in ("titles", "summary", "chapters"))
            status = f"Analysis complete. {returned} of 3 tasks returned output. " + " | ".join(errors)
        else:
            status = "Analysis complete. 3 of 3 tasks returned output."
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
        self.transcript_box.delete("1.0", tk.END)
        self.transcript_box.insert("1.0", transcript)
        self._update_transcript_meta(transcript)
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
                value = children[0].get("1.0", tk.END).strip()
                if task == "summary" and self.summary_tags:
                    value = f"{value}\n\n{','.join(self.summary_tags)}".strip()
                self.results[task] = value

    def _render_results(self) -> None:
        titles_frame = self.output_frames["titles"]
        for widget in titles_frame.winfo_children():
            widget.destroy()
        titles = parse_titles(self.results["titles"])
        self.output_badges["titles"].configure(text=f"{len(titles)} OF 5")
        if titles:
            selected = self.title_var.get()
            self.title_var.set(selected if selected in titles else titles[0])
            title_labels: list[ctk.CTkLabel] = []
            for index, title in enumerate(titles, start=1):
                row = ctk.CTkFrame(titles_frame, fg_color="transparent", corner_radius=0)
                row.pack(fill="x", padx=16, pady=4)
                radio = ctk.CTkRadioButton(
                    row,
                    text="",
                    variable=self.title_var,
                    value=title,
                    width=20,
                    height=20,
                    radiobutton_width=16,
                    radiobutton_height=16,
                    border_width_unchecked=1,
                    border_width_checked=4,
                    border_color=COLOR_BORDER,
                    hover_color=COLOR_VIOLET,
                    fg_color=COLOR_VIOLET,
                )
                radio.pack(side="left")
                number = ctk.CTkLabel(
                    row,
                    text=f"{index:02d}",
                    width=30,
                    text_color=COLOR_SUBTLE,
                    font=ctk.CTkFont(family="Consolas", size=10),
                )
                number.pack(side="left", padx=(3, 7))
                title_label = ctk.CTkLabel(
                    row,
                    text=title,
                    anchor="w",
                    justify="left",
                    text_color=COLOR_BODY,
                    font=ctk.CTkFont(family="Arial", size=13),
                )
                title_label.pack(side="left", fill="x", expand=True)
                title_labels.append(title_label)
                for clickable in (number, title_label):
                    clickable.bind("<Button-1>", lambda _event, value=title: self.title_var.set(value))

            def resize_titles(event: tk.Event) -> None:
                wrap = max(260, int(event.width) - 92)
                for label in title_labels:
                    label.configure(wraplength=wrap)

            titles_frame.bind("<Configure>", resize_titles, add="+")
        else:
            ctk.CTkLabel(
                titles_frame,
                text="No titles generated yet.",
                text_color=COLOR_SUBTLE,
                font=ctk.CTkFont(family="Arial", size=12),
            ).pack(padx=16, pady=18, anchor="w")

        summary_frame = self.output_frames["summary"]
        for widget in summary_frame.winfo_children():
            widget.destroy()
        summary, tags = split_summary(self.results["summary"])
        self.summary_tags = tags
        if summary or tags:
            summary_text = ctk.CTkTextbox(
                summary_frame,
                height=190,
                corner_radius=0,
                fg_color=COLOR_CARD,
                border_width=0,
                text_color=COLOR_BODY,
                font=ctk.CTkFont(family="Arial", size=13),
                wrap="word",
                activate_scrollbars=True,
            )
            summary_text.pack(fill="both", expand=True, padx=10, pady=(9, 4))
            summary_text.insert("1.0", summary)
            ContextMenu(summary_text)
            if tags:
                tag_area = ctk.CTkFrame(summary_frame, fg_color="transparent", corner_radius=0)
                tag_area.pack(fill="x", padx=14, pady=(2, 12))
                for start in range(0, len(tags), 5):
                    tag_row = ctk.CTkFrame(tag_area, fg_color="transparent", corner_radius=0)
                    tag_row.pack(fill="x", pady=(0, 5))
                    for tag in tags[start:start + 5]:
                        ctk.CTkLabel(
                            tag_row,
                            text=tag,
                            height=25,
                            fg_color=("#E5F8F3", "#162123"),
                            text_color=("#18765F", "#A5F3E4"),
                            corner_radius=3,
                            font=ctk.CTkFont(family="Consolas", size=10),
                        ).pack(side="left", padx=(0, 6))
        else:
            ctk.CTkLabel(
                summary_frame,
                text="No summary generated yet.",
                text_color=COLOR_SUBTLE,
                font=ctk.CTkFont(family="Arial", size=12),
            ).pack(padx=16, pady=18, anchor="w")

        chapters_frame = self.output_frames["chapters"]
        for widget in chapters_frame.winfo_children():
            widget.destroy()
        chapters = self.results["chapters"].strip()
        chapter_lines = [line for line in chapters.splitlines() if line.strip()]
        self.output_badges["chapters"].configure(text=f"{len(chapter_lines)} CH")
        if chapters:
            chapter_text = ctk.CTkTextbox(
                chapters_frame,
                height=250,
                corner_radius=0,
                fg_color=COLOR_CARD,
                border_width=0,
                text_color=COLOR_BODY,
                font=ctk.CTkFont(family="Arial", size=12),
                wrap="word",
            )
            chapter_text.pack(fill="both", expand=True, padx=10, pady=9)
            native = chapter_text._textbox
            native.tag_configure("timestamp", foreground=COLOR_YELLOW, font=("Consolas", 11))
            native.tag_configure("chapter", foreground=COLOR_BODY[1] if ctk.get_appearance_mode() == "Dark" else COLOR_BODY[0])
            for index, line in enumerate(chapter_lines):
                match = re.match(r"^(\s*\[?\d{1,2}:\d{2}(?::\d{2})?\]?)(\s+)(.*)$", line)
                if match:
                    native.insert(tk.END, match.group(1), ("timestamp",))
                    native.insert(tk.END, match.group(2) + match.group(3), ("chapter",))
                else:
                    native.insert(tk.END, line, ("chapter",))
                if index < len(chapter_lines) - 1:
                    native.insert(tk.END, "\n")
            ContextMenu(chapter_text)
        else:
            ctk.CTkLabel(
                chapters_frame,
                text="No chapters generated yet.",
                text_color=COLOR_SUBTLE,
                font=ctk.CTkFont(family="Arial", size=12),
            ).pack(padx=16, pady=18, anchor="w")

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
    if sys.platform == "darwin":
        import keyring

        if keyring.get_keyring().priority < 1:
            raise RuntimeError("A supported macOS Keychain backend is unavailable")
        return
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
