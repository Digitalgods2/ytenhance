# Repository Guidelines

## Project Structure & Module Organization

The application is organized as focused Python modules at the repository root. `youtube_enhance.py` owns the CustomTkinter UI and workflow; `model_clients.py` contains provider clients; `transcripts.py` retrieves captions; `app_config.py` manages settings plus DPAPI/Keychain protection; and `prompt_loader.py` loads task prompts. Editable prompts live in the three `create_video_*/` directories. Offline tests are in `tests/test_core.py`; the signed macOS release workflow is `scripts/build_macos.sh`. Treat `build/`, `dist/`, caches, logs, and virtual environments as generated content.

## Build, Test, and Development Commands

Use Python 3.11 or newer:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python youtube_enhance.py
```

Run `python -m unittest discover -s tests -v` for the offline suite and `python youtube_enhance.py --self-test` for startup validation. Build Windows with `pyinstaller --clean youtube_enhance.spec`. On macOS, `bash scripts/build_macos.sh` creates, signs, notarizes, staples, and verifies the DMG; never run that workflow without the intended Developer ID identity.

## Coding Style & Naming Conventions

Follow PEP 8 with four-space indentation. Use `snake_case` for functions and variables, `PascalCase` for classes, and leading underscores for internal helpers. Preserve the existing type hints and small, responsibility-focused modules. No formatter or linter is configured, so match surrounding imports, spacing, and annotations. Keep provider-specific request parsing in `model_clients.py`, transcript logic in `transcripts.py`, and UI behavior in `youtube_enhance.py`.

## Testing Guidelines

Tests use the standard-library `unittest` framework. Name methods `test_<behavior>` and group related cases in `*Tests` classes. Add regression tests for bug fixes and cover success and failure paths for new behavior. Tests must remain offline: mock provider, YouTube, RapidAPI, filesystem, and clipboard boundaries where needed. There is no configured coverage threshold.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit prefixes such as `docs:` and `chore(security):`. Continue that pattern with concise, imperative subjects, for example `feat(ui): refine output cards`, and keep each commit focused. Pull requests should explain user-visible behavior, list verification commands, link relevant issues, and include screenshots for UI changes. Call out prompt or packaging changes explicitly.

## Security & Configuration

Never commit API keys, `.env` files, settings, logs, certificates, or notarization credentials. Preserve DPAPI/Keychain protection and error redaction. Avoid logging transcripts or credentials. If prompts or packaged assets change, update `youtube_enhance.spec` and test both platform targets.
