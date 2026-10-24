"""Loads configuration from config.yaml + .env."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

_BASE = Path(__file__).parent

# Not cwd-relative: keys load however the server is started
_DOTENV = dotenv_values(_BASE / ".env")


def _key(name: str) -> str:
    # A non-empty environment variable wins; an EMPTY one (e.g. left by a login tool)
    # must not hide the key in .env, which load_dotenv's no-override rule would do.
    return os.getenv(name) or _DOTENV.get(name) or ""


def load() -> dict[str, Any]:
    cfg_path = _BASE / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    # Inject API keys from environment
    cfg.setdefault("api_keys", {})
    cfg["api_keys"]["anthropic"] = _key("ANTHROPIC_API_KEY")
    cfg["api_keys"]["openai"] = _key("OPENAI_API_KEY")
    cfg["api_keys"]["google"] = _key("GOOGLE_API_KEY")
    cfg["api_keys"]["meta"] = _key("META_API_KEY")

    # Resolve relative paths to absolute
    paths = cfg.setdefault("paths", {})
    for key, rel in paths.items():
        paths[key] = str(_BASE / rel)

    return cfg
