"""Tests for Hirox CLI, providers, config TUI, and chat TUI."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from hirox.cli import main
from hirox.config import load_config
from hirox.providers import PROVIDER_CATALOG


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "start" in result.output
    assert "doctor" in result.output
    assert "config" in result.output
    assert "chat" in result.output


def test_provider_catalog_count_and_openai_compat() -> None:
    assert len(PROVIDER_CATALOG) >= 35
    for pid, p in PROVIDER_CATALOG.items():
        assert p["openai_compatible"] is True, f"{pid} must be openai_compatible"
        assert p["base_url"], f"{pid} missing base_url"
        assert isinstance(p["models"], list) and len(p["models"]) >= 1


def test_config_lifecycle(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))

    runner = CliRunner()

    res_init = runner.invoke(main, ["config", "init"])
    assert res_init.exit_code == 0
    assert cfg_file.exists()

    res_set = runner.invoke(main, ["config", "set", "runtime.port", "9090"])
    assert res_set.exit_code == 0

    res_get = runner.invoke(main, ["config", "get", "runtime.port"])
    assert res_get.exit_code == 0
    assert res_get.output.strip() == "9090"

    res_show = runner.invoke(main, ["config", "show"])
    assert res_show.exit_code == 0
    data = json.loads(res_show.output)
    assert data["runtime"]["port"] == 9090
    assert len(data["providers"]) >= 35


def test_config_tui_select_provider_and_custom_model(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))

    runner = CliRunner()
    # Menu 1 -> select 'groq' -> enter custom model 'my-custom-llama' -> 0 Save & Exit
    result = runner.invoke(main, ["config"], input="1\ngroq\nmy-custom-llama\n0\n")
    assert result.exit_code == 0
    assert "Hirox Config TUI" in result.output

    cfg = load_config(cfg_file)
    assert cfg["agent"]["provider"] == "groq"
    assert cfg["agent"]["model"] == "my-custom-llama"
    assert "my-custom-llama" in cfg["providers"]["groq"]["models"]


def test_doctor_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(tmp_path / "config.json"))

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0
    checks = json.loads(result.output)
    names = {c["name"] for c in checks}
    assert {"python", "dep:click", "dep:rich", "home_dir", "config", "gateways"} <= names


def test_start_dry_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(tmp_path / "config.json"))

    runner = CliRunner()
    result = runner.invoke(main, ["start", "--dry-run"])
    assert result.exit_code == 0
    assert "cli" in result.output


def test_chat_tui_command_preview_and_model_switch(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))

    runner = CliRunner()
    # /m triggers command preview for /model; /model -> custom model; /exit quits
    result = runner.invoke(
        main,
        ["chat"],
        input="/m\n/model\ncustom-gpt-test\n/exit\n",
    )
    assert result.exit_code == 0
    assert "◆ Hirox" in result.output
    assert "Command Preview" in result.output
    assert "/model" in result.output
    assert "Goodbye." in result.output

    cfg = load_config(cfg_file)
    assert cfg["agent"]["model"] == "custom-gpt-test"


def test_chat_fast_provider_and_model_args(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat"],
        input="/provider groq\n/model llama-fast-custom\n/exit\n",
    )
    assert result.exit_code == 0
    cfg = load_config(cfg_file)
    assert cfg["agent"]["provider"] == "groq"
    assert cfg["agent"]["model"] == "llama-fast-custom"


def test_system_prompt_and_memory_md(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    from hirox.memory import append_memory, build_system_prompt, extract_memory_writes

    append_memory("User prefers Turkish responses")
    prompt = build_system_prompt(None, gateway="cli", provider_id="openai", model="gpt-4o-mini")
    assert "Live Runtime Context" in prompt
    assert "Architecture:" in prompt
    assert "OS:" in prompt
    assert "Local Time:" in prompt
    assert "User prefers Turkish responses" in prompt

    cleaned, entries = extract_memory_writes(
        "Tamam, not ettim. <memory_write>Project uses Python 3.14</memory_write>"
    )
    assert cleaned == "Tamam, not ettim."
    assert entries == ["Project uses Python 3.14"]

    # Sensitive secrets / SSH keys must be blocked
    _, blocked = extract_memory_writes(
        "<memory_write>ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest</memory_write>"
        "<memory_write>api_key=sk-1234567890abcdef123456</memory_write>"
    )
    assert blocked == []


def test_chat_ai_memory_write_visible_in_chat(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))
    monkeypatch.chdir(tmp_path)

    import hirox.tui as tui_mod

    monkeypatch.setattr(
        tui_mod,
        "chat_completion",
        lambda **_kwargs: "Anladim! <memory_write>User name is HiroxDev</memory_write>",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="Benim adim HiroxDev\n/memory\n/exit\n")
    assert result.exit_code == 0
    assert "Memory saved:" in result.output
    assert "User name is HiroxDev" in result.output
    assert (tmp_path / "MEMORY.md").exists()


def test_chat_ai_session_naming_and_resume(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))
    monkeypatch.chdir(tmp_path)

    import hirox.tui as tui_mod
    from hirox.sessions import list_sessions

    monkeypatch.setattr(
        tui_mod,
        "chat_completion",
        lambda **_kwargs: "Merhaba! <session_title>Python Gateway Setup</session_title>",
    )

    runner = CliRunner()
    res1 = runner.invoke(main, ["chat"], input="Selam\n/exit\n")
    assert res1.exit_code == 0
    assert "Session named: Python Gateway Setup" in res1.output

    sessions = list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Python Gateway Setup"

    # Resume session #1 in a new chat invocation
    res2 = runner.invoke(main, ["chat"], input="/resume 1\n/exit\n")
    assert res2.exit_code == 0
    assert "Python Gateway Setup" in res2.output
    assert "Merhaba!" in res2.output


def test_permission_modes_and_2x_confirm(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))

    from hirox.tools import execute_tool

    # read_only blocks run_command & write_file, allows list_dir
    assert "Permission denied" in execute_tool("run_command", {"command": "echo hi"}, "read_only")
    assert "Permission denied" in execute_tool("write_file", {"path": "a.txt", "content": "x"}, "read_only")
    assert "Error:" not in execute_tool("list_dir", {"path": str(tmp_path)}, "read_only")

    # ask mode denies when confirm_fn returns False, allows when True
    assert "Permission denied" in execute_tool(
        "run_command", {"command": "echo hi"}, "ask", confirm_fn=lambda _d: False
    )
    assert "exit_code=0" in execute_tool(
        "run_command", {"command": "echo hi"}, "ask", confirm_fn=lambda _d: True
    )

    runner = CliRunner()
    # Declining confirmation cancels full_access
    res_fail = runner.invoke(main, ["chat"], input="/permission full_access\nn\n/exit\n")
    assert res_fail.exit_code == 0
    assert load_config(cfg_file)["agent"]["permission_mode"] == "ask"

    # Single y confirmation enables full_access
    res_ok = runner.invoke(main, ["chat"], input="/permission full_access\ny\n/exit\n")
    assert res_ok.exit_code == 0
    assert load_config(cfg_file)["agent"]["permission_mode"] == "full_access"


def test_agentic_tool_loop_in_chat(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))
    (tmp_path / "hello.txt").write_text("hirox-content", encoding="utf-8")

    import hirox.tui as tui_mod

    calls = {"n": 0}

    def _fake_tools(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": str(tmp_path / "hello.txt")}),
                        },
                    }
                ],
            }
        return {"content": "Dosyada hirox-content yaziyor."}

    monkeypatch.setattr(tui_mod, "chat_completion_with_tools", _fake_tools)

    runner = CliRunner()
    res = runner.invoke(main, ["chat"], input="hello.txt oku\n/exit\n")
    assert res.exit_code == 0
    assert calls["n"] == 2
    assert "Read" in res.output
    assert "hello.txt" in res.output
    assert "hirox-content" in res.output

    # Verify write_file diff rendering (+added / -removed)
    from io import StringIO
    from rich.console import Console
    buf = StringIO()
    test_con = Console(file=buf, force_terminal=False, width=100)
    tui_mod._render_tool_call(
        test_con,
        "write_file",
        {"path": str(tmp_path / "hello.txt"), "content": "new-line\n"},
    )
    diff_out = buf.getvalue()
    assert "Write" in diff_out
    assert "+1 added" in diff_out
    assert "-1 removed" in diff_out


def test_usage_command_and_context_indicator(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))

    import hirox.tui as tui_mod

    monkeypatch.setattr(
        tui_mod,
        "chat_completion_with_tools",
        lambda **_kw: {
            "content": "Merhaba!",
            "_usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
        },
    )

    runner = CliRunner()
    res = runner.invoke(main, ["chat"], input="selam\n/usage\n/exit\n")
    assert res.exit_code == 0
    assert "ctx [" in res.output
    assert "● Usage" in res.output
    assert "1,500" in res.output
    assert "API reported" in res.output


def test_language_command_15_languages(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))

    from hirox.i18n import LANGUAGES

    assert len(LANGUAGES) == 15

    runner = CliRunner()
    res = runner.invoke(main, ["chat"], input="/language tr\n/exit\n")
    assert res.exit_code == 0
    assert load_config(cfg_file)["agent"]["language"] == "tr"
    assert "Görüşürüz." in res.output


def test_web_search_and_web_fetch_tools(monkeypatch) -> None:
    from hirox.tools import execute_tool
    import hirox.tools as tools_mod

    class _FakeResp:
        def __init__(self, body: str) -> None:
            self._body = body.encode("utf-8")

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    ddg_html = (
        '<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fhirox">'
        "Hirox Docs</a>"
        '<a class="result__snippet">Multi-gateway AI agent</a>'
    )
    page_html = "<html><head><script>bad()</script></head><body><h1>Hello Hirox</h1><p>World</p></body></html>"

    def _fake_urlopen(req, timeout=15):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "duckduckgo.com" in url:
            return _FakeResp(ddg_html)
        return _FakeResp(page_html)

    monkeypatch.setattr(tools_mod.urllib.request, "urlopen", _fake_urlopen)

    s_res = execute_tool("web_search", {"query": "hirox"}, "read_only")
    assert "Hirox Docs" in s_res
    assert "https://example.com/hirox" in s_res
    assert "Multi-gateway AI agent" in s_res

    f_res = execute_tool("web_fetch", {"url": "https://example.com/hirox"}, "read_only")
    assert "Hello Hirox" in f_res
    assert "bad()" not in f_res


def test_webhook_gateway_live_http_and_registry(tmp_path: Path, monkeypatch) -> None:
    import urllib.request
    from hirox.config import load_config
    from hirox.runtime import HiroxRuntime
    import hirox.runtime as rt_mod

    cfg = load_config(tmp_path / "config.json")
    cfg["runtime"]["port"] = 0  # ephemeral free port
    cfg["gateways"]["webhook"]["enabled"] = True

    monkeypatch.setattr(
        rt_mod,
        "chat_completion_with_tools",
        lambda **_kw: {"content": "Webhook AI reply!"},
    )

    rt = HiroxRuntime(config=cfg)
    statuses = rt.start(gateways=["webhook"])
    try:
        assert len(statuses) == 1
        assert statuses[0].running is True
        wh = rt.registry.gateways["webhook"]
        port = wh.port  # type: ignore[attr-defined]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            health = json.loads(r.read().decode("utf-8"))
            assert health["status"] == "ok"

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=json.dumps({"message": "hello", "sender": "tester"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read().decode("utf-8"))
            assert body["reply"] == "Webhook AI reply!"
    finally:
        rt.stop()


def test_chat_connects_to_running_start_gateway(tmp_path: Path, monkeypatch) -> None:
    from hirox.config import load_config, save_config
    from hirox.runtime import HiroxRuntime

    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("HIROX_HOME", str(tmp_path))
    monkeypatch.setenv("HIROX_CONFIG", str(cfg_file))

    cfg = load_config(cfg_file)
    cfg["runtime"]["port"] = 0
    rt = HiroxRuntime(config=cfg)
    rt.start()
    try:
        port = rt.registry.gateways["webhook"].port  # type: ignore[attr-defined]
        cfg["runtime"]["port"] = port
        save_config(cfg, cfg_file)

        runner = CliRunner()
        res = runner.invoke(main, ["chat"], input="/gateways\n/exit\n")
        assert res.exit_code == 0
        assert "cli (connected)" in res.output
        assert "Live Gateways" in res.output
        # After /exit, client should be disconnected
        snap = rt.get_health_snapshot()
        assert snap["active_clients"] == 0
    finally:
        rt.stop()


def test_agent_workflow_plan_and_system_prompt() -> None:
    from hirox.memory import build_system_prompt
    from hirox.tools import execute_tool

    sp = build_system_prompt(None, gateway="cli", provider_id="openai", model="gpt-4o-mini")
    assert "Autonomous Agent Workflow" in sp
    assert "workflow_plan" in sp

    res1 = execute_tool(
        "workflow_plan",
        {"action": "set", "goal": "Build feature", "steps": ["Inspect", "Implement", "Test"]},
        "read_only",
    )
    assert "1. [▶] Inspect" in res1
    assert "0/3 completed" in res1

    res2 = execute_tool(
        "workflow_plan",
        {"action": "update", "step_index": 1, "status": "completed", "note": "done"},
        "read_only",
    )
    assert "1. [✓] Inspect — done" in res2
    assert "2. [▶] Implement" in res2
    assert "1/3 completed" in res2

    assert "cleared" in execute_tool("workflow_plan", {"action": "clear"}, "read_only")


def test_oauth_pkce_loopback_and_model_discovery(tmp_path: Path, monkeypatch) -> None:
    import urllib.request
    from hirox.oauth import (
        build_oauth_authorize_url,
        fetch_provider_models,
        generate_pkce_pair,
        get_valid_oauth_token,
        save_oauth_credentials,
        start_loopback_callback_server,
    )
    import hirox.oauth as oauth_mod

    monkeypatch.setenv("HIROX_HOME", str(tmp_path))

    verifier, challenge = generate_pkce_pair()
    assert len(verifier) >= 40 and len(challenge) >= 40

    server, redirect_uri, cb_res, cb_done = start_loopback_callback_server(
        callback_path="/auth/callback",
        expected_state="st123",
    )
    try:
        url = build_oauth_authorize_url("openai_codex_oauth", redirect_uri, "st123", challenge)
        assert "code_challenge_method=S256" in url
        assert "app_EMoamEEZ73f0CkXaXp7hrann" in url

        with urllib.request.urlopen(f"{redirect_uri}?code=authcode99&state=st123", timeout=5) as r:
            assert r.status == 200
        assert cb_done.wait(timeout=2.0)
        assert cb_res["code"] == "authcode99"
    finally:
        server.shutdown()
        server.server_close()

    save_oauth_credentials("antigravity_oauth", {"access_token": "ag-tok-1", "expires_at": 9999999999})
    assert get_valid_oauth_token("antigravity_oauth") == "ag-tok-1"

    class _FakeModelsResp:
        def read(self) -> bytes:
            return json.dumps({"data": [{"id": "gpt-5.3-codex"}, {"id": "o3"}]}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(oauth_mod.urllib.request, "urlopen", lambda *_a, **_kw: _FakeModelsResp())
    models = fetch_provider_models("https://api.openai.com/v1", "tok")
    assert models == ["gpt-5.3-codex", "o3"]


def test_tool_error_handling_timeout_and_retries(tmp_path: Path) -> None:
    from hirox.memory import DEFAULT_SYSTEM_PROMPT
    from hirox.tools import execute_tool

    # Timeout enforcement
    res_timeout = execute_tool(
        "run_command",
        {"command": "sleep 3", "timeout": 1},
        permission_mode="full_access",
    )
    assert "timed out after 1s" in res_timeout

    # Retry policy: fail on first attempt, succeed on second
    marker = tmp_path / "retry_ok.txt"
    cmd = (
        f"python3 -c \"import pathlib; p=pathlib.Path('{marker}'); "
        "import sys; "
        "sys.exit(0) if p.exists() else (p.write_text('1'), sys.exit(1))\""
    )
    res_retry = execute_tool(
        "run_command",
        {"command": cmd, "retries": 1},
        permission_mode="full_access",
    )
    assert res_retry.startswith("exit_code=0")

    # Retry exhaustion reports attempt count
    res_fail = execute_tool(
        "run_command",
        {"command": "python3 -c 'import sys; sys.exit(2)'", "retries": 1},
        permission_mode="full_access",
    )
    assert "exit_code=2" in res_fail
    assert "failed after 2 attempts" in res_fail

    # Invalid argument types are caught cleanly
    res_bad_arg = execute_tool(
        "read_file",
        {"path": str(marker), "start_line": "not-an-int"},
        permission_mode="read_only",
    )
    assert res_bad_arg.startswith("Error: read_file failed:")

    # System prompt documents retry & timeout rules
    assert "Tool Error Handling, Retries & Timeouts:" in DEFAULT_SYSTEM_PROMPT

