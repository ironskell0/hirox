"""Configuration management for Hirox."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from hirox.providers import default_providers

DEFAULT_CONFIG: dict[str, Any] = {
    "agent": {
        "name": "hirox",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "permission_mode": "ask",
        "system_prompt": "You are Hirox, a helpful multi-gateway AI agent.",
    },
    "runtime": {
        "host": "127.0.0.1",
        "port": 8765,
        "log_level": "INFO",
    },
    "providers": default_providers(),
    "gateways": {
        "cli": {
            "enabled": True,
            "type": "cli",
        },
        "webhook": {
            "enabled": False,
            "type": "webhook",
            "path": "/webhook",
        },
        "telegram": {
            "enabled": False,
            "type": "telegram",
            "token_env": "HIROX_TELEGRAM_TOKEN",
            "token": "",
        },
        "discord": {
            "enabled": False,
            "type": "discord",
            "token_env": "HIROX_DISCORD_TOKEN",
            "token": "",
        },
        "slack": {
            "enabled": False,
            "type": "slack",
            "token_env": "HIROX_SLACK_TOKEN",
            "token": "",
        },
        "whatsapp": {
            "enabled": False,
            "type": "whatsapp",
            "token_env": "HIROX_WHATSAPP_TOKEN",
            "token": "",
        },
        "matrix": {
            "enabled": False,
            "type": "matrix",
            "token_env": "HIROX_MATRIX_TOKEN",
            "token": "",
        },
    },
}


def get_home_dir() -> Path:
    """Return the Hirox home directory."""
    env_home = os.environ.get("HIROX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".hirox"


def get_config_path() -> Path:
    """Return the active Hirox config file path."""
    env_cfg = os.environ.get("HIROX_CONFIG")
    if env_cfg:
        return Path(env_cfg).expanduser()
    return get_home_dir() / "config.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load configuration from disk, merged with defaults."""
    cfg_path = path or get_config_path()
    if not cfg_path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    with cfg_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid config format in {cfg_path}: expected JSON object")
    return _deep_merge(DEFAULT_CONFIG, data)


def save_config(config: dict[str, Any], path: Path | None = None) -> Path:
    """Persist configuration to disk."""
    cfg_path = path or get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return cfg_path


def init_config(force: bool = False, path: Path | None = None) -> tuple[Path, bool]:
    """Initialize default config file if missing (or if force=True).

    Returns (path, created_or_overwritten).
    """
    cfg_path = path or get_config_path()
    if cfg_path.exists() and not force:
        return cfg_path, False
    save_config(copy.deepcopy(DEFAULT_CONFIG), cfg_path)
    return cfg_path, True


def get_value(key: str, path: Path | None = None) -> Any:
    """Get a dotted config key (e.g. 'runtime.port')."""
    cfg = load_config(path)
    current: Any = cfg
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(key)
        current = current[part]
    return current


def _parse_scalar(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def set_value(key: str, raw_value: str, path: Path | None = None) -> tuple[Path, Any]:
    """Set a dotted config key and save to disk."""
    cfg = load_config(path)
    parts = key.split(".")
    current: dict[str, Any] = cfg
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    parsed = _parse_scalar(raw_value)
    current[parts[-1]] = parsed
    saved_path = save_config(cfg, path)
    return saved_path, parsed
