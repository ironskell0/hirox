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
            "token_env": "HIROX_WEBHOOK_TOKEN",
            "token": "",
            "permission_mode": "read_only",
            "allowed_senders": [],
        },
        "telegram": {
            "enabled": False,
            "type": "telegram",
            "token_env": "HIROX_TELEGRAM_TOKEN",
            "token": "",
            "permission_mode": "read_only",
            "allowed_senders": [],
        },
        "discord": {
            "enabled": False,
            "type": "discord",
            "token_env": "HIROX_DISCORD_TOKEN",
            "token": "",
            "permission_mode": "read_only",
            "allowed_senders": [],
        },
        "slack": {
            "enabled": False,
            "type": "slack",
            "token_env": "HIROX_SLACK_TOKEN",
            "token": "",
            "permission_mode": "read_only",
            "allowed_senders": [],
        },
        "whatsapp": {
            "enabled": False,
            "type": "whatsapp",
            "token_env": "HIROX_WHATSAPP_TOKEN",
            "token": "",
            "permission_mode": "read_only",
            "allowed_senders": [],
        },
        "matrix": {
            "enabled": False,
            "type": "matrix",
            "token_env": "HIROX_MATRIX_TOKEN",
            "token": "",
            "permission_mode": "read_only",
            "allowed_senders": [],
        },
        "clickclack": {"enabled": False, "type": "clickclack", "token_env": "CLICKCLACK_TOKEN", "token": "", "url": "", "permission_mode": "read_only", "allowed_senders": []},
        "feishu": {"enabled": False, "type": "feishu", "token_env": "FEISHU_APP_SECRET", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "google_chat": {"enabled": False, "type": "google_chat", "token_env": "GOOGLE_CHAT_WEBHOOK_URL", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "imessage": {"enabled": False, "type": "imessage", "requires_token": False, "permission_mode": "read_only", "allowed_senders": []},
        "irc": {"enabled": False, "type": "irc", "server": "irc.libera.chat", "port": 6697, "tls": True, "nick": "hiroxbot", "channel": "#hirox", "token_env": "IRC_PASSWORD", "token": "", "requires_token": False, "permission_mode": "read_only", "allowed_senders": []},
        "line": {"enabled": False, "type": "line", "token_env": "LINE_CHANNEL_ACCESS_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "mattermost": {"enabled": False, "type": "mattermost", "url": "", "token_env": "MATTERMOST_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "teams": {"enabled": False, "type": "teams", "webhook_url": "", "token_env": "TEAMS_BOT_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "nextcloud_talk": {"enabled": False, "type": "nextcloud_talk", "url": "", "token_env": "NEXTCLOUD_TALK_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "nostr": {"enabled": False, "type": "nostr", "relay": "wss://relay.damus.io", "token_env": "NOSTR_NSEC", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "qq_bot": {"enabled": False, "type": "qq_bot", "token_env": "QQ_BOT_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "raft": {"enabled": False, "type": "raft", "command": "", "requires_token": False, "permission_mode": "read_only", "allowed_senders": []},
        "signal": {"enabled": False, "type": "signal", "account": "", "requires_token": False, "permission_mode": "read_only", "allowed_senders": []},
        "sms_twilio": {"enabled": False, "type": "sms_twilio", "account_sid": "", "from_number": "", "token_env": "TWILIO_AUTH_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "synology_chat": {"enabled": False, "type": "synology_chat", "incoming_url": "", "token_env": "SYNOLOGY_CHAT_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "tlon": {"enabled": False, "type": "tlon", "url": "http://127.0.0.1:8080", "token_env": "URBIT_CODE", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "twitch": {"enabled": False, "type": "twitch", "server": "irc.chat.twitch.tv", "port": 6697, "tls": True, "nick": "hiroxbot", "channel": "#hirox", "token_env": "TWITCH_OAUTH_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "wecom": {"enabled": False, "type": "wecom", "webhook_url": "", "token_env": "WECOM_SECRET", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "weixin": {"enabled": False, "type": "weixin", "bridge_url": "", "token_env": "WEIXIN_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "yuanbao": {"enabled": False, "type": "yuanbao", "bridge_url": "", "token_env": "YUANBAO_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "zalo_bot": {"enabled": False, "type": "zalo_bot", "token_env": "ZALO_BOT_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "zalo_personal": {"enabled": False, "type": "zalo_personal", "bridge_url": "", "token_env": "ZALO_PERSONAL_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
        "zalo_clawbot": {"enabled": False, "type": "zalo_clawbot", "bridge_url": "", "token_env": "ZALO_CLAWBOT_TOKEN", "token": "", "permission_mode": "read_only", "allowed_senders": []},
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
    """Persist configuration to disk with restricted 0600 permissions."""
    cfg_path = path or get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
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
