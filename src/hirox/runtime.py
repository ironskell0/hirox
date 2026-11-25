"""Core runtime lifecycle for Hirox."""

from __future__ import annotations

import signal
import threading
import time
from typing import Any

from hirox.config import load_config
from hirox.gateway import GatewayRegistry, GatewayStatus, WebhookGateway, build_registry_from_config
from hirox.memory import build_system_prompt
from hirox.providers import chat_completion_with_tools, estimate_context_window, estimate_tokens
from hirox.security import log_security_event, wrap_untrusted_content
from hirox.tools import PERMISSION_MODES, TOOL_SCHEMAS, execute_tool

MAX_GATEWAY_TOOL_ITERATIONS = 12
MAX_GATEWAY_TURN_SECONDS = 180.0
MAX_CONCURRENT_GATEWAY_REQUESTS = 4


class HiroxRuntime:
    """Coordinates Hirox agent startup, gateways, and shutdown."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else load_config()
        self._started_at = time.monotonic()
        self._clients: dict[str, dict[str, Any]] = {}
        self.registry: GatewayRegistry = build_registry_from_config(
            self.config,
            message_handler=self.handle_gateway_message,
        )
        wh = self.registry.gateways.get("webhook")
        if isinstance(wh, WebhookGateway):
            wh.health_provider = self.get_health_snapshot
            wh.client_tracker = self._clients
        self._running = False
        self._concurrency_sem = threading.Semaphore(MAX_CONCURRENT_GATEWAY_REQUESTS)

    def get_health_snapshot(self) -> dict[str, Any]:
        """Return live health, gateway statuses, and connected chat clients."""
        now = time.time()
        stale = [cid for cid, info in self._clients.items() if now - float(info.get("last_seen", 0)) > 20.0]
        for cid in stale:
            self._clients.pop(cid, None)
        ag = self.config.get("agent", {})
        wh = self.registry.gateways.get("webhook")
        reqs = wh.requests_handled if isinstance(wh, WebhookGateway) else 0
        return {
            "status": "ok" if self._running else "stopped",
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "provider": str(ag.get("provider", "openai")),
            "model": str(ag.get("model", "gpt-4o-mini")),
            "active_clients": len(self._clients),
            "clients": list(self._clients.values()),
            "requests_handled": reqs,
            "gateways": [
                {
                    "name": st.name,
                    "type": st.gateway_type,
                    "enabled": st.enabled,
                    "running": st.running,
                    "detail": st.detail,
                }
                for st in self.registry.statuses()
            ],
        }

    def handle_gateway_message(self, gateway_name: str, sender: str, text: str) -> str:
        """Route an incoming gateway message through the Hirox AI agent with resource & permission guards."""
        if not self._concurrency_sem.acquire(timeout=5.0):
            log_security_event(
                "concurrency_limit_reached",
                f"Rejected message from {sender}: too many concurrent requests",
                gateway=gateway_name,
                severity="warn",
            )
            return "Error: server busy, please retry shortly."
        try:
            ag = self.config.get("agent", {})
            provider_id = str(ag.get("provider", "openai"))
            model = str(ag.get("model", "gpt-4o-mini"))
            language = str(ag.get("language", "en"))
            provider_cfg = self.config.get("providers", {}).get(provider_id)
            if not provider_cfg:
                return f"Error: unknown provider '{provider_id}'."

            gw_cfg = self.config.get("gateways", {}).get(gateway_name, {})
            gw_perm = str(gw_cfg.get("permission_mode", "read_only"))
            if gw_perm not in PERMISSION_MODES or (gateway_name != "cli" and gw_perm == "full_access"):
                log_security_event(
                    "gateway_permission_clamped",
                    f"Clamped gateway '{gateway_name}' permission_mode '{gw_perm}' to 'read_only'",
                    gateway=gateway_name,
                    severity="warn",
                )
                gw_perm = "read_only"

            sys_prompt = build_system_prompt(
                base_prompt=str(ag.get("system_prompt", "")),
                gateway=gateway_name,
                provider_id=provider_id,
                model=model,
                permission_mode=gw_perm,
                language=language,
            )
            safe_user_msg = wrap_untrusted_content(f"gateway:{gateway_name}:{sender}", text)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": safe_user_msg},
            ]

            ctx_limit = int(estimate_context_window(model) * 0.9)
            turn_deadline = time.monotonic() + MAX_GATEWAY_TURN_SECONDS
            seen_calls: dict[tuple[str, str], int] = {}

            for _iteration in range(MAX_GATEWAY_TOOL_ITERATIONS):
                if time.monotonic() > turn_deadline:
                    log_security_event("turn_timeout", "Gateway turn exceeded time limit", gateway=gateway_name, severity="warn")
                    return "Error: request timed out before completion."
                if estimate_tokens(messages) > ctx_limit:
                    log_security_event("token_budget_exceeded", "Context token budget exceeded", gateway=gateway_name, severity="warn")
                    return "Error: context token budget exceeded."

                msg_obj = chat_completion_with_tools(
                    provider_cfg=provider_cfg,
                    model=model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                )
                tool_calls = msg_obj.get("tool_calls") or []
                if not tool_calls:
                    return str(msg_obj.get("content") or "")
                messages.append(
                    {
                        "role": "assistant",
                        "content": str(msg_obj.get("content") or ""),
                        "tool_calls": tool_calls,
                    }
                )
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    t_name = str(fn.get("name", ""))
                    t_args = fn.get("arguments", {})
                    sig = (t_name, str(t_args))
                    seen_calls[sig] = seen_calls.get(sig, 0) + 1
                    if seen_calls[sig] >= 3:
                        log_security_event(
                            "tool_loop_detected",
                            f"Repeated tool call '{t_name}' aborted",
                            gateway=gateway_name,
                            severity="warn",
                        )
                        return f"Error: aborted repeated tool loop on '{t_name}'."
                    t_res = execute_tool(t_name, t_args, permission_mode=gw_perm)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(tc.get("id", t_name)),
                            "content": t_res,
                        }
                    )

            log_security_event("max_tool_iterations", "Exceeded max tool iterations", gateway=gateway_name, severity="warn")
            return "Error: exceeded maximum tool-call iterations."
        finally:
            self._concurrency_sem.release()

    def start(self, gateways: list[str] | None = None) -> list[GatewayStatus]:
        """Start enabled (or explicitly requested) gateways."""
        self._running = True
        self._started_at = time.monotonic()
        if gateways is None:
            wh = self.registry.gateways.get("webhook")
            if wh is not None and not wh.enabled:
                wh.enabled = True
        return self.registry.start_all(only=gateways)

    def stop(self) -> None:
        """Stop all running gateways."""
        self.registry.stop_all()
        self._running = False

    def run_forever(self, gateways: list[str] | None = None, poll_interval: float = 0.5) -> None:
        """Start runtime, continuously report health/connections, and block until interrupted."""
        self.start(gateways=gateways)

        def _handle_signal(_signum: int, _frame: Any) -> None:
            self._running = False

        prev_int = signal.signal(signal.SIGINT, _handle_signal)
        prev_term = signal.signal(signal.SIGTERM, _handle_signal)
        last_report = 0.0
        try:
            while self._running:
                now = time.monotonic()
                if now - last_report >= 2.0:
                    last_report = now
                    snap = self.get_health_snapshot()
                    active_gws = [g["name"] for g in snap["gateways"] if g["running"]]
                    print(
                        f"[health] status={snap['status']} uptime={snap['uptime_seconds']}s "
                        f"gateways={','.join(active_gws) or 'none'} "
                        f"clients={snap['active_clients']} reqs={snap['requests_handled']} "
                        f"model={snap['provider']}/{snap['model']}",
                        flush=True,
                    )
                time.sleep(poll_interval)
        finally:
            self.stop()
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)
