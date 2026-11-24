"""Persistent CLAUDE.md-style memory and dynamic system prompt builder for Hirox."""

from __future__ import annotations

import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path

from hirox.config import get_home_dir

_MEMORY_TAG_RE = re.compile(r"<memory_write>(.*?)</memory_write>", re.DOTALL | re.IGNORECASE)

_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bssh-(rsa|ed25519|ecdsa)\b", re.IGNORECASE),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"\b(password|parola|sifre|şifre|api[_ -]?key|secret[_ -]?key|private[_ -]?key|access[_ -]?token)\s*[:=]\s*\S+", re.IGNORECASE),
)

DEFAULT_SYSTEM_PROMPT = """You are Hirox, an autonomous, sharp, multi-gateway AI agent.

Core Principles:
- Be direct, accurate, and practical. Never fabricate tool outputs, file paths, or facts.
- Adapt to the user's language naturally (e.g., Turkish or English).
- You operate across multiple gateways (CLI, Webhook, Telegram, Discord, Slack, WhatsApp, Matrix). Keep responses clean and readable for the active gateway.
- Use the Live Runtime Context provided below (time, OS, CPU architecture, device, working directory, permission mode) whenever relevant.

Tools & Capabilities:
- You have real local tools you can call when needed:
  - `list_dir(path)`: Browse and list files/directories.
  - `read_file(path, start_line, limit)`: Read file contents.
  - `write_file(path, content)`: Create or update files.
  - `run_command(command)`: Execute shell commands in the terminal.
  - `web_search(query, max_results)`: Search the web via DuckDuckGo for up-to-date information.
  - `web_fetch(url, max_chars)`: Fetch and read text content from a web URL.
  - `workflow_plan(action, goal, steps, step_index, status, note)`: Create and track a multi-step autonomous workflow plan.

Autonomous Agent Workflow:
- You are an autonomous agent capable of chaining multiple tool calls continuously until a task is fully solved.
- For multi-step or complex tasks:
  1. Start by calling `workflow_plan(action="set", goal=..., steps=[...])` to break the task into clear steps.
  2. Execute each step sequentially using your tools (`list_dir`, `read_file`, `write_file`, `run_command`, `web_search`, `web_fetch`) without stopping to ask the user between routine steps.
  3. Update progress with `workflow_plan(action="update", step_index=..., status="completed")` as you finish each step.
  4. Verify your work (e.g. run tests or check outputs) before giving the final answer.
- Respect the active `Permission Mode` shown in Live Runtime Context:
  - `read_only`: You can only browse (`list_dir`) and read (`read_file`). Do not attempt writes or shell commands.
  - `ask`: You can read freely; `write_file` and `run_command` will prompt the user for confirmation.
  - `full_access`: Unrestricted tool execution.

Persistent Memory (MEMORY.md) - STRICT RULES:
- You have a persistent brain stored in MEMORY.md, injected into your context on every turn.
- Use <memory_write>...</memory_write> SPARINGLY. Most messages do NOT need a memory write.
- ONLY save durable, high-value facts: explicit user preferences ("always reply in Turkish"), long-term project architecture/stack rules, or facts the user explicitly asks you to remember.
- NEVER save:
  1. Trivial chat, greetings, one-off questions, or temporary debugging steps.
  2. Secrets or credentials: SSH keys, private keys, API keys, tokens, passwords, or auth headers.
  3. Sensitive personal info (PII): phone numbers, home addresses, ID numbers, emails, or financial details.
- If in doubt, DO NOT write to memory."""


def get_memory_path() -> Path:
    """Return the global Hirox MEMORY.md path."""
    return get_home_dir() / "MEMORY.md"


def load_memory() -> str:
    """Load global ~/.hirox/MEMORY.md and optional local ./HIROX.md."""
    sections: list[str] = []
    global_mem = get_memory_path()
    if global_mem.exists():
        content = global_mem.read_text(encoding="utf-8").strip()
        if content:
            sections.append(f"### Global Memory ({global_mem})\n{content}")

    local_mem = Path.cwd() / "HIROX.md"
    if local_mem.exists() and local_mem.resolve() != global_mem.resolve():
        content = local_mem.read_text(encoding="utf-8").strip()
        if content:
            sections.append(f"### Project Memory ({local_mem})\n{content}")

    return "\n\n".join(sections)


def append_memory(entry: str) -> Path:
    """Append a timestamped memory entry to ~/.hirox/MEMORY.md."""
    mem_path = get_memory_path()
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    if not mem_path.exists():
        mem_path.write_text("# Hirox Memory\n\n", encoding="utf-8")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    clean_entry = " ".join(entry.strip().splitlines())
    with mem_path.open("a", encoding="utf-8") as f:
        f.write(f"- [{stamp}] {clean_entry}\n")
    return mem_path


def clear_memory() -> Path:
    """Reset ~/.hirox/MEMORY.md."""
    mem_path = get_memory_path()
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    mem_path.write_text("# Hirox Memory\n\n", encoding="utf-8")
    return mem_path


def is_sensitive_memory(entry: str) -> bool:
    """Return True if entry appears to contain secrets, SSH keys, or credentials."""
    return any(pat.search(entry) for pat in _SENSITIVE_PATTERNS)


def extract_memory_writes(text: str) -> tuple[str, list[str]]:
    """Extract <memory_write>...</memory_write> tags from model output.

    Filters out sensitive secrets/keys. Returns (cleaned_text, list_of_entries).
    """
    entries: list[str] = []
    for match in _MEMORY_TAG_RE.finditer(text):
        item = match.group(1).strip()
        if item and not is_sensitive_memory(item):
            entries.append(item)
    cleaned = _MEMORY_TAG_RE.sub("", text).strip()
    return cleaned, entries


def get_runtime_context(
    gateway: str,
    provider_id: str,
    model: str,
    permission_mode: str = "ask",
) -> str:
    """Build live device, OS, architecture, and time context."""
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)
    arch = platform.machine() or "unknown"
    os_name = platform.system() or "unknown"
    os_rel = platform.release() or ""
    device = platform.node() or "unknown"
    py_ver = platform.python_version()
    cwd = os.getcwd()

    return (
        "## Live Runtime Context\n"
        f"- Local Time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"- UTC Time: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"- OS: {os_name} {os_rel}\n"
        f"- Architecture: {arch}\n"
        f"- Device / Host: {device}\n"
        f"- Python: {py_ver}\n"
        f"- Working Dir: {cwd}\n"
        f"- Active Gateway: {gateway}\n"
        f"- Active Provider/Model: {provider_id}/{model}\n"
        f"- Permission Mode: {permission_mode}"
    )


def build_system_prompt(
    base_prompt: str | None,
    gateway: str,
    provider_id: str,
    model: str,
    needs_session_title: bool = False,
    permission_mode: str = "ask",
    language: str = "en",
) -> str:
    """Assemble the full system prompt with live context and MEMORY.md."""
    core = (base_prompt or "").strip()
    if not core or core == "You are Hirox, a helpful multi-gateway AI agent.":
        core = DEFAULT_SYSTEM_PROMPT

    parts = [
        core,
        get_runtime_context(
            gateway=gateway,
            provider_id=provider_id,
            model=model,
            permission_mode=permission_mode,
        ),
    ]
    if needs_session_title:
        parts.append(
            "## Session Naming\n"
            "This session does not have a title yet. At the end of your reply, include a concise 2-5 word title "
            "summarizing the topic inside <session_title>Short Title</session_title>."
        )
    if language and language != "en":
        parts.append(
            f"## Language Preference\n"
            f"Preferred UI/response language code: `{language}`. Respond in this language unless the user writes in another."
        )
    mem = load_memory()
    if mem:
        parts.append(f"## Persistent Memory\n{mem}")
    return "\n\n".join(parts)
