"""Persistent chat session management for Hirox."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from hirox.config import get_home_dir

_TITLE_TAG_RE = re.compile(r"<session_title>(.*?)</session_title>", re.DOTALL | re.IGNORECASE)


def get_sessions_dir() -> Path:
    """Return the directory where Hirox chat sessions are stored."""
    return get_home_dir() / "sessions"


def create_session(gateway: str, provider: str, model: str) -> dict[str, Any]:
    """Create a new in-memory session dict."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sid = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    return {
        "id": sid,
        "title": "New Session",
        "created_at": now,
        "updated_at": now,
        "gateway": gateway,
        "provider": provider,
        "model": model,
        "messages": [],
    }


def save_session(session: dict[str, Any]) -> Path:
    """Persist a session to ~/.hirox/sessions/<id>.json."""
    sdir = get_sessions_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = sdir / f"{session['id']}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(session, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_session(session_id: str) -> dict[str, Any]:
    """Load a session by ID."""
    path = get_sessions_dir() / f"{session_id}.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def list_sessions() -> list[dict[str, Any]]:
    """List all saved sessions sorted by updated_at descending."""
    sdir = get_sessions_dir()
    if not sdir.exists():
        return []
    sessions: list[dict[str, Any]] = []
    for path in sdir.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "id" in data:
                sessions.append(data)
        except Exception:
            continue
    sessions.sort(key=lambda s: str(s.get("updated_at", "")), reverse=True)
    return sessions


def extract_session_title(text: str) -> tuple[str, str | None]:
    """Extract <session_title>...</session_title> from model output."""
    title: str | None = None
    match = _TITLE_TAG_RE.search(text)
    if match:
        candidate = " ".join(match.group(1).strip().splitlines())
        if candidate:
            title = candidate[:60]
    cleaned = _TITLE_TAG_RE.sub("", text).strip()
    return cleaned, title
