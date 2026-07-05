# AI DocPrep — repo conventions

Rules for any coding agent working in this repository:

- **Never add AI attribution to git history.** No "Co-Authored-By: Claude/GPT/…", no "Generated with …" lines in commit messages, PR bodies, or release notes. Plain messages only. (A commit-msg hook also enforces this locally.)
- Python engine lives in `backend/` + `docprep_core.py` (headless CLI, JSON-lines events). Run tests with `.venv\Scripts\python tests\test_backend.py` and `tests\test_gui.py` (GUI test needs a display).
- The Tauri 2 + Svelte 5 desktop app lives in `desktop/`; the marketing site is static HTML in `site/` (deployed via Cloudflare Workers assets, `wrangler.jsonc`).
- Token savings are always shown as a single percentage, never absolute token counts.
