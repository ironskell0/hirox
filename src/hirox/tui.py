"""Interactive terminal chat TUI for Hirox."""

from __future__ import annotations

import difflib
import json
import os
import select
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from io import StringIO
from pathlib import Path
from typing import Any, Callable

try:
    import readline
except ImportError:  # pragma: no cover
    readline = None  # type: ignore[assignment]

try:
    import termios
    import tty
except ImportError:  # pragma: no cover
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from hirox import __version__
from hirox.config import load_config, save_config
from hirox.config_tui import _arrow_select, _pick_or_enter_model, _pick_provider, run_config_tui
from hirox.gateway import build_registry_from_config
from hirox.i18n import LANGUAGES, t
from hirox.memory import (
    append_memory,
    build_system_prompt,
    clear_memory,
    extract_memory_writes,
    get_memory_path,
    load_memory,
)
from hirox.providers import (
    chat_completion,
    chat_completion_with_tools,
    estimate_context_window,
    estimate_tokens,
    resolve_api_key,
)

_ORIG_CHAT_COMPLETION = chat_completion
from hirox.sessions import (
    create_session,
    extract_session_title,
    list_sessions,
    load_session,
    save_session,
)
from hirox.tools import PERMISSION_MODES, TOOL_SCHEMAS, execute_tool

SLASH_COMMANDS: dict[str, str] = {
    "/help": "Show available chat commands",
    "/permission": "Set AI permission mode (read_only, ask, full_access)",
    "/resume": "List and resume a previous chat session",
    "/model": "Select a model from list or enter a custom model",
    "/provider": "Switch active AI provider (40+ OpenAI-compatible)",
    "/memory": "View MEMORY.md, add note (/memory <text>), or /memory clear",
    "/config": "Open interactive Config TUI",
    "/gateways": "Show configured gateways and status",
    "/status": "Show active agent, provider, model, and runtime info",
    "/usage": "Show token usage and context window stats",
    "/language": "Switch UI & AI language (15 languages)",
    "/clear": "Clear terminal screen",
    "/exit": "Exit Hirox chat",
}


def _setup_readline() -> None:
    if readline is None or not sys.stdin.isatty():
        return
    cmds = list(SLASH_COMMANDS.keys())

    def _completer(text: str, state: int) -> str | None:
        matches = [c for c in cmds if c.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(_completer)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")


def _render_command_preview(console: Console, prefix: str = "/") -> None:
    matches = {k: v for k, v in SLASH_COMMANDS.items() if k.startswith(prefix)}
    if not matches:
        matches = SLASH_COMMANDS
    table = Table(title="Command Preview", show_header=True, box=None, pad_edge=False)
    table.add_column("Command", style="cyan")
    table.add_column("Description")
    for cmd, desc in matches.items():
        table.add_row(cmd, desc)
    console.print(table)


def _read_chat_input(
    con: Console,
    on_resize: Any | None = None,
    initial_buf: list[str] | None = None,
    on_scroll: Callable[[int], None] | None = None,
) -> str:
    """Read chat input with live '/' command preview and Tab completion on TTY."""
    if not sys.stdin.isatty() or termios is None or tty is None:
        return Prompt.ask("[bold green]you[/bold green]", console=con).strip()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buf: list[str] = list(initial_buf) if initial_buf else []
    resize_flag = [False]
    preview_active = False
    preview_matches: list[tuple[str, str]] = []
    preview_cursor = 0
    prev_preview_range: tuple[int, int] | None = None
    last_ctrl_c = 0.0

    def _on_winch(_signum: int, _frame: Any) -> None:
        resize_flag[0] = True

    def _redraw_slash(show_preview: bool = True) -> None:
        nonlocal preview_active, preview_matches, preview_cursor, prev_preview_range
        text = "".join(buf)
        term = shutil.get_terminal_size((80, 24))
        cols = max(20, term.columns)
        rows = max(4, term.lines)
        input_row = rows - 1

        # Clear exact previously drawn preview rows
        if prev_preview_range is not None:
            old_s, old_e = prev_preview_range
            for r_idx in range(old_s, old_e):
                sys.stdout.write(f"\x1b[{r_idx};1H\x1b[2K")
            prev_preview_range = None

        preview_active = False
        preview_matches = []
        if show_preview and text.startswith("/"):
            matches = [(k, v) for k, v in SLASH_COMMANDS.items() if k.startswith(text)]
            if matches:
                shown = matches[:6]
                preview_matches = shown
                if preview_cursor >= len(shown):
                    preview_cursor = 0
                start_r = max(1, input_row - len(shown))
                for i, (cmd, desc) in enumerate(shown):
                    is_sel = i == preview_cursor
                    prefix_part = cmd[: len(text)]
                    rest_part = cmd[len(text) :]
                    pad = " " * max(1, 12 - len(cmd))
                    max_desc = max(0, cols - 16)
                    short_desc = desc if len(desc) <= max_desc else (desc[: max(0, max_desc - 1)] + "…" if max_desc > 2 else "")
                    if is_sel:
                        cmd_fmt = f"\x1b[1;36m>\x1b[0m \x1b[1;33m{prefix_part}\x1b[1;36m{rest_part}\x1b[0m{pad}"
                        desc_fmt = f"\x1b[37m{short_desc}\x1b[0m"
                    else:
                        cmd_fmt = f"  \x1b[1;33m{prefix_part}\x1b[36m{rest_part}\x1b[0m{pad}"
                        desc_fmt = f"\x1b[2m{short_desc}\x1b[0m"
                    sys.stdout.write(f"\x1b[{start_r + i};1H\x1b[2K{cmd_fmt}{desc_fmt}")
                prev_preview_range = (start_r, start_r + len(shown))
                preview_active = True

        max_vis = max(4, cols - 6)
        disp_text = text if len(text) <= max_vis else ("…" + text[-(max_vis - 1) :])
        sys.stdout.write(f"\x1b[{input_row};1H\x1b[2K\x1b[1;32myou>\x1b[0m {disp_text}")
        col_pos = min(cols, 6 + len(disp_text))
        sys.stdout.write(f"\x1b[{input_row};{col_pos}H")
        sys.stdout.flush()

    prev_winch = signal.signal(signal.SIGWINCH, _on_winch) if hasattr(signal, "SIGWINCH") else None

    term0 = shutil.get_terminal_size((80, 24))
    rows0 = max(4, term0.lines)
    cols0 = max(20, term0.columns)
    init_text = "".join(buf)
    max_vis0 = max(4, cols0 - 6)
    disp0 = init_text if len(init_text) <= max_vis0 else ("…" + init_text[-(max_vis0 - 1) :])
    sys.stdout.write(f"\x1b[{rows0 - 1};1H\x1b[2K\x1b[1;32myou>\x1b[0m {disp0}")
    sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            if resize_flag[0]:
                resize_flag[0] = False
                if on_resize is not None:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        on_resize()
                    finally:
                        tty.setraw(fd)
                prev_preview_range = None
                if preview_active:
                    _redraw_slash(show_preview=True)
                else:
                    r_now = max(4, shutil.get_terminal_size((80, 24)).lines)
                    sys.stdout.write(f"\x1b[{r_now - 1};1H\x1b[2K\x1b[1;32myou>\x1b[0m {''.join(buf)}")
                    sys.stdout.flush()
                continue
            r, _, _ = select.select([fd], [], [], 0.15)
            if not r:
                continue
            b0 = os.read(fd, 1)
            if not b0:
                raise EOFError
            # Decode UTF-8 multi-byte characters cleanly
            if b0[0] >= 0x80:
                need = 1 if (b0[0] & 0xE0) == 0xC0 else (2 if (b0[0] & 0xF0) == 0xE0 else 3)
                extra = b""
                for _ in range(need):
                    rx, _, _ = select.select([fd], [], [], 0.02)
                    if rx:
                        extra += os.read(fd, 1)
                ch = (b0 + extra).decode("utf-8", errors="ignore")
            else:
                ch = b0.decode("ascii", errors="ignore")
            if ch in ("\r", "\n"):
                if preview_active and preview_matches:
                    typed_now = "".join(buf).strip()
                    if typed_now not in SLASH_COMMANDS and " " not in typed_now:
                        buf = list(preview_matches[preview_cursor][0])
                if preview_active:
                    _redraw_slash(show_preview=False)
                r_now = max(4, shutil.get_terminal_size((80, 24)).lines)
                sys.stdout.write(f"\x1b[{r_now - 1};1H\x1b[2K\x1b[{max(1, r_now - 2)};1H\r\n")
                sys.stdout.flush()
                return "".join(buf).strip()
            if ch == "\x03":
                now = time.monotonic()
                if now - last_ctrl_c <= 2.0:
                    if preview_active:
                        _redraw_slash(show_preview=False)
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt
                last_ctrl_c = now
                buf.clear()
                preview_active = False
                sys.stdout.write("\r\x1b[2K\x1b[J\x1b[2mPress Ctrl+C again to exit.\x1b[0m\r\n\x1b[1;32myou>\x1b[0m ")
                sys.stdout.flush()
                continue
            if ch == "\x04":
                if preview_active:
                    _redraw_slash(show_preview=False)
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                raise EOFError
            if ch in ("\x7f", "\b"):
                if buf:
                    buf.pop()
                    text = "".join(buf)
                    if text.startswith("/") or preview_active:
                        _redraw_slash(show_preview=text.startswith("/"))
                    else:
                        cols = max(20, shutil.get_terminal_size((80, 24)).columns)
                        pos = 5 + len(text)
                        if pos % cols == cols - 1:
                            sys.stdout.write(f"\x1b[A\r\x1b[{cols - 1}C \b")
                        else:
                            sys.stdout.write("\b \b")
                        sys.stdout.flush()
                continue
            if ch == "\t":
                text = "".join(buf)
                if text.startswith("/"):
                    matches = [k for k in SLASH_COMMANDS if k.startswith(text)]
                    if matches:
                        buf = list(matches[0])
                        _redraw_slash(show_preview=True)
                continue
            if ch == "\x1b":
                r2, _, _ = select.select([fd], [], [], 0.05)
                if not r2:
                    buf.clear()
                    if preview_active and on_resize is not None:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                        try:
                            on_resize()
                        finally:
                            tty.setraw(fd)
                    preview_active = False
                    r_now = max(4, shutil.get_terminal_size((80, 24)).lines)
                    sys.stdout.write(f"\x1b[{r_now - 1};1H\x1b[2K\x1b[1;32myou>\x1b[0m ")
                    sys.stdout.flush()
                    continue
                if r2:
                    b1 = os.read(fd, 1)
                    if b1 in (b"[", b"O"):
                        r3, _, _ = select.select([fd], [], [], 0.05)
                        if r3:
                            b2 = os.read(fd, 1)
                            delta = 0
                            if b2 == b"A":
                                if preview_active and preview_matches:
                                    preview_cursor = (preview_cursor - 1) % len(preview_matches)
                                    _redraw_slash(show_preview=True)
                                    continue
                                delta = 3
                            elif b2 == b"B":
                                if preview_active and preview_matches:
                                    preview_cursor = (preview_cursor + 1) % len(preview_matches)
                                    _redraw_slash(show_preview=True)
                                    continue
                                delta = -3
                            elif b2 == b"5":
                                os.read(fd, 1)
                                delta = 10
                            elif b2 == b"6":
                                os.read(fd, 1)
                                delta = -10
                            if delta and on_scroll is not None:
                                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                                try:
                                    on_scroll(delta)
                                finally:
                                    tty.setraw(fd)
                                r_now = max(4, shutil.get_terminal_size((80, 24)).lines)
                                sys.stdout.write(f"\x1b[{r_now - 1};1H\x1b[2K\x1b[1;32myou>\x1b[0m {''.join(buf)}")
                                sys.stdout.flush()
                continue
            if ch >= " ":
                buf.append(ch)
                text = "".join(buf)
                if text.startswith("/"):
                    _redraw_slash(show_preview=True)
                else:
                    if preview_active:
                        _redraw_slash(show_preview=False)
                    else:
                        sys.stdout.write(ch)
                        sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        if prev_winch is not None:
            signal.signal(signal.SIGWINCH, prev_winch)


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


_LAST_BAR_ROW: list[int] = [0]


def _set_terminal_title(title: str) -> None:
    """Update the terminal tab/window title on TTY."""
    if sys.stdout.isatty():
        clean = title.replace("\x1b", "").replace("\x07", "").strip() or "New Session"
        sys.stdout.write(f"\x1b]0;Hirox · {clean}\x07\x1b]2;Hirox · {clean}\x07")
        sys.stdout.flush()


def _render_context_bar(con: Console, used_tokens: int, max_tokens: int) -> None:
    """Render a fixed bottom-right context window progress bar (in-place on TTY)."""
    pct = min(100.0, (used_tokens / max(1, max_tokens)) * 100.0)
    bar_width = 12
    filled = min(bar_width, max(0, round((pct / 100.0) * bar_width)))
    empty = bar_width - filled
    fill_color = "red" if pct >= 85 else ("yellow" if pct >= 60 else "cyan")
    t_bar = Text(justify="right")
    t_bar.append("ctx ", style="dim")
    t_bar.append("[", style="dim")
    t_bar.append("█" * filled, style=fill_color)
    t_bar.append("░" * empty, style="dim")
    t_bar.append("] ", style="dim")
    t_bar.append(
        f"{_format_tokens(used_tokens)}/{_format_tokens(max_tokens)} ({pct:.1f}%)",
        style=fill_color if pct >= 60 else "dim",
    )
    if sys.stdout.isatty():
        term = shutil.get_terminal_size((80, 24))
        rows = max(4, term.lines)
        buf = StringIO()
        bar_con = Console(
            file=buf,
            force_terminal=True,
            width=term.columns,
            color_system=con.color_system or "truecolor",
        )
        bar_con.print(t_bar, justify="right", end="")
        rendered = buf.getvalue().rstrip("\r\n")
        clear_old = ""
        if _LAST_BAR_ROW[0] and _LAST_BAR_ROW[0] != rows:
            clear_old = f"\x1b[{_LAST_BAR_ROW[0]};1H\x1b[2K\x1b[{max(1, _LAST_BAR_ROW[0] - 1)};1H\x1b[2K"
        _LAST_BAR_ROW[0] = rows
        scroll_bottom = max(2, rows - 2)
        sys.stdout.write(
            f"\x1b7{clear_old}\x1b[1;{scroll_bottom}r\x1b[{rows};1H\x1b[2K{rendered}\x1b8"
        )
        sys.stdout.flush()
    else:
        con.print(t_bar, justify="right")


def _gateway_request(
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 1.5,
) -> dict[str, Any] | None:
    """Send a lightweight JSON request to the running `hirox start` gateway."""
    url = f"{base_url.rstrip('/')}{path}"
    try:
        if payload is not None:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        else:
            req = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _render_header(
    console: Console,
    agent_name: str,
    gateway: str,
    provider_id: str,
    model: str,
    session_title: str = "New Session",
    permission_mode: str = "ask",
    gw_connected: bool = False,
) -> None:
    perm_label = PERMISSION_MODES.get(permission_mode, (permission_mode, ""))[0]
    if permission_mode == "full_access":
        perm_badge = f"[bold red]{perm_label}[/bold red]"
    elif permission_mode == "read_only":
        perm_badge = f"[dim]{perm_label}[/dim]"
    else:
        perm_badge = f"[cyan]{perm_label}[/cyan]"
    gw_badge = (
        f"[green]{gateway} (connected)[/green]"
        if gw_connected
        else f"[yellow]{gateway} (standalone)[/yellow]"
    )
    console.print(
        f"[bold cyan]◆ Hirox[/bold cyan] [dim]v{__version__}[/dim]  [dim]·[/dim]  "
        f"{gw_badge}  [dim]·[/dim]  "
        f"[magenta]{provider_id}[/magenta]/[yellow]{model}[/yellow]  [dim]·[/dim]  "
        f"{perm_badge}\n"
        f"[dim]session:[/dim] [bold white]{rich_escape(session_title)}[/bold white]  [dim]·  "
        f"/help  /permission  /resume  /model  /provider  /usage  /language  /exit[/dim]"
    )
    console.rule(style="dim")
    if not gw_connected:
        console.print(
            "[yellow]⚠ Gateway is not running. Run `hirox start` in another terminal to connect.[/yellow]"
        )


def _render_gateways(console: Console, config: dict[str, Any]) -> None:
    registry = build_registry_from_config(config)
    table = Table(title="Configured Gateways")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Enabled")
    for st in registry.statuses():
        enabled_str = "[green]yes[/green]" if st.enabled else "[dim]no[/dim]"
        table.add_row(st.name, st.gateway_type, enabled_str)
    console.print(table)


def _full_clear(con: Console) -> None:
    """Clear terminal screen and scrollback buffer."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[3J\x1b[2J\x1b[H")
        sys.stdout.flush()
    else:
        con.clear()


def _run_cancellable(
    con: Console,
    status_text: str,
    fn: Callable[[], Any],
    cancel_event: threading.Event,
    typeahead_buf: list[str] | None = None,
    queued_msgs: list[str] | None = None,
) -> tuple[bool, Any]:
    """Run fn() in a background thread while allowing Esc to cancel and live type-ahead."""
    result_box: dict[str, Any] = {}
    err_box: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result_box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            err_box["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    is_tty = sys.stdin.isatty() and termios is not None and tty is not None
    fd = sys.stdin.fileno() if is_tty else -1
    old_settings = termios.tcgetattr(fd) if is_tty else None
    ta = typeahead_buf if typeahead_buf is not None else []
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    tick = 0

    try:
        if is_tty and old_settings is not None:
            tty.setcbreak(fd)
            while t.is_alive():
                if cancel_event.is_set():
                    break
                spin = frames[tick % len(frames)]
                tick += 1
                cols = max(20, shutil.get_terminal_size((80, 24)).columns)
                typed = "".join(ta)
                short_status = status_text
                if len(short_status) + 24 >= cols:
                    short_status = "thinking..."
                prefix_len = 2 + len(short_status) + 14  # "{spin} {short_status} (Esc) next> "
                avail = max(0, cols - prefix_len - 1)
                if len(typed) > avail:
                    typed_view = ("…" + typed[-(avail - 1):]) if avail > 1 else ""
                else:
                    typed_view = typed
                line = (
                    f"\x1b[36m{spin}\x1b[0m \x1b[2m{short_status} (Esc)\x1b[0m "
                    f"\x1b[1;32mnext>\x1b[0m {typed_view}"
                )
                sys.stdout.write(f"\x1b[?7l\r\x1b[2K{line}\x1b[?7h")
                sys.stdout.flush()

                r, _, _ = select.select([fd], [], [], 0.08)
                if r:
                    b = os.read(fd, 1)
                    if b == b"\x1b":
                        r2, _, _ = select.select([fd], [], [], 0.04)
                        if not r2:
                            cancel_event.set()
                            break
                        os.read(fd, 2)
                    elif b in (b"\r", b"\n"):
                        msg = "".join(ta).strip()
                        ta.clear()
                        if msg and queued_msgs is not None:
                            queued_msgs.append(msg)
                            sys.stdout.write(f"\r\x1b[2K\x1b[2mQueued: {msg}\x1b[0m\r\n")
                            sys.stdout.flush()
                    elif b in (b"\x7f", b"\x08"):
                        if ta:
                            ta.pop()
                    elif b == b"\x03":
                        cancel_event.set()
                        break
                    else:
                        ch = b.decode("utf-8", errors="ignore")
                        if ch and ch.isprintable():
                            ta.append(ch)
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        else:
            with con.status(f"[dim]{status_text} (Esc to cancel)[/dim]"):
                while t.is_alive():
                    if cancel_event.is_set():
                        break
                    t.join(timeout=0.05)
    finally:
        if is_tty and old_settings is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if cancel_event.is_set():
        return True, None
    if "error" in err_box:
        raise err_box["error"]
    return False, result_box.get("value")


def _render_tool_call(con: Console, name: str, raw_args: dict[str, Any] | str) -> None:
    """Render a clean, readable tool header and details (including colored diff for write_file)."""
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    else:
        args = dict(raw_args or {})

    if name == "read_file":
        path = str(args.get("path", ""))
        start = args.get("start_line")
        limit = args.get("limit")
        loc = f"[white]{rich_escape(path)}[/white]"
        if start is not None or limit is not None:
            s = int(start or 1)
            l = int(limit or 200)
            loc += f" [dim](lines {s}-{s + l - 1})[/dim]"
        con.print(f"[bold cyan]● Read[/bold cyan]  {loc}")
    elif name == "list_dir":
        path = str(args.get("path", "."))
        con.print(f"[bold cyan]● List[/bold cyan]  [white]{rich_escape(path)}[/white]")
    elif name == "run_command":
        cmd = str(args.get("command", ""))
        con.print(f"[bold yellow]● Run[/bold yellow]  [dim]$[/dim] [white]{rich_escape(cmd)}[/white]")
    elif name == "web_search":
        q = str(args.get("query", ""))
        con.print(f"[bold green]● Search[/bold green]  [white]{rich_escape(q)}[/white]")
    elif name == "web_fetch":
        u = str(args.get("url", ""))
        con.print(f"[bold green]● Fetch[/bold green]  [white]{rich_escape(u)}[/white]")
    elif name == "workflow_plan":
        act = str(args.get("action", "status"))
        goal = str(args.get("goal", ""))
        detail = f"{act}: {goal}" if goal else act
        con.print(f"[bold blue]● Workflow[/bold blue]  [white]{rich_escape(detail)}[/white]")
    elif name == "write_file":
        path = str(args.get("path", ""))
        new_content = str(args.get("content", ""))
        target = Path(path).expanduser()
        old_lines = (
            target.read_text(encoding="utf-8", errors="replace").splitlines()
            if target.exists() and target.is_file()
            else []
        )
        new_lines = new_content.splitlines()
        diff_lines = list(
            difflib.unified_diff(old_lines, new_lines, lineterm="", n=2)
        )
        body = [ln for ln in diff_lines if not ln.startswith(("---", "+++"))]
        added = sum(1 for ln in body if ln.startswith("+"))
        removed = sum(1 for ln in body if ln.startswith("-"))
        con.print(
            f"[bold magenta]● Write[/bold magenta]  [white]{rich_escape(path)}[/white]  "
            f"[bold green]+{added} added[/bold green]  [bold red]-{removed} removed[/bold red]"
        )
        max_preview = 40
        for ln in body[:max_preview]:
            esc = rich_escape(ln)
            if ln.startswith("+"):
                con.print(f"  [green]{esc}[/green]")
            elif ln.startswith("-"):
                con.print(f"  [red]{esc}[/red]")
            else:
                con.print(f"  [dim]{esc}[/dim]")
        if len(body) > max_preview:
            con.print(f"  [dim]... ({len(body) - max_preview} more diff lines)[/dim]")
    else:
        con.print(f"[bold cyan]● {rich_escape(name)}[/bold cyan]  [dim]{rich_escape(str(args))}[/dim]")


def _render_tool_result(con: Console, name: str, result: str) -> None:
    """Render a compact, readable summary line below a tool call."""
    if result.startswith(("Error:", "Permission denied", "Cancelled")):
        con.print(f"  [red]└─ {rich_escape(result.splitlines()[0])}[/red]")
        return
    if name == "read_file":
        n_lines = 0 if result == "(empty)" else len(result.splitlines())
        con.print(f"  [dim]└─ {n_lines} lines read[/dim]")
    elif name == "list_dir":
        n_items = 0 if result == "(empty directory)" else len(result.splitlines())
        con.print(f"  [dim]└─ {n_items} items[/dim]")
    elif name == "run_command":
        lines = result.splitlines()
        first = lines[0] if lines else "exit_code=0"
        ok = first == "exit_code=0"
        badge = f"[green]{first}[/green]" if ok else f"[red]{first}[/red]"
        con.print(f"  [dim]└─[/dim] {badge}")
        for ln in lines[1:7]:
            con.print(f"     [dim]{rich_escape(ln)}[/dim]")
        if len(lines) > 7:
            con.print(f"     [dim]... ({len(lines) - 7} more lines)[/dim]")
    elif name == "write_file":
        con.print(f"  [dim]└─ {rich_escape(result)}[/dim]")
    elif name == "web_search":
        n_res = sum(1 for ln in result.splitlines() if ln[:2].isdigit() or (len(ln) > 2 and ln[0].isdigit() and ln[1] == "."))
        con.print(f"  [dim]└─ {n_res} results[/dim]")
    elif name == "web_fetch":
        con.print(f"  [dim]└─ {len(result)} chars fetched[/dim]")
    elif name == "workflow_plan":
        for ln in result.splitlines():
            con.print(f"  [dim]{rich_escape(ln)}[/dim]")


def run_chat_tui(gateway: str = "cli", console: Console | None = None) -> None:
    """Run the interactive Hirox terminal chat loop."""
    con = console or Console()
    config = load_config()

    def _active_meta() -> tuple[str, str, str, str, str, str]:
        ag = config.get("agent", {})
        return (
            str(ag.get("name", "hirox")),
            str(ag.get("provider", "openai")),
            str(ag.get("model", "gpt-4o-mini")),
            str(ag.get("system_prompt", "You are Hirox, a helpful multi-gateway AI agent.")),
            str(ag.get("permission_mode", "ask")),
            str(ag.get("language", "en")),
        )

    agent_name, provider_id, model, system_prompt, permission_mode, language = _active_meta()
    rt_cfg = config.get("runtime", {})
    rt_host = str(rt_cfg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    if rt_host == "0.0.0.0":
        rt_host = "127.0.0.1"
    gw_url = f"http://{rt_host}:{rt_cfg.get('port', 8765)}"
    reg_res = _gateway_request(
        gw_url,
        "/chat/register",
        {"gateway": gateway, "provider": provider_id, "model": model},
        timeout=1.5,
    )
    gw_connected = bool(reg_res and reg_res.get("status") == "connected")
    client_id = str((reg_res or {}).get("client_id", ""))
    hb_stop = threading.Event()

    def _hb_loop() -> None:
        nonlocal gw_connected, client_id
        while not hb_stop.wait(5.0):
            if gw_connected and client_id:
                hb_res = _gateway_request(
                    gw_url,
                    "/chat/heartbeat",
                    {
                        "client_id": client_id,
                        "gateway": gateway,
                        "provider": provider_id,
                        "model": model,
                    },
                    timeout=1.5,
                )
                if hb_res and hb_res.get("status") == "ok":
                    continue
                gw_connected = False
                client_id = ""
            re_res = _gateway_request(
                gw_url,
                "/chat/register",
                {"gateway": gateway, "provider": provider_id, "model": model},
                timeout=1.5,
            )
            if re_res and re_res.get("status") == "connected":
                gw_connected = True
                client_id = str(re_res.get("client_id", ""))

    threading.Thread(target=_hb_loop, daemon=True).start()

    session = create_session(gateway=gateway, provider=provider_id, model=model)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    usage_stats: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "last_context_tokens": 0,
        "requests": 0,
        "estimated": False,
    }

    def _render_message(role: str, content: str) -> None:
        if role == "user":
            con.print(f"[bold green]❯ you[/bold green]  {content}")
        elif role == "assistant" and content:
            con.print(f"\n[bold blue]◆ {agent_name}[/bold blue] [dim]({provider_id}/{model})[/dim]")
            con.print(Markdown(content))
            con.print()

    scroll_offset = [0]

    def _redraw_screen(scroll_delta: int = 0) -> None:
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[r")
            sys.stdout.flush()
        _full_clear(con)
        s_title = str(session.get("title", "New Session"))
        _set_terminal_title(s_title)
        if sys.stdout.isatty():
            term = shutil.get_terminal_size((80, 24))
            view_h = max(4, term.lines - 2)
            buf = StringIO()
            vcon = Console(
                file=buf,
                force_terminal=True,
                width=term.columns,
                color_system=con.color_system or "truecolor",
            )
            _render_header(
                vcon,
                agent_name=agent_name,
                gateway=gateway,
                provider_id=provider_id,
                model=model,
                session_title=s_title,
                permission_mode=permission_mode,
                gw_connected=gw_connected,
            )
            for msg in messages[1:]:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    role = str(msg["role"])
                    content = str(msg["content"])
                    if role == "user":
                        vcon.print(f"[bold green]❯ you[/bold green]  {content}")
                    else:
                        vcon.print(f"\n[bold blue]◆ {agent_name}[/bold blue] [dim]({provider_id}/{model})[/dim]")
                        vcon.print(Markdown(content))
                        vcon.print()
            all_lines = buf.getvalue().splitlines()
            max_off = max(0, len(all_lines) - view_h)
            scroll_offset[0] = max(0, min(max_off, scroll_offset[0] + scroll_delta))
            cap_h = max(1, view_h - 1) if scroll_offset[0] > 0 else view_h
            end_idx = len(all_lines) - scroll_offset[0]
            start_idx = max(0, end_idx - cap_h)
            visible = all_lines[start_idx:end_idx]
            out_buf = "\x1b[?2026h\x1b[1;1H"
            if visible:
                out_buf += "\r\n".join(visible)
                if len(visible) < view_h:
                    out_buf += "\r\n"
            if scroll_offset[0] > 0:
                out_buf += f"\x1b[2m↑ scrolled ({scroll_offset[0]} lines) · ↓/PgDn to return\x1b[0m"
            out_buf += "\x1b[?2026l"
            sys.stdout.write(out_buf)
            sys.stdout.flush()
        else:
            _render_header(
                con,
                agent_name=agent_name,
                gateway=gateway,
                provider_id=provider_id,
                model=model,
                session_title=s_title,
                permission_mode=permission_mode,
                gw_connected=gw_connected,
            )
            for msg in messages[1:]:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    _render_message(str(msg["role"]), str(msg["content"]))
        ctx_limit = estimate_context_window(model)
        ctx_used = usage_stats["last_context_tokens"] or estimate_tokens(messages)
        _render_context_bar(con, ctx_used, ctx_limit)

    if sys.stdout.isatty():
        sys.stdout.write("\x1b[?1049h")
        sys.stdout.flush()

    _setup_readline()
    _redraw_screen()
    queued_msgs: list[str] = []
    typeahead_buf: list[str] = []

    while True:
        if sys.stdout.isatty():
            ctx_limit = estimate_context_window(model)
            ctx_used = usage_stats["last_context_tokens"] or estimate_tokens(messages)
            _render_context_bar(con, ctx_used, ctx_limit)
        if queued_msgs:
            user_input = queued_msgs.pop(0)
            _render_message("user", user_input)
        else:
            try:
                user_input = _read_chat_input(
                    con,
                    on_resize=_redraw_screen,
                    initial_buf=typeahead_buf,
                    on_scroll=_redraw_screen,
                )
                typeahead_buf.clear()
                scroll_offset[0] = 0
                if user_input and sys.stdout.isatty():
                    _render_message("user", user_input)
            except (EOFError, KeyboardInterrupt):
                con.print(f"\n[dim]{t(language, 'exiting')}[/dim]")
                break

        if not user_input:
            continue

        if user_input in ("/exit", "/quit"):
            con.print(f"[dim]{t(language, 'goodbye')}[/dim]")
            break
        if user_input == "/help":
            _render_command_preview(con, "/")
            continue
        if user_input == "/clear":
            _full_clear(con)
            _render_header(con, agent_name=agent_name, gateway=gateway, provider_id=provider_id, model=model)
            continue
        if user_input == "/gateways":
            live_health = _gateway_request(gw_url, "/health", timeout=1.5)
            if live_health and isinstance(live_health.get("gateways"), list):
                gw_connected = True
                table = Table(
                    title=f"Live Gateways ({gw_url} · uptime {live_health.get('uptime_seconds')}s · clients {live_health.get('active_clients')})"
                )
                table.add_column("Name", style="cyan")
                table.add_column("Type")
                table.add_column("Running")
                table.add_column("Detail", style="dim")
                for g in live_health["gateways"]:
                    run_str = "[green]yes[/green]" if g.get("running") else "[dim]no[/dim]"
                    table.add_row(str(g.get("name")), str(g.get("type")), run_str, str(g.get("detail")))
                con.print(table)
            else:
                _render_gateways(con, config)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            arg = user_input[6:].strip()
            if arg:
                chosen = arg
                models = list(config.get("providers", {}).get(provider_id, {}).get("models", []))
                if chosen not in models:
                    models.append(chosen)
                    config["providers"][provider_id]["models"] = models
            else:
                chosen = _pick_or_enter_model(con, config, provider_id)
            if chosen:
                model = chosen
                config.setdefault("agent", {})["model"] = model
                save_config(config)
            con.clear()
            _render_header(con, agent_name=agent_name, gateway=gateway, provider_id=provider_id, model=model)
            continue
        if user_input == "/provider" or user_input.startswith("/provider "):
            arg = user_input[9:].strip()
            providers_map = config.get("providers", {})
            if arg and arg in providers_map:
                pid = arg
                p_models = list(providers_map[pid].get("models", []))
                chosen = p_models[0] if p_models else model
            elif arg:
                con.print(f"[red]Unknown provider:[/red] {arg}")
                continue
            else:
                pid = _pick_provider(con, config)
                chosen = _pick_or_enter_model(con, config, pid) if pid else None
            if pid:
                provider_id = pid
                config.setdefault("agent", {})["provider"] = provider_id
                if chosen:
                    model = chosen
                    config["agent"]["model"] = model
                config["providers"][provider_id]["enabled"] = True
                save_config(config)
            con.clear()
            _render_header(con, agent_name=agent_name, gateway=gateway, provider_id=provider_id, model=model)
            continue
        if user_input == "/permission" or user_input.startswith("/permission "):
            arg = user_input[11:].strip()
            if arg in PERMISSION_MODES:
                chosen_perm: str | None = arg
            elif arg:
                con.print(f"[red]Unknown permission mode:[/red] {arg}")
                continue
            else:
                perm_items = [
                    (k, label, desc) for k, (label, desc) in PERMISSION_MODES.items()
                ]
                chosen_perm = _arrow_select(con, title="Select Permission Mode", items=perm_items)

            if chosen_perm == "full_access":
                con.print(
                    "[bold red]WARNING: Full Access [RISKY][/bold red] allows unrestricted shell commands and file writes."
                )
                c1 = Prompt.ask("Enable Full Access? (y/n)", console=con, default="n").strip().lower()
                if c1 not in ("y", "yes", "evet"):
                    con.print("[yellow]Cancelled.[/yellow]")
                    continue

            if chosen_perm:
                permission_mode = chosen_perm
                config.setdefault("agent", {})["permission_mode"] = permission_mode
                save_session(session)
                save_config(config)
            _redraw_screen()
            continue
        if user_input == "/resume" or user_input.startswith("/resume "):
            arg = user_input[7:].strip()
            saved = list_sessions()
            if not saved:
                con.print("[dim]No saved sessions found.[/dim]")
                continue
            chosen_id: str | None = None
            if arg:
                for s in saved:
                    if s["id"] == arg or s["id"].startswith(arg):
                        chosen_id = s["id"]
                        break
                if chosen_id is None and arg.isdigit() and 1 <= int(arg) <= len(saved):
                    chosen_id = saved[int(arg) - 1]["id"]
            else:
                items = [
                    (
                        s["id"],
                        str(s.get("title", "Untitled")),
                        f"{len(s.get('messages', []))} msgs | {s.get('updated_at', '')}",
                    )
                    for s in saved
                ]
                chosen_id = _arrow_select(con, title="Resume Session", items=items)
            if chosen_id:
                loaded = load_session(chosen_id)
                session = loaded
                provider_id = str(loaded.get("provider", provider_id))
                model = str(loaded.get("model", model))
                messages = [{"role": "system", "content": system_prompt}] + list(loaded.get("messages", []))
            _redraw_screen()
            continue
        if user_input == "/memory" or user_input.startswith("/memory "):
            arg = user_input[7:].strip()
            if arg == "clear":
                p = clear_memory()
                con.print(f"[yellow]Memory cleared:[/yellow] {p}")
            elif arg:
                p = append_memory(arg)
                con.print(f"[bold magenta]Memory saved:[/bold magenta] {arg} [dim]({p})[/dim]")
            else:
                mem_text = load_memory()
                if mem_text:
                    con.print(Panel(Markdown(mem_text), title=f"Memory ({get_memory_path()})", border_style="magenta"))
                else:
                    con.print(f"[dim]Memory is empty ({get_memory_path()}).[/dim]")
            continue
        if user_input == "/config":
            if sys.stdout.isatty():
                sys.stdout.write("\x1b[r\x1b[2J\x1b[H")
                sys.stdout.flush()
            try:
                run_config_tui(console=con)
            finally:
                if sys.stdout.isatty():
                    sys.stdout.write("\x1b[?25h")
                    sys.stdout.flush()
            config = load_config()
            agent_name, provider_id, model, system_prompt, permission_mode, language = _active_meta()
            messages[0] = {"role": "system", "content": system_prompt}
            _redraw_screen()
            continue
        if user_input == "/language" or user_input.startswith("/language "):
            arg = user_input[9:].strip().lower()
            chosen_lang: str | None = None
            if arg in LANGUAGES:
                chosen_lang = arg
            elif arg:
                con.print(f"[red]{t(language, 'unknown_lang')}[/red] {arg}")
                continue
            else:
                lang_items = [
                    (code, f"{native} ({eng})", code)
                    for code, (eng, native) in LANGUAGES.items()
                ]
                chosen_lang = _arrow_select(
                    con,
                    title=t(language, "select_lang"),
                    items=lang_items,
                )
            if chosen_lang and chosen_lang in LANGUAGES:
                language = chosen_lang
                config.setdefault("agent", {})["language"] = language
                save_config(config)
                eng, native = LANGUAGES[language]
                _redraw_screen()
                con.print(f"[green]{t(language, 'lang_updated')}[/green] {native} ({eng})")
            else:
                _redraw_screen()
            continue
        if user_input == "/usage":
            ctx_limit = estimate_context_window(model)
            ctx_used = usage_stats["last_context_tokens"] or estimate_tokens(messages)
            pct = min(100.0, (ctx_used / max(1, ctx_limit)) * 100.0)
            mode_note = "estimated" if usage_stats["estimated"] or usage_stats["requests"] == 0 else "API reported"
            con.print(
                f"[bold cyan]● Usage[/bold cyan]  [dim]({model} · {mode_note})[/dim]\n"
                f"  Context window : [white]{ctx_used:,}[/white] / [dim]{ctx_limit:,}[/dim] ({pct:.1f}%)\n"
                f"  Prompt tokens  : [white]{usage_stats['prompt_tokens']:,}[/white]\n"
                f"  Output tokens  : [white]{usage_stats['completion_tokens']:,}[/white]\n"
                f"  Total tokens   : [bold green]{usage_stats['total_tokens']:,}[/bold green]  "
                f"[dim]({usage_stats['requests']} requests)[/dim]"
            )
            continue
        if user_input == "/status":
            rt = config.get("runtime", {})
            p_cfg = config.get("providers", {}).get(provider_id, {})
            has_key = bool(resolve_api_key(p_cfg)) or not p_cfg.get("requires_key", True)
            con.print(
                f"[cyan]Agent:[/cyan] {agent_name} | "
                f"[cyan]Provider:[/cyan] {provider_id} (key={'ok' if has_key else 'missing'}) | "
                f"[cyan]Model:[/cyan] {model} | "
                f"[cyan]Language:[/cyan] {language} | "
                f"[cyan]Host:[/cyan] {rt.get('host')}:{rt.get('port')} | "
                f"[cyan]Gateway:[/cyan] {gateway}"
            )
            continue
        if user_input.startswith("/") and user_input not in SLASH_COMMANDS and user_input != "/quit":
            _render_command_preview(con, user_input)
            continue

        provider_cfg = config.get("providers", {}).get(provider_id)
        if not provider_cfg:
            con.print(f"[red]Unknown provider:[/red] {provider_id}. Use /provider or /config.")
            continue

        needs_title = session.get("title") == "New Session"
        messages[0] = {
            "role": "system",
            "content": build_system_prompt(
                system_prompt,
                gateway=gateway,
                provider_id=provider_id,
                model=model,
                needs_session_title=needs_title,
                permission_mode=permission_mode,
                language=language,
            ),
        }
        turn_start_len = len(messages)
        messages.append({"role": "user", "content": user_input})

        def _confirm_tool(desc: str) -> bool:
            ans = Prompt.ask(
                f"[yellow]Allow tool[/yellow] [cyan]{desc}[/cyan]? (y/n)",
                console=con,
                default="n",
            ).strip().lower()
            return ans in ("y", "yes", "evet")

        cancel_event = threading.Event()
        try:
            final_reply = ""
            was_cancelled = False
            while True:
                def _call_llm() -> dict[str, Any]:
                    if chat_completion is not _ORIG_CHAT_COMPLETION:
                        return {
                            "content": chat_completion(
                                provider_cfg=provider_cfg,
                                model=model,
                                messages=messages,
                            )
                        }
                    return chat_completion_with_tools(
                        provider_cfg=provider_cfg,
                        model=model,
                        messages=messages,
                        tools=TOOL_SCHEMAS,
                    )

                was_cancelled, msg_obj = _run_cancellable(
                    con,
                    f"{provider_id}/{model} {t(language, 'thinking')}",
                    _call_llm,
                    cancel_event,
                    typeahead_buf=typeahead_buf,
                    queued_msgs=queued_msgs,
                )
                if was_cancelled:
                    break

                tool_calls = msg_obj.get("tool_calls") or []
                raw_content = str(msg_obj.get("content") or "")
                api_usage = msg_obj.get("_usage")
                if isinstance(api_usage, dict):
                    pt = int(api_usage.get("prompt_tokens") or 0)
                    ct = int(api_usage.get("completion_tokens") or 0)
                    tt = int(api_usage.get("total_tokens") or (pt + ct))
                    usage_stats["prompt_tokens"] += pt
                    usage_stats["completion_tokens"] += ct
                    usage_stats["total_tokens"] += tt
                    usage_stats["last_context_tokens"] = pt + ct
                    usage_stats["requests"] += 1
                else:
                    pt = estimate_tokens(messages)
                    ct = max(1, len(raw_content) // 4)
                    usage_stats["prompt_tokens"] += pt
                    usage_stats["completion_tokens"] += ct
                    usage_stats["total_tokens"] += pt + ct
                    usage_stats["last_context_tokens"] = pt + ct
                    usage_stats["requests"] += 1
                    usage_stats["estimated"] = True

                if tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": raw_content,
                            "tool_calls": tool_calls,
                        }
                    )
                    for tc in tool_calls:
                        fn = tc.get("function") or {}
                        t_name = str(fn.get("name", ""))
                        t_args = fn.get("arguments", {})
                        _render_tool_call(con, t_name, t_args)

                        # Handle ask-mode confirmation on main thread before cbreak watcher
                        eff_perm = permission_mode
                        if permission_mode == "ask" and t_name in ("write_file", "run_command"):
                            parsed_args = (
                                json.loads(t_args)
                                if isinstance(t_args, str) and t_args.strip()
                                else (dict(t_args) if isinstance(t_args, dict) else {})
                            )
                            desc = (
                                f"run_command: {parsed_args.get('command', '')}"
                                if t_name == "run_command"
                                else f"write_file: {parsed_args.get('path', '')}"
                            )
                            if _confirm_tool(desc):
                                eff_perm = "full_access"
                            else:
                                t_result = f"Permission denied by user for {t_name}."
                                messages.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": str(tc.get("id", t_name)),
                                        "content": t_result,
                                    }
                                )
                                continue

                        was_cancelled, t_result = _run_cancellable(
                            con,
                            f"Running {t_name}...",
                            lambda n=t_name, a=t_args, p=eff_perm: execute_tool(
                                name=n,
                                arguments=a,
                                permission_mode=p,
                                confirm_fn=_confirm_tool,
                                cancel_event=cancel_event,
                            ),
                            cancel_event,
                            typeahead_buf=typeahead_buf,
                            queued_msgs=queued_msgs,
                        )
                        if was_cancelled:
                            break
                        _render_tool_result(con, t_name, t_result)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": str(tc.get("id", t_name)),
                                "content": t_result,
                            }
                        )
                    if was_cancelled:
                        break
                    continue

                final_reply = raw_content
                break

            if was_cancelled:
                del messages[turn_start_len:]
                con.print(f"[yellow]{t(language, 'cancelled_esc')}[/yellow]\n")
                continue

            reply, mem_entries = extract_memory_writes(final_reply)
            reply, ai_title = extract_session_title(reply)
            title_changed = False
            if ai_title and session.get("title") == "New Session":
                session["title"] = ai_title
                _set_terminal_title(ai_title)
                con.print(f"[dim]Session named: {ai_title}[/dim]")
                title_changed = True
            elif session.get("title") == "New Session":
                fallback = " ".join(user_input.split()[:5])[:40]
                if fallback:
                    session["title"] = fallback
                    _set_terminal_title(fallback)
                    title_changed = True
            for entry in mem_entries:
                append_memory(entry)
                con.print(f"[bold magenta]Memory saved:[/bold magenta] {entry}")
            messages.append({"role": "assistant", "content": reply})
            session["provider"] = provider_id
            session["model"] = model
            session["messages"] = [
                m for m in messages[1:] if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            save_session(session)
            if title_changed and sys.stdout.isatty():
                _redraw_screen()
            elif reply:
                _render_message("assistant", reply)
        except Exception as exc:
            del messages[turn_start_len:]
            con.print(
                f"[yellow]{rich_escape(str(exc))}[/yellow]\n"
                "[dim]Tip: run /config or /provider to set API key or switch provider.[/dim]"
            )

    hb_stop.set()
    if gw_connected and client_id:
        _gateway_request(gw_url, "/chat/disconnect", {"client_id": client_id}, timeout=0.5)

    if sys.stdout.isatty():
        sys.stdout.write("\x1b[r\x1b[?1049l")
        sys.stdout.flush()
