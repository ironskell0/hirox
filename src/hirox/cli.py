"""Command-line interface for Hirox."""

from __future__ import annotations

import json
import sys

import click
from rich.console import Console
from rich.table import Table

from hirox import __version__
from hirox.config import (
    get_config_path,
    get_value,
    init_config,
    load_config,
    set_value,
)
from hirox.config_tui import run_config_tui
from hirox.doctor import run_diagnostics
from hirox.runtime import HiroxRuntime
from hirox.security import compute_pairing_code
from hirox.tui import run_chat_tui

console = Console()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="hirox")
def main() -> None:
    """Hirox - Python-based multi-gateway AI agent."""


@main.command()
@click.option(
    "--gateway",
    "-g",
    "gateways",
    multiple=True,
    help="Specific gateway(s) to start (defaults to all enabled gateways).",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help="Override runtime port.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Initialize runtime and gateways, print status, and exit immediately.",
)
def start(gateways: tuple[str, ...], port: int | None, dry_run: bool) -> None:
    """Start the Hirox agent runtime and gateways."""
    cfg = load_config()
    if port is not None:
        cfg.setdefault("runtime", {})["port"] = port

    runtime = HiroxRuntime(config=cfg)
    selected = list(gateways) if gateways else None

    if dry_run:
        statuses = runtime.registry.statuses()
        table = Table(title="Hirox Runtime (dry-run)")
        table.add_column("Gateway", style="cyan")
        table.add_column("Type")
        table.add_column("Status")
        for st in statuses:
            if st.enabled or (selected and st.name in selected):
                table.add_row(st.name, st.gateway_type, "ready (dry-run)")
        if not statuses:
            console.print("[yellow]No gateways enabled to start.[/yellow]")
        else:
            console.print(table)
        return

    rt_cfg = cfg.get("runtime", {})
    console.print(
        f"[bold cyan]Starting Hirox[/bold cyan] v{__version__} "
        f"on {rt_cfg.get('host', '127.0.0.1')}:{rt_cfg.get('port', 8765)}"
    )
    console.print("[dim]Press Ctrl+C to stop.[/dim]")
    runtime.run_forever(gateways=selected)
    console.print("[dim]Hirox stopped.[/dim]")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Output diagnostics as JSON.")
def doctor(as_json: bool) -> None:
    """Run environment, config, and gateway diagnostics."""
    results = run_diagnostics()
    has_fail = any(r.status == "fail" for r in results)

    if as_json:
        payload = [
            {"name": r.name, "status": r.status, "detail": r.detail}
            for r in results
        ]
        click.echo(json.dumps(payload, indent=2))
        if has_fail:
            sys.exit(1)
        return

    table = Table(title="Hirox Doctor")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Detail")

    style_map = {
        "ok": "[green]OK[/green]",
        "warn": "[yellow]WARN[/yellow]",
        "fail": "[red]FAIL[/red]",
    }
    for r in results:
        table.add_row(r.name, style_map.get(r.status, r.status), r.detail)

    console.print(table)
    if has_fail:
        sys.exit(1)


@main.command()
@click.argument("challenge")
def pair(challenge: str) -> None:
    """Compute the 8-character verification code for a gateway pairing challenge."""
    code = compute_pairing_code(challenge)
    click.echo(code)


@main.group(invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """Inspect and manage Hirox configuration (opens Config TUI by default)."""
    if ctx.invoked_subcommand is None:
        run_config_tui(console=console)


@config.command("tui")
def config_tui() -> None:
    """Launch the interactive Hirox Config TUI."""
    run_config_tui(console=console)


@config.command("path")
def config_path() -> None:
    """Print the active configuration file path."""
    click.echo(str(get_config_path()))


@config.command("init")
@click.option("--force", is_flag=True, help="Overwrite existing config file.")
def config_init(force: bool) -> None:
    """Create a default Hirox configuration file."""
    path, created = init_config(force=force)
    if created:
        console.print(f"[green]Initialized config:[/green] {path}")
    else:
        console.print(f"[yellow]Config already exists:[/yellow] {path}")


@config.command("show")
def config_show() -> None:
    """Display the effective Hirox configuration."""
    cfg = load_config()
    click.echo(json.dumps(cfg, indent=2, ensure_ascii=False))


@config.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Get a configuration value by dotted key (e.g. runtime.port)."""
    try:
        val = get_value(key)
    except KeyError:
        console.print(f"[red]Key not found:[/red] {key}")
        sys.exit(1)
    if isinstance(val, (dict, list)):
        click.echo(json.dumps(val, indent=2, ensure_ascii=False))
    else:
        click.echo(str(val))


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a configuration value by dotted key (e.g. runtime.port 9000)."""
    path, parsed = set_value(key, value)
    console.print(f"[green]Updated[/green] {key} = {parsed!r} ({path})")


@main.command()
@click.option("--gateway", "-g", default="cli", help="Gateway channel for chat.")
@click.option("--tui/--no-tui", default=True, help="Use interactive TUI mode.")
def chat(gateway: str, tui: bool) -> None:
    """Start an interactive chat session with Hirox."""
    if tui:
        run_chat_tui(gateway=gateway, console=console)
    else:
        console.print(f"[cyan]Hirox plain chat ({gateway})[/cyan] - Ctrl+C to exit")
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if line in ("/exit", "/quit"):
                break
            if line:
                click.echo(f"hirox ({gateway})> {line}")
