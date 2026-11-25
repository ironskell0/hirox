"""Agentic tools and permission enforcement for Hirox."""

from __future__ import annotations

import html
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from hirox.security import (
    backup_file,
    check_command_safety,
    latest_backup_for,
    log_security_event,
    validate_path,
    wrap_untrusted_content,
)

PERMISSION_MODES: dict[str, tuple[str, str]] = {
    "read_only": ("Only Read", "Browse directories and read files only"),
    "ask": ("Ask Permission", "Read freely; ask before running commands or writing"),
    "full_access": ("Full Access [RISKY]", "Unrestricted command & file execution"),
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories at a given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default '.')"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read text content of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "start_line": {"type": "integer", "description": "1-based start line (default 1)"},
                    "limit": {"type": "integer", "description": "Max lines to read (default 200)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite a file with text content (creates automatic backup).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Text content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Safely patch an existing file by replacing an exact text snippet (creates automatic backup).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_text": {"type": "string", "description": "Exact text to replace"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback_file",
            "description": "Restore a file from its most recent automatic backup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to restore"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command in the terminal with timeout and optional retries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)"},
                    "retries": {"type": "integer", "description": "Retry count on non-zero exit or timeout (default 0, max 2)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo and return top results (title, URL, snippet).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results to return (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and extract readable text from a web URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "HTTP/HTTPS URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Max characters to return (default 4000)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_plan",
            "description": "Manage an autonomous multi-step agent workflow plan (set steps, update step status, check status, or clear).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "update", "status", "clear"],
                        "description": "Workflow action",
                    },
                    "goal": {"type": "string", "description": "Overall goal (for action='set')"},
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of step titles (for action='set')",
                    },
                    "step_index": {"type": "integer", "description": "1-based step index (for action='update')"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed"],
                        "description": "New step status (for action='update')",
                    },
                    "note": {"type": "string", "description": "Optional progress note"},
                },
                "required": ["action"],
            },
        },
    },
]


def _tool_list_dir(path: str = ".") -> str:
    ok, target, err = validate_path(path)
    if not ok or target is None:
        log_security_event("path_blocked", err, severity="warn")
        return err
    if not target.exists():
        return f"Error: path does not exist: {target}"
    if not target.is_dir():
        return f"Error: not a directory: {target}"
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = [f"{'[DIR] ' if p.is_dir() else '      '}{p.name}" for p in entries[:200]]
    return "\n".join(lines) if lines else "(empty directory)"


def _tool_read_file(path: str, start_line: int = 1, limit: int = 200) -> str:
    ok, target, err = validate_path(path)
    if not ok or target is None:
        log_security_event("path_blocked", err, severity="warn")
        return err
    if not target.exists():
        return f"Error: file does not exist: {target}"
    if not target.is_file():
        return f"Error: not a file: {target}"
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(start_line)) - 1
    end = start + max(1, int(limit))
    chunk = lines[start:end]
    numbered = [f"{idx}: {line}" for idx, line in enumerate(chunk, start=start + 1)]
    return "\n".join(numbered) if numbered else "(empty)"


def _tool_write_file(path: str, content: str) -> str:
    ok, target, err = validate_path(path)
    if not ok or target is None:
        log_security_event("path_blocked", err, severity="warn")
        return err
    target.parent.mkdir(parents=True, exist_ok=True)
    bak = backup_file(target)
    target.write_text(content, encoding="utf-8")
    bak_note = f" (backup: {bak.name})" if bak else ""
    return f"Wrote {len(content)} chars to {target}{bak_note}"


def _tool_edit_file(path: str, old_text: str, new_text: str) -> str:
    ok, target, err = validate_path(path)
    if not ok or target is None:
        log_security_event("path_blocked", err, severity="warn")
        return err
    if not target.exists() or not target.is_file():
        return f"Error: file does not exist: {target}"
    raw = target.read_text(encoding="utf-8", errors="replace")
    count = raw.count(old_text)
    if count == 0:
        return "Error: old_text not found in file."
    if count > 1:
        return f"Error: old_text matched {count} times; provide a more specific snippet."
    bak = backup_file(target)
    updated = raw.replace(old_text, new_text, 1)
    target.write_text(updated, encoding="utf-8")
    bak_note = f" (backup: {bak.name})" if bak else ""
    return f"Patched {target}{bak_note}"


def _tool_rollback_file(path: str) -> str:
    ok, target, err = validate_path(path)
    if not ok or target is None:
        log_security_event("path_blocked", err, severity="warn")
        return err
    bak = latest_backup_for(target)
    if bak is None or not bak.exists():
        return f"Error: no backup found for {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bak.read_bytes())
    return f"Restored {target} from backup {bak.name}"


def _run_command_once(
    command: str,
    timeout_sec: int,
    cancel_event: threading.Event | None = None,
) -> tuple[bool, str]:
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"Error: failed to start command: {exc}"

    deadline = time.monotonic() + float(timeout_sec)
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait(timeout=2)
            return False, "Cancelled by user (Esc)."
        if time.monotonic() > deadline:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait(timeout=2)
            return False, f"Error: command timed out after {timeout_sec}s."
        time.sleep(0.05)

    out = (proc.stdout.read() if proc.stdout else "") or ""
    out = out.strip()
    if len(out) > 4000:
        out = out[:4000] + "\n...(truncated)"
    msg = f"exit_code={proc.returncode}\n{out}" if out else f"exit_code={proc.returncode}"
    return proc.returncode == 0, msg


def _tool_run_command(
    command: str,
    timeout: int = 60,
    retries: int = 0,
    cancel_event: threading.Event | None = None,
) -> str:
    if not command.strip():
        return "Error: empty command."
    timeout_sec = max(1, min(300, int(timeout)))
    max_retries = max(0, min(2, int(retries)))

    last_msg = ""
    for attempt in range(max_retries + 1):
        ok, msg = _run_command_once(command, timeout_sec, cancel_event=cancel_event)
        last_msg = msg
        if ok or msg.startswith("Cancelled by user"):
            return msg
        if attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))
    if max_retries > 0:
        return f"{last_msg}\n(failed after {max_retries + 1} attempts)"
    return last_msg


def _tool_web_search(query: str, max_results: int = 5) -> str:
    q = query.strip()
    if not q:
        return "Error: empty search query."
    limit = max(1, min(10, int(max_results)))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _fetch_ddg(url: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(0.5)
        raise last_exc  # type: ignore[misc]

    links: list[tuple[str, str]] = []
    snippets: list[tuple[str, str]] = []
    try:
        raw = _fetch_ddg(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(q)}")
        links = re.findall(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            raw,
            flags=re.DOTALL | re.IGNORECASE,
        )
        snippets = re.findall(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>|<div[^>]+class="result__snippet"[^>]*>(.*?)</div>',
            raw,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not links:
            raw_lite = _fetch_ddg(f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote_plus(q)}")
            links = re.findall(
                r'<a[^>]+class=[\'"]result-link[\'"][^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>',
                raw_lite,
                flags=re.DOTALL | re.IGNORECASE,
            )
            lite_snips = re.findall(
                r'<td[^>]+class=[\'"]result-snippet[\'"][^>]*>(.*?)</td>',
                raw_lite,
                flags=re.DOTALL | re.IGNORECASE,
            )
            snippets = [(s, "") for s in lite_snips]
    except Exception as exc:
        return f"Error: web_search failed: {exc}"

    if not links:
        return "No search results found."

    results: list[str] = []
    for idx, (href, raw_title) in enumerate(links[:limit], start=1):
        title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        # Unwrap DuckDuckGo uddg redirect if present
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        clean_url = qs.get("uddg", [href])[0]
        snip = ""
        if idx - 1 < len(snippets):
            raw_snip = snippets[idx - 1][0] or snippets[idx - 1][1]
            snip = html.unescape(re.sub(r"<[^>]+>", "", raw_snip)).strip()
        entry = f"{idx}. {title}\n   {clean_url}"
        if snip:
            entry += f"\n   {snip}"
        results.append(entry)
    return wrap_untrusted_content("web_search:duckduckgo", "\n\n".join(results))


def _tool_web_fetch(url: str, max_chars: int = 4000) -> str:
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"
    req = urllib.request.Request(
        target,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Hirox/0.1)"},
    )
    raw = ""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            last_exc = None
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.5)
        except Exception as exc:
            return f"Error: web_fetch failed: {exc}"
    if last_exc is not None:
        return f"Error: web_fetch failed after retry: {last_exc}"

    cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(re.sub(r"\s+", " ", cleaned)).strip()
    cap = max(200, min(12000, int(max_chars)))
    if len(cleaned) > cap:
        cleaned = cleaned[:cap] + "\n...(truncated)"
    return wrap_untrusted_content(f"web_fetch:{target}", cleaned or "(empty page)")


_WORKFLOW_STATE: dict[str, Any] = {"goal": "", "steps": []}


def _format_workflow() -> str:
    steps = _WORKFLOW_STATE.get("steps") or []
    if not steps:
        return "Workflow plan is empty."
    icons = {"completed": "✓", "in_progress": "▶", "failed": "✗", "pending": " "}
    lines = [f"Goal: {_WORKFLOW_STATE.get('goal') or '(untitled)'}"]
    for idx, st in enumerate(steps, start=1):
        ic = icons.get(st.get("status", "pending"), " ")
        note = f" — {st['note']}" if st.get("note") else ""
        lines.append(f"  {idx}. [{ic}] {st.get('title', '')}{note}")
    done = sum(1 for s in steps if s.get("status") == "completed")
    lines.append(f"Progress: {done}/{len(steps)} completed")
    return "\n".join(lines)


def _tool_workflow_plan(args: dict[str, Any]) -> str:
    action = str(args.get("action", "status")).strip().lower()
    if action == "clear":
        _WORKFLOW_STATE["goal"] = ""
        _WORKFLOW_STATE["steps"] = []
        return "Workflow plan cleared."
    if action == "set":
        raw_steps = args.get("steps") or []
        _WORKFLOW_STATE["goal"] = str(args.get("goal", "")).strip()
        _WORKFLOW_STATE["steps"] = [
            {"title": str(s).strip(), "status": "in_progress" if i == 0 else "pending", "note": ""}
            for i, s in enumerate(raw_steps)
            if str(s).strip()
        ]
        return _format_workflow()
    if action == "update":
        steps = _WORKFLOW_STATE.get("steps") or []
        idx = int(args.get("step_index", 0)) - 1
        if idx < 0 or idx >= len(steps):
            return f"Error: invalid step_index {idx + 1} (total {len(steps)})."
        status = str(args.get("status", "completed")).strip().lower()
        steps[idx]["status"] = status
        if args.get("note"):
            steps[idx]["note"] = str(args["note"]).strip()
        if status == "completed" and idx + 1 < len(steps) and steps[idx + 1]["status"] == "pending":
            steps[idx + 1]["status"] = "in_progress"
        return _format_workflow()
    return _format_workflow()


def execute_tool(
    name: str,
    arguments: dict[str, Any] | str,
    permission_mode: str,
    confirm_fn: Callable[[str], bool] | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Execute a Hirox tool while enforcing permission_mode."""
    if permission_mode not in PERMISSION_MODES:
        log_security_event(
            "invalid_permission_mode",
            f"Unknown permission_mode '{permission_mode}', falling back to read_only",
            severity="warn",
        )
        permission_mode = "read_only"

    if isinstance(arguments, str):
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return f"Error: invalid JSON arguments for {name}"
    else:
        args = dict(arguments or {})

    try:
        if name == "list_dir":
            return _tool_list_dir(str(args.get("path", ".")))
        if name == "read_file":
            return _tool_read_file(
                path=str(args.get("path", "")),
                start_line=int(args.get("start_line", 1)),
                limit=int(args.get("limit", 200)),
            )
        if name == "web_search":
            return _tool_web_search(
                query=str(args.get("query", "")),
                max_results=int(args.get("max_results", 5)),
            )
        if name == "web_fetch":
            return _tool_web_fetch(
                url=str(args.get("url", "")),
                max_chars=int(args.get("max_chars", 4000)),
            )
        if name == "workflow_plan":
            return _tool_workflow_plan(args)

        if name in ("write_file", "edit_file", "rollback_file", "run_command"):
            if permission_mode == "read_only":
                log_security_event("permission_denied", f"{name} blocked in read_only mode", severity="info")
                return f"Permission denied: '{name}' is blocked in read_only mode."

            if name == "run_command":
                cmd_str = str(args.get("command", ""))
                safety, reason = check_command_safety(cmd_str)
                if safety == "block":
                    log_security_event("catastrophic_command_blocked", f"{reason}: {cmd_str}", severity="critical")
                    return f"Security blocked: {reason} is not permitted."
                if safety == "confirm":
                    log_security_event("destructive_command_prompt", f"{reason}: {cmd_str}", severity="warn")
                    if confirm_fn is None or not confirm_fn(f"DESTRUCTIVE ({reason}): {cmd_str}"):
                        return f"Security blocked: destructive command ({reason}) requires explicit confirmation."

            if permission_mode == "ask":
                if name == "run_command":
                    desc = f"run_command: {args.get('command', '')}"
                elif name == "edit_file":
                    desc = f"edit_file: {args.get('path', '')}"
                elif name == "rollback_file":
                    desc = f"rollback_file: {args.get('path', '')}"
                else:
                    desc = f"write_file: {args.get('path', '')}"
                if confirm_fn is None or not confirm_fn(desc):
                    log_security_event("permission_denied", f"User denied {name}", severity="info")
                    return f"Permission denied by user for {name}."

            if name == "write_file":
                return _tool_write_file(
                    path=str(args.get("path", "")),
                    content=str(args.get("content", "")),
                )
            if name == "edit_file":
                return _tool_edit_file(
                    path=str(args.get("path", "")),
                    old_text=str(args.get("old_text", "")),
                    new_text=str(args.get("new_text", "")),
                )
            if name == "rollback_file":
                return _tool_rollback_file(path=str(args.get("path", "")))
            return _tool_run_command(
                str(args.get("command", "")),
                timeout=int(args.get("timeout", 60)),
                retries=int(args.get("retries", 0)),
                cancel_event=cancel_event,
            )

        return f"Error: unknown tool '{name}'"
    except Exception as exc:
        return f"Error: {name} failed: {exc}"
