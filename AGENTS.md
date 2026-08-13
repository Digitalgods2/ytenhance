# Repository Guidelines

## Project Structure & Module Organization

The application is organized as focused Python modules at the repository root. `youtube_enhance.py` owns the CustomTkinter UI and workflow; `model_clients.py` contains OpenAI and Gemini HTTP clients; `transcripts.py` retrieves and normalizes captions; `app_config.py` manages per-user settings and Windows DPAPI secret protection; and `prompt_loader.py` loads task prompts. Editable prompt assets live in `create_video_titles/`, `create_video_summary/`, and `create_video_chapters/`, each with `system.md` and `user.md`. Offline tests are in `tests/test_core.py`. Treat `build/`, `dist/`, `__pycache__/`, logs, and virtual environments as generated content.

## Build, Test, and Development Commands

Use Python 3.11 or newer on Windows:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python youtube_enhance.py
```

Run `python -m unittest discover -s tests -v` for the complete offline suite. Use `python youtube_enhance.py --self-test` for a lightweight startup check. Build the standalone executable with `pyinstaller --clean youtube_enhance.spec`; output is written to `dist\YouTubeEnhance.exe`.

## Coding Style & Naming Conventions

Follow PEP 8 with four-space indentation. Use `snake_case` for functions and variables, `PascalCase` for classes, and leading underscores for internal helpers. Preserve the existing type hints and small, responsibility-focused modules. No formatter or linter is configured, so match surrounding imports, spacing, and annotations. Keep provider-specific request parsing in `model_clients.py`, transcript logic in `transcripts.py`, and UI behavior in `youtube_enhance.py`.

## Testing Guidelines

Tests use the standard-library `unittest` framework. Name methods `test_<behavior>` and group related cases in `*Tests` classes. Add regression tests for bug fixes and cover success and failure paths for new behavior. Tests must remain offline: mock provider, YouTube, RapidAPI, filesystem, and clipboard boundaries where needed. There is no configured coverage threshold.

## Commit & Pull Request Guidelines

History currently contains only `Initial release of YouTube Enhance`, so no formal convention is established. Use concise, imperative subjects such as `Add transcript retry handling`, and keep each commit focused. Pull requests should explain user-visible behavior, list verification commands, link relevant issues, and include screenshots for UI changes. Call out prompt or packaging changes explicitly.

## Security & Configuration

Never commit API keys, `.env` files, settings, or logs. Preserve DPAPI protection and error redaction when changing configuration or provider code. Avoid logging transcripts or credentials. If prompt directories change, update `youtube_enhance.spec` so packaged builds include them.
