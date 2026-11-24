"""Multi-gateway implementations and registry for Hirox."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

MessageHandler = Callable[[str, str, str], str]


@dataclass
class GatewayStatus:
    name: str
    gateway_type: str
    enabled: bool
    running: bool = False
    detail: str = "ready"


class BaseGateway(ABC):
    """Base interface for all Hirox communication gateways."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        message_handler: MessageHandler | None = None,
    ) -> None:
        self.name = name
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))
        self.running = False
        self._detail = "standby" if self.enabled else "disabled"
        self.message_handler = message_handler

    @property
    def gateway_type(self) -> str:
        return str(self.config.get("type", self.name))

    def resolve_token(self) -> str:
        tok = str(self.config.get("token", "")).strip()
        if tok:
            return tok
        env_name = str(self.config.get("token_env", "")).strip()
        return os.environ.get(env_name, "").strip() if env_name else ""

    def dispatch_message(self, sender: str, text: str) -> str:
        if self.message_handler is None:
            return f"[{self.name}] received: {text}"
        return self.message_handler(self.name, sender, text)

    @abstractmethod
    def start(self) -> None:
        """Start the gateway."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the gateway."""

    def status(self) -> GatewayStatus:
        return GatewayStatus(
            name=self.name,
            gateway_type=self.gateway_type,
            enabled=self.enabled,
            running=self.running,
            detail=self._detail,
        )


class CLIGateway(BaseGateway):
    """Interactive terminal CLI gateway."""

    def start(self) -> None:
        if self.enabled:
            self.running = True
            self._detail = "active"

    def stop(self) -> None:
        self.running = False
        self._detail = "stopped"


class WebhookGateway(BaseGateway):
    """HTTP Webhook & Control gateway using stdlib ThreadingHTTPServer."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        message_handler: MessageHandler | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        health_provider: Callable[[], dict[str, Any]] | None = None,
        client_tracker: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(name=name, config=config, message_handler=message_handler)
        self.host = str(self.config.get("host", host))
        self.port = int(self.config.get("port", port))
        self.path = str(self.config.get("path", "/webhook"))
        self.health_provider = health_provider
        self.client_tracker = client_tracker if client_tracker is not None else {}
        self.requests_handled = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        gw = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/health":
                    if gw.health_provider is not None:
                        self._send_json(200, gw.health_provider())
                    else:
                        self._send_json(200, {"status": "ok", "gateway": gw.name})
                    return
                if parsed.path == "/whatsapp/webhook":
                    qs = urllib.parse.parse_qs(parsed.query)
                    challenge = qs.get("hub.challenge", [""])[0]
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(challenge.encode("utf-8"))
                    return
                self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid json"})
                    return

                if self.path in ("/chat/register", "/chat/heartbeat"):
                    cid = str(data.get("client_id") or f"chat-{int(time.time() * 1000)}")
                    gw.client_tracker[cid] = {
                        "client_id": cid,
                        "gateway": str(data.get("gateway", "cli")),
                        "provider": str(data.get("provider", "")),
                        "model": str(data.get("model", "")),
                        "last_seen": time.time(),
                    }
                    self._send_json(200, {"status": "connected", "client_id": cid})
                    return

                if self.path == "/chat/disconnect":
                    cid = str(data.get("client_id") or "")
                    gw.client_tracker.pop(cid, None)
                    self._send_json(200, {"status": "disconnected"})
                    return

                if self.path == "/slack/events" and data.get("type") == "url_verification":
                    self._send_json(200, {"challenge": data.get("challenge", "")})
                    return

                if self.path in (gw.path, "/chat", "/slack/events", "/whatsapp/webhook", "/discord"):
                    text = str(
                        data.get("message")
                        or data.get("text")
                        or (data.get("event") or {}).get("text")
                        or ""
                    ).strip()
                    sender = str(data.get("sender") or data.get("user") or "webhook-user")
                    if not text:
                        self._send_json(400, {"error": "missing message/text"})
                        return
                    gw.requests_handled += 1
                    reply = gw.dispatch_message(sender, text)
                    self._send_json(200, {"reply": reply, "gateway": gw.name})
                    return

                self._send_json(404, {"error": "unknown endpoint"})

        self._server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.running = True
        self._detail = f"listening http://{self.host}:{self.port}{self.path}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.running = False
        self._detail = "stopped"


class TelegramGateway(BaseGateway):
    """Telegram Bot API long-polling gateway."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        message_handler: MessageHandler | None = None,
    ) -> None:
        super().__init__(name=name, config=config, message_handler=message_handler)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _api_call(self, token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _poll_loop(self, token: str) -> None:
        offset = 0
        while not self._stop_event.is_set():
            try:
                res = self._api_call(token, "getUpdates", {"timeout": 15, "offset": offset})
                for upd in res.get("result", []):
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message") or {}
                    text = str(msg.get("text") or "").strip()
                    chat_id = (msg.get("chat") or {}).get("id")
                    if text and chat_id is not None:
                        reply = self.dispatch_message(str(chat_id), text)
                        self._api_call(token, "sendMessage", {"chat_id": chat_id, "text": reply[:4000]})
            except Exception:
                time.sleep(2.0)

    def start(self) -> None:
        if not self.enabled:
            return
        token = self.resolve_token()
        if not token:
            self.running = False
            self._detail = f"missing token ({self.config.get('token_env', 'TELEGRAM_BOT_TOKEN')})"
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, args=(token,), daemon=True)
        self._thread.start()
        self.running = True
        self._detail = "polling Telegram Bot API"

    def stop(self) -> None:
        self._stop_event.set()
        self.running = False
        self._detail = "stopped"


class DiscordGateway(BaseGateway):
    """Discord REST Bot gateway."""

    def send_message(self, channel_id: str, content: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("Discord bot token missing.")
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        req = urllib.request.Request(
            url,
            data=json.dumps({"content": content[:2000]}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bot {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.resolve_token():
            self.running = False
            self._detail = f"missing token ({self.config.get('token_env', 'DISCORD_BOT_TOKEN')})"
            return
        self.running = True
        self._detail = "active (Discord REST ready)"

    def stop(self) -> None:
        self.running = False
        self._detail = "stopped"


class SlackGateway(BaseGateway):
    """Slack Web API gateway."""

    def send_message(self, channel: str, text: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("Slack bot token missing.")
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": text[:4000]}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.resolve_token():
            self.running = False
            self._detail = f"missing token ({self.config.get('token_env', 'SLACK_BOT_TOKEN')})"
            return
        self.running = True
        self._detail = "active (Slack Web API ready)"

    def stop(self) -> None:
        self.running = False
        self._detail = "stopped"


class WhatsAppGateway(BaseGateway):
    """WhatsApp Cloud API gateway."""

    def send_message(self, to: str, text: str) -> dict[str, Any]:
        token = self.resolve_token()
        phone_id = str(self.config.get("phone_number_id", "")).strip()
        if not token or not phone_id:
            raise RuntimeError("WhatsApp token or phone_number_id missing.")
        url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[:4000]},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.resolve_token():
            self.running = False
            self._detail = f"missing token ({self.config.get('token_env', 'WHATSAPP_TOKEN')})"
            return
        self.running = True
        self._detail = "active (WhatsApp Cloud API ready)"

    def stop(self) -> None:
        self.running = False
        self._detail = "stopped"


class MatrixGateway(BaseGateway):
    """Matrix Client-Server API sync gateway."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        message_handler: MessageHandler | None = None,
    ) -> None:
        super().__init__(name=name, config=config, message_handler=message_handler)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _sync_loop(self, homeserver: str, token: str) -> None:
        since = ""
        base = homeserver.rstrip("/")
        while not self._stop_event.is_set():
            try:
                qs = f"?timeout=15000" + (f"&since={urllib.parse.quote(since)}" if since else "")
                req = urllib.request.Request(
                    f"{base}/_matrix/client/v3/sync{qs}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                since = str(data.get("next_batch", since))
                rooms = (data.get("rooms") or {}).get("join") or {}
                for room_id, room_data in rooms.items():
                    for ev in (room_data.get("timeline") or {}).get("events", []):
                        if ev.get("type") == "m.room.message":
                            body = str((ev.get("content") or {}).get("body") or "").strip()
                            sender = str(ev.get("sender") or "matrix-user")
                            if body:
                                reply = self.dispatch_message(sender, body)
                                txn = str(int(time.time() * 1000))
                                send_url = (
                                    f"{base}/_matrix/client/v3/rooms/"
                                    f"{urllib.parse.quote(room_id)}/send/m.room.message/{txn}"
                                )
                                sreq = urllib.request.Request(
                                    send_url,
                                    data=json.dumps({"msgtype": "m.text", "body": reply[:4000]}).encode("utf-8"),
                                    headers={
                                        "Content-Type": "application/json",
                                        "Authorization": f"Bearer {token}",
                                    },
                                    method="PUT",
                                )
                                urllib.request.urlopen(sreq, timeout=15).close()
            except Exception:
                time.sleep(2.0)

    def start(self) -> None:
        if not self.enabled:
            return
        token = self.resolve_token()
        if not token:
            self.running = False
            self._detail = f"missing token ({self.config.get('token_env', 'MATRIX_ACCESS_TOKEN')})"
            return
        homeserver = str(self.config.get("homeserver", "https://matrix.org"))
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sync_loop, args=(homeserver, token), daemon=True)
        self._thread.start()
        self.running = True
        self._detail = f"syncing {homeserver}"

    def stop(self) -> None:
        self._stop_event.set()
        self.running = False
        self._detail = "stopped"


@dataclass
class GatewayRegistry:
    """Manages multiple Hirox gateways."""

    gateways: dict[str, BaseGateway] = field(default_factory=dict)

    def register(self, gateway: BaseGateway) -> None:
        self.gateways[gateway.name] = gateway

    def enabled_gateways(self) -> list[BaseGateway]:
        return [gw for gw in self.gateways.values() if gw.enabled]

    def start_all(self, only: list[str] | None = None) -> list[GatewayStatus]:
        started: list[GatewayStatus] = []
        for name, gw in self.gateways.items():
            if only and name not in only:
                continue
            if gw.enabled or (only and name in only):
                gw.enabled = True
                gw.start()
                started.append(gw.status())
        return started

    def stop_all(self) -> None:
        for gw in self.gateways.values():
            if gw.running:
                gw.stop()

    def statuses(self) -> list[GatewayStatus]:
        return [gw.status() for gw in self.gateways.values()]


def build_registry_from_config(
    config: dict[str, Any],
    message_handler: MessageHandler | None = None,
) -> GatewayRegistry:
    """Create a GatewayRegistry from loaded Hirox configuration."""
    registry = GatewayRegistry()
    gateways_cfg = config.get("gateways", {})
    rt = config.get("runtime", {})
    host = str(rt.get("host", "127.0.0.1"))
    port = int(rt.get("port", 8765))

    if isinstance(gateways_cfg, dict):
        for name, gw_cfg in gateways_cfg.items():
            if not isinstance(gw_cfg, dict):
                continue
            gtype = str(gw_cfg.get("type", name)).lower()
            if gtype == "cli":
                gw: BaseGateway = CLIGateway(name=name, config=gw_cfg, message_handler=message_handler)
            elif gtype == "webhook":
                gw = WebhookGateway(
                    name=name,
                    config=gw_cfg,
                    message_handler=message_handler,
                    host=host,
                    port=port,
                )
            elif gtype == "telegram":
                gw = TelegramGateway(name=name, config=gw_cfg, message_handler=message_handler)
            elif gtype == "discord":
                gw = DiscordGateway(name=name, config=gw_cfg, message_handler=message_handler)
            elif gtype == "slack":
                gw = SlackGateway(name=name, config=gw_cfg, message_handler=message_handler)
            elif gtype == "whatsapp":
                gw = WhatsAppGateway(name=name, config=gw_cfg, message_handler=message_handler)
            elif gtype == "matrix":
                gw = MatrixGateway(name=name, config=gw_cfg, message_handler=message_handler)
            else:
                gw = CLIGateway(name=name, config=gw_cfg, message_handler=message_handler)
            registry.register(gw)
    return registry
