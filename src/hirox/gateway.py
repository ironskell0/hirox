"""Multi-gateway implementations and registry for Hirox."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from hirox.security import (
    create_pairing_challenge,
    has_pending_challenge,
    is_sender_paired,
    log_security_event,
    verify_pairing_response,
)

MAX_WEBHOOK_BODY_BYTES = 1_048_576  # 1 MiB

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

    def is_sender_allowed(self, sender: str) -> bool:
        allowed = self.config.get("allowed_senders") or []
        if not allowed:
            return True
        return str(sender) in {str(s) for s in allowed}

    def dispatch_message(self, sender: str, text: str) -> str:
        if not self.is_sender_allowed(sender):
            log_security_event(
                "gateway_sender_denied",
                f"Sender '{sender}' not in allowed_senders",
                gateway=self.name,
                severity="warn",
            )
            return f"Permission denied: sender '{sender}' is not authorized for gateway '{self.name}'."
        requires_pairing = bool(
            self.config.get(
                "require_pairing",
                self.gateway_type in ("telegram", "discord", "slack", "whatsapp", "matrix"),
            )
        )
        if requires_pairing and not is_sender_paired(self.name, sender):
            if has_pending_challenge(self.name, sender):
                _ok, msg = verify_pairing_response(self.name, sender, text)
                return msg
            challenge = create_pairing_challenge(self.name, sender)
            return (
                "Hirox Gateway Verification Required\n"
                "Run this command in your Hirox terminal:\n"
                f"  hirox pair {challenge}\n"
                "Then reply here with the 8-character code it outputs."
            )
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

            def _is_authorized(self) -> bool:
                expected = gw.resolve_token()
                if not expected:
                    return True
                auth_hdr = str(self.headers.get("Authorization", "")).strip()
                x_tok = str(self.headers.get("X-Hirox-Token", "")).strip()
                provided = ""
                if auth_hdr.lower().startswith("bearer "):
                    provided = auth_hdr[7:].strip()
                elif x_tok:
                    provided = x_tok
                if provided and hmac.compare_digest(provided, expected):
                    return True
                log_security_event(
                    "webhook_auth_failed",
                    f"Unauthorized request to {self.path}",
                    gateway=gw.name,
                    severity="warn",
                )
                return False

            def do_POST(self) -> None:  # noqa: N802
                if not self._is_authorized():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    self._send_json(400, {"error": "invalid content-length"})
                    return
                if length < 0 or length > MAX_WEBHOOK_BODY_BYTES:
                    log_security_event(
                        "webhook_payload_too_large",
                        f"Rejected payload of {length} bytes",
                        gateway=gw.name,
                        severity="warn",
                    )
                    self._send_json(413, {"error": "payload too large"})
                    return
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
        self._bot_username = ""

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

    def _send_chunked(self, token: str, chat_id: Any, text: str) -> None:
        msg = text or "(empty response)"
        for i in range(0, len(msg), 4000):
            self._api_call(token, "sendMessage", {"chat_id": chat_id, "text": msg[i : i + 4000]})

    def _poll_loop(self, token: str) -> None:
        offset = 0
        while not self._stop_event.is_set():
            try:
                res = self._api_call(token, "getUpdates", {"timeout": 15, "offset": offset})
                for upd in res.get("result", []):
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message") or {}
                    text = str(msg.get("text") or "").strip()
                    chat = msg.get("chat") or {}
                    chat_id = chat.get("id")
                    sender_user = str((msg.get("from") or {}).get("username") or "")
                    if not text or chat_id is None:
                        continue
                    if text == "/start":
                        self._send_chunked(token, chat_id, "Hirox Telegram gateway connected. Send any message to chat.")
                        continue
                    sender_id = str(chat_id)
                    if not (self.is_sender_allowed(sender_id) or (sender_user and self.is_sender_allowed(sender_user))):
                        self._send_chunked(token, chat_id, f"Permission denied for sender '{sender_id}'.")
                        continue
                    try:
                        self._api_call(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
                    except Exception:
                        pass
                    reply = self.dispatch_message(sender_id, text)
                    self._send_chunked(token, chat_id, reply)
            except Exception as exc:
                self._detail = f"polling error: {str(exc)[:60]}"
                time.sleep(2.0)

    def start(self) -> None:
        if not self.enabled:
            return
        token = self.resolve_token()
        if not token:
            self.running = False
            self._detail = f"missing token ({self.config.get('token_env', 'TELEGRAM_BOT_TOKEN')})"
            return
        try:
            me = self._api_call(token, "getMe")
            self._bot_username = str((me.get("result") or {}).get("username") or "")
        except Exception as exc:
            self.running = False
            self._detail = f"auth failed: {str(exc)[:60]}"
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, args=(token,), daemon=True)
        self._thread.start()
        self.running = True
        self._detail = f"polling @{self._bot_username}" if self._bot_username else "polling Telegram Bot API"

    def stop(self) -> None:
        self._stop_event.set()
        self.running = False
        self._detail = "stopped"


class _SimpleTLSWebSocket:
    """Minimal stdlib RFC 6455 TLS WebSocket client for Discord Gateway."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        raw_sock = socket.create_connection((host, port), timeout=timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed")
            buf += chunk
        status_line = buf.split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            raise ConnectionError(f"WebSocket upgrade rejected: {status_line.decode('ascii', errors='replace')}")
        self._extra = buf.split(b"\r\n\r\n", 1)[1]
        self._send_lock = threading.Lock()

    def _recv_exact(self, n: int) -> bytes:
        out = bytearray()
        if self._extra:
            take = min(n, len(self._extra))
            out.extend(self._extra[:take])
            self._extra = self._extra[take:]
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            out.extend(chunk)
        return bytes(out)

    def recv_json(self) -> dict[str, Any] | None:
        while True:
            hdr = self._recv_exact(2)
            opcode = hdr[0] & 0x0F
            masked = bool(hdr[1] & 0x80)
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask_key = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                return None
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x1, 0x2):
                return json.loads(payload.decode("utf-8"))

    def _send_frame(self, opcode: int, data: bytes) -> None:
        mask = os.urandom(4)
        n = len(data)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        header.extend(mask)
        masked_data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked_data)

    def send_json(self, obj: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(obj).encode("utf-8"))

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class DiscordGateway(BaseGateway):
    """Discord Gateway WebSocket + REST Bot gateway."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        message_handler: MessageHandler | None = None,
    ) -> None:
        super().__init__(name=name, config=config, message_handler=message_handler)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._bot_id = ""
        self._bot_tag = ""

    def _rest_call(self, token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"https://discord.com/api/v10{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "HiroxBot (https://github.com/hirox, 1.0)",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

    def send_message(self, channel_id: str, content: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("Discord bot token missing.")
        msg = content or "(empty response)"
        last: dict[str, Any] = {}
        for i in range(0, len(msg), 2000):
            last = self._rest_call(token, "POST", f"/channels/{channel_id}/messages", {"content": msg[i : i + 2000]})
        return last

    def edit_message(self, channel_id: str, message_id: str, content: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("Discord bot token missing.")
        return self._rest_call(
            token,
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            {"content": (content or "(empty response)")[:2000]},
        )

    def send_dm(self, user_id: str, content: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("Discord bot token missing.")
        dm = self._rest_call(token, "POST", "/users/@me/channels", {"recipient_id": str(user_id)})
        ch_id = str(dm.get("id") or "")
        if not ch_id:
            raise RuntimeError("Could not open Discord DM channel.")
        return self.send_message(ch_id, content)

    def send_streaming_reply(self, channel_id: str, reply: str) -> None:
        """Send an initial message and progressively edit it to stream the reply."""
        msg = reply or "(empty response)"
        first_part = msg[:2000]
        if len(first_part) <= 180:
            self.send_message(channel_id, msg)
            return
        step = max(120, len(first_part) // 4)
        sent = self.send_message(channel_id, first_part[:step] + " ▌")
        msg_id = str(sent.get("id") or "")
        if not msg_id:
            if len(msg) > 2000:
                self.send_message(channel_id, msg[2000:])
            return
        pos = step
        while pos < len(first_part):
            time.sleep(0.75)
            pos = min(len(first_part), pos + step)
            suffix = " ▌" if pos < len(first_part) else ""
            self.edit_message(channel_id, msg_id, first_part[:pos] + suffix)
        if len(msg) > 2000:
            self.send_message(channel_id, msg[2000:])

    def _gateway_loop(self, token: str) -> None:
        seq: list[int | None] = [None]
        while not self._stop_event.is_set():
            ws: _SimpleTLSWebSocket | None = None
            hb_stop = threading.Event()
            try:
                ws = _SimpleTLSWebSocket("wss://gateway.discord.gg/?v=10&encoding=json", timeout=60.0)
                hello = ws.recv_json()
                if not hello or hello.get("op") != 10:
                    raise RuntimeError("Expected Discord Hello (op 10)")
                hb_interval = max(5.0, float((hello.get("d") or {}).get("heartbeat_interval", 41250)) / 1000.0)

                def _hb() -> None:
                    while not hb_stop.wait(hb_interval) and not self._stop_event.is_set():
                        try:
                            ws.send_json({"op": 1, "d": seq[0]})
                        except Exception:
                            break

                threading.Thread(target=_hb, daemon=True).start()
                ws.send_json(
                    {
                        "op": 2,
                        "d": {
                            "token": token,
                            "intents": 37377,  # GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
                            "properties": {"os": "linux", "browser": "hirox", "device": "hirox"},
                        },
                    }
                )
                while not self._stop_event.is_set():
                    ev = ws.recv_json()
                    if ev is None:
                        break
                    if ev.get("s") is not None:
                        seq[0] = int(ev["s"])
                    op = ev.get("op")
                    if op == 1:
                        ws.send_json({"op": 1, "d": seq[0]})
                        continue
                    if op in (7, 9):
                        break
                    if op == 0 and ev.get("t") == "READY":
                        user = (ev.get("d") or {}).get("user") or {}
                        self._bot_id = str(user.get("id") or "")
                        self._bot_tag = str(user.get("username") or "")
                        self._detail = f"connected @{self._bot_tag}" if self._bot_tag else "connected (Discord WS)"
                    elif op == 0 and ev.get("t") == "MESSAGE_CREATE":
                        d = ev.get("d") or {}
                        author = d.get("author") or {}
                        if author.get("bot"):
                            continue
                        content = str(d.get("content") or "").strip()
                        channel_id = str(d.get("channel_id") or "")
                        sender_id = str(author.get("id") or "")
                        if not content or not channel_id:
                            continue
                        if self._bot_id:
                            content = content.replace(f"<@{self._bot_id}>", "").replace(f"<@!{self._bot_id}>", "").strip()
                        if not content:
                            continue
                        typing_stop = threading.Event()

                        def _typing_loop(ch: str = channel_id, stop_ev: threading.Event = typing_stop) -> None:
                            while not stop_ev.is_set():
                                try:
                                    self._rest_call(token, "POST", f"/channels/{ch}/typing")
                                except Exception:
                                    pass
                                stop_ev.wait(7.0)

                        threading.Thread(target=_typing_loop, daemon=True).start()
                        try:
                            reply = self.dispatch_message(sender_id or channel_id, content)
                        finally:
                            typing_stop.set()
                        self.send_streaming_reply(channel_id, reply)
            except Exception as exc:
                self._detail = f"reconnecting ({str(exc)[:45]})"
            finally:
                hb_stop.set()
                if ws is not None:
                    ws.close()
            if not self._stop_event.is_set():
                self._stop_event.wait(3.0)

    def start(self) -> None:
        if not self.enabled:
            return
        token = self.resolve_token()
        if not token:
            self.running = False
            self._detail = f"missing token ({self.config.get('token_env', 'DISCORD_BOT_TOKEN')})"
            return
        try:
            me = self._rest_call(token, "GET", "/users/@me")
            self._bot_id = str(me.get("id") or "")
            self._bot_tag = str(me.get("username") or "")
        except Exception as exc:
            self.running = False
            self._detail = f"auth failed: {str(exc)[:60]}"
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._gateway_loop, args=(token,), daemon=True)
        self._thread.start()
        self.running = True
        self._detail = f"connecting @{self._bot_tag}" if self._bot_tag else "connecting Discord WS"

    def stop(self) -> None:
        self._stop_event.set()
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
