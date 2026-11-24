"""Interactive arrow-key Config TUI for Hirox providers, models, and gateways."""

from __future__ import annotations

import base64
import os
import select
import shutil
import signal
import subprocess
import sys
import webbrowser
from io import StringIO
from typing import Any, Callable

try:
    import termios
    import tty
except ImportError:  # pragma: no cover
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from hirox import __version__
from hirox.config import get_config_path, load_config, save_config
from hirox.i18n import t
from hirox.oauth import (
    OAUTH_PROFILES,
    build_oauth_authorize_url,
    exchange_oauth_code,
    fetch_provider_models,
    generate_pkce_pair,
    save_oauth_credentials,
    start_loopback_callback_server,
)
from hirox.providers import resolve_api_key


def _render_banner(con: Console, cfg: dict[str, Any]) -> None:
    agent = cfg.get("agent", {})
    provider_id = agent.get("provider", "openai")
    model = agent.get("model", "gpt-4o-mini")
    providers = cfg.get("providers", {})
    p_name = providers.get(provider_id, {}).get("name", provider_id)
    gateways = cfg.get("gateways", {})
    enabled_gws = [k for k, v in gateways.items() if isinstance(v, dict) and v.get("enabled")]

    con.print(
        f"[bold cyan]Hirox Config TUI[/bold cyan] v{__version__}\n"
        f"[cyan]Provider:[/cyan] [green]{p_name}[/green] ({provider_id})  |  "
        f"[cyan]Model:[/cyan] [yellow]{model}[/yellow]\n"
        f"[cyan]Gateways:[/cyan] {', '.join(enabled_gws) or 'none'}\n"
        f"[dim]{get_config_path()}[/dim]\n"
    )


def _open_url(url: str) -> bool:
    """Open URL in browser (Termux-aware with webbrowser fallback)."""
    if shutil.which("termux-open-url"):
        try:
            subprocess.Popen(
                ["termux-open-url", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard via Termux/OS tools or OSC 52 terminal escape."""
    for cmd in (
        ["termux-clipboard-set"],
        ["pbcopy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True, timeout=3)
                return True
            except Exception:
                pass
    if sys.stdout.isatty():
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        sys.stdout.write(f"\x1b]52;c;{b64}\x07")
        sys.stdout.flush()
        return True
    return False


def _read_key(resize_flag: list[bool] | None = None) -> str:
    """Read a single keypress (including arrow keys) from a TTY using unbuffered os.read."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            if resize_flag and resize_flag[0]:
                resize_flag[0] = False
                return "RESIZE"
            r, _, _ = select.select([fd], [], [], 0.15)
            if not r:
                continue
            b = os.read(fd, 1)
            if not b:
                return "ESC"
            if b == b"\x1b":
                r2, _, _ = select.select([fd], [], [], 0.08)
                if not r2:
                    return "ESC"
                seq = os.read(fd, 1)
                if seq in (b"[", b"O"):
                    r3, _, _ = select.select([fd], [], [], 0.08)
                    if not r3:
                        return "ESC"
                    code = os.read(fd, 1)
                    if code == b"A":
                        return "UP"
                    if code == b"B":
                        return "DOWN"
                    if code == b"5":
                        os.read(fd, 1)
                        return "PGUP"
                    if code == b"6":
                        os.read(fd, 1)
                        return "PGDN"
                    # Drain any remaining CSI params (e.g. 1;2B)
                    while code and code < b"@":
                        r_more, _, _ = select.select([fd], [], [], 0.02)
                        if not r_more:
                            break
                        code = os.read(fd, 1)
                        if code == b"A":
                            return "UP"
                        if code == b"B":
                            return "DOWN"
                return "OTHER"
            if b in (b"\r", b"\n"):
                return "ENTER"
            if b == b"\x03":
                raise KeyboardInterrupt
            if b in (b"k", b"K"):
                return "UP"
            if b in (b"j", b"J"):
                return "DOWN"
            if b in (b"q", b"Q"):
                return "ESC"
            return b.decode("utf-8", errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _arrow_select(
    con: Console,
    title: str,
    items: list[tuple[str, str, str]],
    banner_fn: Callable[[], None] | None = None,
    page_size: int = 14,
) -> str | None:
    """Interactive arrow-key selector with non-TTY fallback.

    items: list of (value, label, hint)
    Returns selected value or None if cancelled.
    """
    if not items:
        return None

    if not sys.stdin.isatty() or termios is None or tty is None:
        con.clear()
        if banner_fn:
            banner_fn()
        table = Table(title=title)
        table.add_column("#", style="dim", width=4)
        table.add_column("Option", style="cyan")
        table.add_column("Info", style="dim")
        for idx, (_val, label, hint) in enumerate(items, start=1):
            table.add_row(str(idx), label, hint)
        con.print(table)
        choice = Prompt.ask("Select # or value (or 'b' back)", console=con, default="b").strip()
        if choice.lower() in ("b", "back", "q", ""):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            return items[int(choice) - 1][0]
        for val, _label, _hint in items:
            if choice == val:
                return val
        return choice

    cursor = 0
    total = len(items)
    resize_flag = [False]

    def _on_winch(_signum: int, _frame: Any) -> None:
        resize_flag[0] = True

    nav_hint = t(str(load_config().get("agent", {}).get("language", "en")), "nav_hint")
    prev_winch = signal.signal(signal.SIGWINCH, _on_winch) if hasattr(signal, "SIGWINCH") else None
    sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")
    sys.stdout.flush()
    try:
        while True:
            term = shutil.get_terminal_size((80, 24))
            dyn_page = max(3, min(page_size, term.lines - 10))
            max_label = max(16, term.columns - 6)

            start = max(0, min(cursor - dyn_page // 2, max(0, total - dyn_page)))
            end = min(total, start + dyn_page)
            shown_count = end - start
            est_height = shown_count + (8 if banner_fn else 4)
            top_pad = max(0, (term.lines - est_height) // 2)

            buf = StringIO()
            frame_con = Console(
                file=buf,
                force_terminal=True,
                width=term.columns,
                color_system=con.color_system or "truecolor",
            )
            if top_pad:
                frame_con.print("\n" * top_pad, end="")

            if banner_fn:
                old_file = con.file
                try:
                    con.file = buf
                    banner_fn()
                finally:
                    con.file = old_file

            table = Table(
                title=f"{title} ({cursor + 1}/{total})",
                box=None,
                show_header=False,
                pad_edge=False,
            )
            table.add_column("", width=2, no_wrap=True)
            table.add_column("Option", no_wrap=True)
            table.add_column("Info", style="dim", no_wrap=True)

            for idx in range(start, end):
                _val, label, hint = items[idx]
                short_label = label if len(label) <= max_label else label[: max_label - 1] + "…"
                avail_hint = max(0, term.columns - len(short_label) - 6)
                short_hint = hint if len(hint) <= avail_hint else (hint[: max(0, avail_hint - 1)] + "…" if avail_hint > 3 else "")
                if idx == cursor:
                    table.add_row("[bold cyan]>[/bold cyan]", f"[bold reverse cyan] {short_label} [/bold reverse cyan]", short_hint)
                else:
                    table.add_row(" ", short_label, short_hint)

            frame_con.print(table)
            frame_con.print(f"\n[dim]{nav_hint}[/dim]")

            lines = buf.getvalue().splitlines()[: term.lines]
            frame_out = "\r\n".join(f"{ln}\x1b[K" for ln in lines)
            sys.stdout.write(f"\x1b[?2026h\x1b[H{frame_out}\x1b[J\x1b[?2026l")
            sys.stdout.flush()

            key = _read_key(resize_flag)
            if key == "RESIZE":
                continue
            if key == "UP":
                cursor = (cursor - 1) % total
            elif key == "DOWN":
                cursor = (cursor + 1) % total
            elif key == "PGUP":
                cursor = max(0, cursor - dyn_page)
            elif key == "PGDN":
                cursor = min(total - 1, cursor + dyn_page)
            elif key == "ENTER":
                return items[cursor][0]
            elif key == "ESC":
                return None
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        if prev_winch is not None:
            signal.signal(signal.SIGWINCH, prev_winch)


def _pick_provider(con: Console, cfg: dict[str, Any]) -> str | None:
    providers: dict[str, dict[str, Any]] = cfg.get("providers", {})
    items: list[tuple[str, str, str]] = []
    for pid, p in providers.items():
        has_key = bool(resolve_api_key(p)) or not p.get("requires_key", True)
        badge = "key:OK" if has_key else "key:--"
        preview = ", ".join(p.get("models", [])[:2])
        items.append((pid, f"{p.get('name', pid)} ({pid})", f"{badge} | {preview}"))

    return _arrow_select(
        con,
        title=f"AI Providers ({len(items)} OpenAI-Compatible)",
        items=items,
        banner_fn=lambda: _render_banner(con, cfg),
    )


def _pick_or_enter_model(con: Console, cfg: dict[str, Any], provider_id: str) -> str | None:
    providers = cfg.get("providers", {})
    p = providers.get(provider_id, {})
    models: list[str] = list(p.get("models", []))
    api_key = resolve_api_key(p, provider_id=provider_id)
    base_url = str(p.get("base_url", ""))
    if provider_id == "openai_codex_oauth":
        base_url = "https://chatgpt.com/backend-api/codex"
    if base_url and api_key:
        live_models = fetch_provider_models(base_url, api_key, timeout=4.0)
        if live_models:
            merged = list(live_models)
            for m in models:
                if m not in merged:
                    merged.append(m)
            models = merged

    items: list[tuple[str, str, str]] = [(m, m, "model") for m in models]
    items.append(("__custom__", "+ Enter custom model name...", "manual input"))

    selected = _arrow_select(
        con,
        title=f"Models for {p.get('name', provider_id)}",
        items=items,
        banner_fn=lambda: _render_banner(con, cfg),
    )
    if selected is None:
        return None
    if selected == "__custom__":
        custom = Prompt.ask("Enter custom model name", console=con, default="").strip()
        if not custom:
            return None
        selected = custom

    if selected not in models:
        models.append(selected)
        p["models"] = models
    return selected


def _configure_provider_details(con: Console, cfg: dict[str, Any]) -> None:
    pid = _pick_provider(con, cfg)
    if not pid:
        return
    p = cfg["providers"][pid]

    while True:
        has_key = bool(resolve_api_key(p))
        is_oauth = p.get("auth_type") == "oauth"
        key_label = "OAuth Login / Token" if is_oauth else "Set API Key"
        actions = [
            ("key", key_label, "connected" if has_key else "not connected"),
            ("url", "Set Base URL", str(p.get("base_url", ""))),
            ("toggle", "Toggle Enabled", "enabled" if p.get("enabled") else "disabled"),
            ("model", "Add Custom Model", f"{len(p.get('models', []))} models"),
            ("back", "Back", ""),
        ]
        choice = _arrow_select(
            con,
            title=f"Provider: {p.get('name', pid)} ({pid})",
            items=actions,
            banner_fn=lambda: _render_banner(con, cfg),
        )
        if choice in (None, "back"):
            break
        if choice == "key":
            if is_oauth:
                import secrets as _secrets

                profile = OAUTH_PROFILES.get(pid, {})
                cb_path = str(profile.get("callback_path", "/callback"))
                cb_host = str(profile.get("callback_host", "127.0.0.1"))
                cb_port = int(profile.get("callback_port", 0))
                verifier, challenge = generate_pkce_pair()
                state = _secrets.token_urlsafe(16)
                server, redirect_uri, cb_res, cb_done = start_loopback_callback_server(
                    callback_path=cb_path,
                    expected_state=state,
                    host=cb_host,
                    port=cb_port,
                )
                try:
                    oauth_url = build_oauth_authorize_url(
                        pid,
                        redirect_uri=redirect_uri,
                        state=state,
                        code_challenge=challenge,
                    )
                    con.print(f"[cyan]OAuth PKCE URL:[/cyan] {oauth_url}")
                    con.print(f"[dim]Loopback callback listening on {redirect_uri}[/dim]")
                    action = Prompt.ask(
                        "Press [bold]o[/bold] to open browser, [bold]c[/bold] to copy link, or [bold]Enter[/bold] to paste callback URL/token",
                        console=con,
                        default="o",
                    ).strip().lower()
                    if action == "o":
                        if _open_url(oauth_url):
                            con.print("[green]Opened OAuth URL in browser. Waiting up to 120s for callback...[/green]")
                            cb_done.wait(timeout=120.0)
                        else:
                            con.print("[yellow]Could not open browser automatically. Copy the URL above.[/yellow]")
                    elif action == "c":
                        if _copy_to_clipboard(oauth_url):
                            con.print("[green]OAuth URL copied to clipboard. Waiting up to 120s for callback...[/green]")
                            cb_done.wait(timeout=120.0)
                        else:
                            con.print("[yellow]Clipboard unavailable. Copy the URL above manually.[/yellow]")

                    if "code" in cb_res:
                        try:
                            creds = exchange_oauth_code(pid, cb_res["code"], redirect_uri, verifier)
                            key_val = str(creds.get("access_token", ""))
                            con.print("[green]OAuth PKCE login succeeded![/green]")
                        except Exception as exc:
                            con.print(f"[yellow]Token exchange failed ({exc}). Paste access token manually:[/yellow]")
                            key_val = Prompt.ask("Paste OAuth Access Token", console=con, default="").strip()
                    else:
                        pasted = Prompt.ask(
                            "Paste callback URL or OAuth Access Token (leave empty to clear)",
                            console=con,
                            default="",
                        ).strip()
                        key_val = ""
                        if pasted:
                            if "code=" in pasted:
                                import urllib.parse as _up
                                parsed_cb = _up.urlparse(pasted)
                                qs_cb = _up.parse_qs(parsed_cb.query or pasted)
                                code_val = qs_cb.get("code", [""])[0]
                                if code_val:
                                    try:
                                        creds = exchange_oauth_code(pid, code_val, redirect_uri, verifier)
                                        key_val = str(creds.get("access_token", ""))
                                        con.print("[green]OAuth PKCE login succeeded![/green]")
                                    except Exception as exc:
                                        con.print(f"[yellow]Token exchange failed ({exc}).[/yellow]")
                            if not key_val:
                                key_val = pasted
                                save_oauth_credentials(pid, {"access_token": key_val})
                finally:
                    server.shutdown()
                    server.server_close()
            else:
                key_val = Prompt.ask("Enter API Key (leave empty to clear)", console=con, default="").strip()
            p["api_key"] = key_val
            p["enabled"] = True if key_val else p.get("enabled", False)
            save_config(cfg)
        elif choice == "url":
            url_val = Prompt.ask("Enter Base URL", console=con, default=str(p.get("base_url", ""))).strip()
            if url_val:
                p["base_url"] = url_val
                save_config(cfg)
        elif choice == "toggle":
            p["enabled"] = not bool(p.get("enabled", False))
            save_config(cfg)
        elif choice == "model":
            custom_m = Prompt.ask("Enter custom model ID", console=con, default="").strip()
            if custom_m:
                models = list(p.get("models", []))
                if custom_m not in models:
                    models.append(custom_m)
                    p["models"] = models
                    save_config(cfg)


def _manage_gateways(con: Console, cfg: dict[str, Any]) -> None:
    gateways: dict[str, dict[str, Any]] = cfg.get("gateways", {})

    while True:
        items: list[tuple[str, str, str]] = []
        for gname, gw in gateways.items():
            state = "ON" if gw.get("enabled") else "OFF"
            detail = gw.get("path") or gw.get("token_env") or ""
            items.append((gname, f"{gname} [{state}]", f"{gw.get('type', gname)} | {detail}"))
        items.append(("__back__", "Back", ""))

        target = _arrow_select(
            con,
            title="Manage Gateways (Enter to toggle)",
            items=items,
            banner_fn=lambda: _render_banner(con, cfg),
        )
        if target in (None, "__back__"):
            break
        if target in gateways:
            gw = gateways[target]
            gw["enabled"] = not bool(gw.get("enabled", False))
            if "token" in gw and gw["enabled"]:
                tok = Prompt.ask(
                    f"Token for {target} (optional, or use {gw.get('token_env')})",
                    console=con,
                    default=str(gw.get("token", "")),
                ).strip()
                gw["token"] = tok
            save_config(cfg)


def _agent_runtime_settings(con: Console, cfg: dict[str, Any]) -> None:
    con.clear()
    _render_banner(con, cfg)
    agent = cfg.setdefault("agent", {})
    rt = cfg.setdefault("runtime", {})

    agent["name"] = Prompt.ask("Agent name", console=con, default=str(agent.get("name", "hirox"))).strip()
    agent["system_prompt"] = Prompt.ask(
        "System prompt",
        console=con,
        default=str(agent.get("system_prompt", "You are Hirox, a helpful multi-gateway AI agent.")),
    ).strip()
    rt["host"] = Prompt.ask("Runtime host", console=con, default=str(rt.get("host", "127.0.0.1"))).strip()
    port_str = Prompt.ask("Runtime port", console=con, default=str(rt.get("port", 8765))).strip()
    if port_str.isdigit():
        rt["port"] = int(port_str)
    save_config(cfg)


def run_config_tui(console: Console | None = None) -> None:
    """Run the interactive arrow-key Hirox Config TUI."""
    standalone = console is None and sys.stdin.isatty() and sys.stdout.isatty()
    con = console or Console()
    cfg = load_config()

    menu_items = [
        ("1", "Select Active Provider & Model", "40+ providers, list or custom model"),
        ("2", "Configure Provider API Key / OAuth / URL", "API keys, OAuth login, endpoints, models"),
        ("3", "Manage Gateways", "CLI, Webhook, Telegram, Discord, Slack, ..."),
        ("4", "Agent & Runtime Settings", "name, system prompt, host, port"),
        ("0", "Save & Exit", str(get_config_path())),
    ]

    if standalone:
        sys.stdout.write("\x1b[?1049h\x1b[H\x1b[2J")
        sys.stdout.flush()

    try:
        while True:
            try:
                choice = _arrow_select(
                    con,
                    title="Main Menu",
                    items=menu_items,
                    banner_fn=lambda: _render_banner(con, cfg),
                )
            except (EOFError, KeyboardInterrupt):
                save_config(cfg)
                con.print("\n[dim]Config saved.[/dim]")
                break

            if choice in (None, "0", "q", "exit"):
                save_config(cfg)
                con.print(f"[green]Saved config to[/green] {get_config_path()}")
                break
            if choice == "1":
                pid = _pick_provider(con, cfg)
                if pid:
                    model = _pick_or_enter_model(con, cfg, pid)
                    if model:
                        cfg.setdefault("agent", {})["provider"] = pid
                        cfg["agent"]["model"] = model
                        cfg["providers"][pid]["enabled"] = True
                        save_config(cfg)
            elif choice == "2":
                _configure_provider_details(con, cfg)
            elif choice == "3":
                _manage_gateways(con, cfg)
            elif choice == "4":
                _agent_runtime_settings(con, cfg)
    finally:
        if standalone:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
