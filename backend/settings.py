import os
import sys
import json

from backend.converter import DEFAULT_OLLAMA_PROMPT

APP_NAME = "AIDocPrep"

DEFAULTS = {
    "open_folder": True,
    "conflict": "keep_both",          # "keep_both" | "overwrite"
    "yaml": True,
    "toc": True,
    "output_mode": "both",            # "individual" | "both" | "combined_only"
    "zip": False,                     # opt-in: convert ZIP archives (filtered + capped)
    "redact": False,
    "redact_engine": "Regex Only",    # "Regex Only" | "Local NER (spaCy)" | "Local LLM (Ollama)"
    "ollama_model": "llama3",
    "custom_prompt": DEFAULT_OLLAMA_PROMPT,
    "custom_terms": "",
}


def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, APP_NAME)


def config_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def cache_dir() -> str:
    # Cache belongs in local (non-roaming) storage, unlike config_dir
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
        return os.path.join(base, APP_NAME, "Cache")
    elif sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Caches"), APP_NAME)
    else:
        base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
        return os.path.join(base, "aidocprep")


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            for key in DEFAULTS:
                if key in stored and isinstance(stored[key], type(DEFAULTS[key])):
                    settings[key] = stored[key]
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    try:
        os.makedirs(config_dir(), exist_ok=True)
        data = {key: settings[key] for key in DEFAULTS if key in settings}
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
