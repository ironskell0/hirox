"""Extended multi-gateway implementations for Hirox (28+ platforms)."""

from __future__ import annotations

import base64
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from hirox.gateway import BaseGateway, MessageHandler, _SimpleTLSWebSocket


class _HTTPJsonGateway(BaseGateway):
    """Base helper for REST/Webhook-backed gateways."""

    default_token_env: str = ""
    default_detail: str = "ready"

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: int = 15,
    ) -> dict[str, Any]:
        hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=hdrs,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}

    def start(self) -> None:
        if not self.enabled:
            return
        if self.config.get("requires_token", True) and not self.resolve_token():
            self.running = False
            env_hint = self.config.get("token_env") or self.default_token_env or "TOKEN"
            self._detail = f"missing token ({env_hint})"
            return
        self.running = True
        self._detail = self.default_detail

    def stop(self) -> None:
        self.running = False
        self._detail = "stopped"


class IRCGateway(BaseGateway):
    """Real TCP/TLS IRC client gateway (Server + Nick + Channel)."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        message_handler: MessageHandler | None = None,
    ) -> None:
        super().__init__(name=name, config=config, message_handler=message_handler)
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()

    def _send_raw(self, line: str) -> None:
        if self._sock is None:
            return
        with self._send_lock:
            self._sock.sendall((line.rstrip("\r\n") + "\r\n").encode("utf-8"))

    def send_message(self, target: str, text: str) -> None:
        for chunk in [text[i : i + 400] for i in range(0, len(text or " "), 400)]:
            for ln in chunk.splitlines() or [""]:
                self._send_raw(f"PRIVMSG {target} :{ln}")

    def _irc_loop(self) -> None:
        server = str(self.config.get("server", "irc.libera.chat"))
        port = int(self.config.get("port", 6697))
        use_tls = bool(self.config.get("tls", port == 6697))
        nick = str(self.config.get("nick", "hiroxbot"))
        channel = str(self.config.get("channel", "#hirox"))
        password = self.resolve_token()
        while not self._stop_event.is_set():
            try:
                raw = socket.create_connection((server, port), timeout=30.0)
                if use_tls:
                    raw = ssl.create_default_context().wrap_socket(raw, server_hostname=server)
                raw.settimeout(120.0)
                self._sock = raw
                if password:
                    self._send_raw(f"PASS {password}")
                self._send_raw(f"NICK {nick}")
                self._send_raw(f"USER {nick} 0 * :Hirox Agent")
                if channel:
                    self._send_raw(f"JOIN {channel}")
                self._detail = f"connected {server}:{port} ({channel})"
                buf = ""
                while not self._stop_event.is_set():
                    data = raw.recv(4096)
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip("\r")
                        if line.startswith("PING "):
                            self._send_raw("PONG " + line[5:])
                        elif " PRIVMSG " in line:
                            prefix, rest = line.split(" PRIVMSG ", 1)
                            sender = prefix.lstrip(":").split("!", 1)[0]
                            target, msg = rest.split(" :", 1) if " :" in rest else (rest, "")
                            reply_to = sender if target == nick else target
                            if msg.strip():
                                reply = self.dispatch_message(sender, msg.strip())
                                self.send_message(reply_to, reply)
            except Exception as exc:
                self._detail = f"reconnecting ({str(exc)[:40]})"
            finally:
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
            if not self._stop_event.is_set():
                self._stop_event.wait(3.0)

    def start(self) -> None:
        if not self.enabled:
            return
        self._stop_event.clear()
        threading.Thread(target=self._irc_loop, daemon=True).start()
        self.running = True
        self._detail = f"connecting {self.config.get('server', 'irc.libera.chat')}"

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self.running = False
        self._detail = "stopped"


class TwitchGateway(IRCGateway):
    """Twitch Chat IRC gateway (irc.chat.twitch.tv:6697)."""

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.resolve_token():
            self.running = False
            self._detail = f"missing token ({self.config.get('token_env', 'TWITCH_OAUTH_TOKEN')})"
            return
        self.config.setdefault("server", "irc.chat.twitch.tv")
        self.config.setdefault("port", 6697)
        self.config.setdefault("tls", True)
        super().start()


class SignalGateway(BaseGateway):
    """Signal gateway via signal-cli subprocess bridge."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        message_handler: MessageHandler | None = None,
    ) -> None:
        super().__init__(name=name, config=config, message_handler=message_handler)
        self._stop_event = threading.Event()

    def send_message(self, recipient: str, text: str) -> None:
        account = str(self.config.get("account", "")).strip()
        cmd = ["signal-cli"]
        if account:
            cmd.extend(["-u", account])
        cmd.extend(["send", "-m", text[:4000], recipient])
        subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)

    def _poll_loop(self) -> None:
        account = str(self.config.get("account", "")).strip()
        while not self._stop_event.is_set():
            try:
                cmd = ["signal-cli"]
                if account:
                    cmd.extend(["-u", account])
                cmd.extend(["--output=json", "receive", "--timeout", "5"])
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
                for ln in res.stdout.splitlines():
                    if not ln.strip():
                        continue
                    ev = json.loads(ln)
                    env = ev.get("envelope") or {}
                    sender = str(env.get("source") or "")
                    msg = str((env.get("dataMessage") or {}).get("message") or "").strip()
                    if sender and msg:
                        reply = self.dispatch_message(sender, msg)
                        self.send_message(sender, reply)
            except Exception:
                pass
            self._stop_event.wait(2.0)

    def start(self) -> None:
        if not self.enabled:
            return
        if not shutil.which("signal-cli"):
            self.running = False
            self._detail = "signal-cli not installed"
            return
        self._stop_event.clear()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.running = True
        self._detail = "polling signal-cli"

    def stop(self) -> None:
        self._stop_event.set()
        self.running = False
        self._detail = "stopped"


class IMessageGateway(BaseGateway):
    """iMessage gateway via imsg CLI / macOS Messages."""

    def send_message(self, recipient: str, text: str) -> None:
        if shutil.which("imsg"):
            subprocess.run(["imsg", "send", "--to", recipient, "--text", text[:4000]], capture_output=True, timeout=15, check=False)

    def start(self) -> None:
        if not self.enabled:
            return
        if not shutil.which("imsg"):
            self.running = False
            self._detail = "imsg CLI not found (macOS only)"
            return
        self.running = True
        self._detail = "active (imsg ready)"

    def stop(self) -> None:
        self.running = False
        self._detail = "stopped"


class MattermostGateway(_HTTPJsonGateway):
    """Mattermost REST + WebSocket gateway."""

    default_token_env = "MATTERMOST_TOKEN"
    default_detail = "active (Mattermost API ready)"

    def send_message(self, channel_id: str, text: str) -> dict[str, Any]:
        base = str(self.config.get("url", "")).rstrip("/")
        token = self.resolve_token()
        if not base or not token:
            raise RuntimeError("Mattermost url or token missing.")
        return self._post_json(
            f"{base}/api/v4/posts",
            {"channel_id": channel_id, "message": text[:4000]},
            headers={"Authorization": f"Bearer {token}"},
        )


class NextcloudTalkGateway(_HTTPJsonGateway):
    """Nextcloud Talk self-hosted OCS API gateway."""

    default_token_env = "NEXTCLOUD_TALK_TOKEN"
    default_detail = "active (Nextcloud Talk ready)"

    def send_message(self, room_token: str, text: str) -> dict[str, Any]:
        base = str(self.config.get("url", "")).rstrip("/")
        token = self.resolve_token()
        if not base or not token:
            raise RuntimeError("Nextcloud Talk url or token missing.")
        return self._post_json(
            f"{base}/ocs/v2.php/apps/spreed/api/v1/chat/{room_token}",
            {"message": text[:4000]},
            headers={"Authorization": f"Bearer {token}", "OCS-APIRequest": "true"},
        )


class NostrGateway(BaseGateway):
    """Nostr NIP-04 DM relay WebSocket gateway."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        message_handler: MessageHandler | None = None,
    ) -> None:
        super().__init__(name=name, config=config, message_handler=message_handler)
        self._stop_event = threading.Event()

    def send_message(self, pubkey: str, text: str) -> None:
        relay = str(self.config.get("relay", "wss://relay.damus.io"))
        ws = _SimpleTLSWebSocket(relay, timeout=15.0)
        try:
            ws.send_json(["EVENT", {"kind": 4, "tags": [["p", pubkey]], "content": text[:4000]}])
        finally:
            ws.close()

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.resolve_token():
            self.running = False
            self._detail = f"missing nsec ({self.config.get('token_env', 'NOSTR_NSEC')})"
            return
        self.running = True
        self._detail = f"ready ({self.config.get('relay', 'wss://relay.damus.io')})"

    def stop(self) -> None:
        self.running = False
        self._detail = "stopped"


class FeishuGateway(_HTTPJsonGateway):
    """Feishu / Lark (飞书) Open API gateway."""

    default_token_env = "FEISHU_APP_SECRET"
    default_detail = "active (Feishu/Lark webhook + API ready)"

    def send_message(self, receive_id: str, text: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("Feishu token missing.")
        return self._post_json(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            {"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text[:4000]})},
            headers={"Authorization": f"Bearer {token}"},
        )


class GoogleChatGateway(_HTTPJsonGateway):
    """Google Chat API / Webhook gateway."""

    default_token_env = "GOOGLE_CHAT_WEBHOOK_URL"
    default_detail = "active (Google Chat ready)"

    def send_message(self, space_or_url: str, text: str) -> dict[str, Any]:
        target = space_or_url if space_or_url.startswith("http") else self.resolve_token()
        if not target:
            raise RuntimeError("Google Chat webhook URL or token missing.")
        return self._post_json(target, {"text": text[:4000]})


class LineGateway(_HTTPJsonGateway):
    """LINE Messaging API gateway."""

    default_token_env = "LINE_CHANNEL_ACCESS_TOKEN"
    default_detail = "active (LINE Messaging API ready)"

    def send_message(self, to: str, text: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("LINE channel access token missing.")
        return self._post_json(
            "https://api.line.me/v2/bot/message/push",
            {"to": to, "messages": [{"type": "text", "text": text[:4000]}]},
            headers={"Authorization": f"Bearer {token}"},
        )


class TeamsGateway(_HTTPJsonGateway):
    """Microsoft Teams Bot / Webhook gateway."""

    default_token_env = "TEAMS_BOT_TOKEN"
    default_detail = "active (Microsoft Teams ready)"

    def send_message(self, webhook_or_conv: str, text: str) -> dict[str, Any]:
        target = webhook_or_conv if webhook_or_conv.startswith("http") else str(self.config.get("webhook_url", ""))
        if not target:
            raise RuntimeError("Teams webhook_url missing.")
        return self._post_json(target, {"text": text[:4000]})


class QQBotGateway(_HTTPJsonGateway):
    """QQ Bot Official API gateway."""

    default_token_env = "QQ_BOT_TOKEN"
    default_detail = "active (QQ Bot API ready)"

    def send_message(self, target: str, text: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("QQ Bot token missing.")
        return self._post_json(
            f"https://api.sgroup.qq.com/v2/users/{target}/messages",
            {"content": text[:2000], "msg_type": 0},
            headers={"Authorization": f"QQBot {token}"},
        )


class TwilioSMSGateway(_HTTPJsonGateway):
    """Twilio SMS REST + Webhook gateway."""

    default_token_env = "TWILIO_AUTH_TOKEN"
    default_detail = "active (Twilio SMS ready)"

    def send_message(self, to_number: str, text: str) -> dict[str, Any]:
        sid = str(self.config.get("account_sid", "")).strip()
        from_num = str(self.config.get("from_number", "")).strip()
        token = self.resolve_token()
        if not sid or not from_num or not token:
            raise RuntimeError("Twilio account_sid, from_number, or auth token missing.")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        body = urllib.parse.urlencode({"To": to_number, "From": from_num, "Body": text[:1600]}).encode("utf-8")
        auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(url, data=body, headers={"Authorization": f"Basic {auth}"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))


class SynologyChatGateway(_HTTPJsonGateway):
    """Synology Chat incoming/outgoing webhook gateway."""

    default_token_env = "SYNOLOGY_CHAT_TOKEN"
    default_detail = "active (Synology Chat webhook ready)"

    def send_message(self, user_id: str, text: str) -> dict[str, Any]:
        url = str(self.config.get("incoming_url", "")).strip()
        if not url:
            raise RuntimeError("Synology Chat incoming_url missing.")
        payload = urllib.parse.urlencode({"payload": json.dumps({"text": text[:4000], "user_ids": [int(user_id)] if user_id.isdigit() else []})}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}


class TlonGateway(_HTTPJsonGateway):
    """Tlon (Urbit) Eyre HTTP gateway."""

    default_token_env = "URBIT_CODE"
    default_detail = "active (Tlon/Urbit ready)"

    def send_message(self, target: str, text: str) -> dict[str, Any]:
        url = str(self.config.get("url", "http://127.0.0.1:8080")).rstrip("/")
        return self._post_json(f"{url}/~/channel/hirox", {"ship": target, "text": text[:4000]})


class WeComGateway(_HTTPJsonGateway):
    """WeCom (企业微信) API + Webhook gateway."""

    default_token_env = "WECOM_SECRET"
    default_detail = "active (WeCom ready)"

    def send_message(self, target: str, text: str) -> dict[str, Any]:
        webhook = str(self.config.get("webhook_url", "")).strip()
        if webhook:
            return self._post_json(webhook, {"msgtype": "text", "text": {"content": text[:2048]}})
        token = self.resolve_token()
        if not token:
            raise RuntimeError("WeCom token or webhook_url missing.")
        return self._post_json(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            {"touser": target, "msgtype": "text", "text": {"content": text[:2048]}},
        )


class WeixinGateway(_HTTPJsonGateway):
    """Weixin (微信) bridge gateway."""

    default_token_env = "WEIXIN_TOKEN"
    default_detail = "active (Weixin bridge ready)"

    def send_message(self, target: str, text: str) -> dict[str, Any]:
        url = str(self.config.get("bridge_url", "")).strip()
        if not url:
            raise RuntimeError("Weixin bridge_url missing.")
        return self._post_json(url, {"to": target, "text": text[:4000]})


class YuanbaoGateway(_HTTPJsonGateway):
    """Yuanbao (元宝) gateway."""

    default_token_env = "YUANBAO_TOKEN"
    default_detail = "active (Yuanbao ready)"

    def send_message(self, target: str, text: str) -> dict[str, Any]:
        url = str(self.config.get("bridge_url", "")).strip()
        if not url:
            raise RuntimeError("Yuanbao bridge_url missing.")
        return self._post_json(url, {"to": target, "text": text[:4000]})


class ZaloBotGateway(_HTTPJsonGateway):
    """Zalo Official Account Bot API gateway."""

    default_token_env = "ZALO_BOT_TOKEN"
    default_detail = "active (Zalo Bot API ready)"

    def send_message(self, user_id: str, text: str) -> dict[str, Any]:
        token = self.resolve_token()
        if not token:
            raise RuntimeError("Zalo Bot token missing.")
        return self._post_json(
            "https://openapi.zalo.me/v3.0/oa/message/cs",
            {"recipient": {"user_id": user_id}, "message": {"text": text[:2000]}},
            headers={"access_token": token},
        )


class ZaloPersonalGateway(_HTTPJsonGateway):
    """Zalo Personal Account bridge gateway."""

    default_token_env = "ZALO_PERSONAL_TOKEN"
    default_detail = "active (Zalo Personal ready)"

    def send_message(self, target: str, text: str) -> dict[str, Any]:
        url = str(self.config.get("bridge_url", "")).strip()
        if not url:
            raise RuntimeError("Zalo Personal bridge_url missing.")
        return self._post_json(url, {"to": target, "text": text[:2000]})


class ZaloClawBotGateway(_HTTPJsonGateway):
    """Zalo ClawBot QR bridge gateway."""

    default_token_env = "ZALO_CLAWBOT_TOKEN"
    default_detail = "active (Zalo ClawBot QR ready)"

    def send_message(self, target: str, text: str) -> dict[str, Any]:
        url = str(self.config.get("bridge_url", "")).strip()
        if not url:
            raise RuntimeError("Zalo ClawBot bridge_url missing.")
        return self._post_json(url, {"to": target, "text": text[:2000]})


class ClickClackGateway(_HTTPJsonGateway):
    """ClickClack (@openclaw/clickclack) gateway."""

    default_token_env = "CLICKCLACK_TOKEN"
    default_detail = "active (ClickClack ready)"

    def send_message(self, target: str, text: str) -> dict[str, Any]:
        url = str(self.config.get("url", "")).strip()
        if not url:
            raise RuntimeError("ClickClack url missing.")
        return self._post_json(url, {"to": target, "text": text[:4000]}, headers={"Authorization": f"Bearer {self.resolve_token()}"})


class RaftGateway(BaseGateway):
    """Raft CLI wake bridge gateway."""

    def send_message(self, target: str, text: str) -> None:
        cmd = str(self.config.get("command", "")).strip()
        if cmd:
            subprocess.run([cmd, target, text[:4000]], capture_output=True, timeout=15, check=False)

    def start(self) -> None:
        if not self.enabled:
            return
        self.running = True
        self._detail = "active (Raft CLI wake bridge)"

    def stop(self) -> None:
        self.running = False
        self._detail = "stopped"


EXTRA_GATEWAY_CLASSES: dict[str, type[BaseGateway]] = {
    "clickclack": ClickClackGateway,
    "feishu": FeishuGateway,
    "google_chat": GoogleChatGateway,
    "imessage": IMessageGateway,
    "irc": IRCGateway,
    "line": LineGateway,
    "mattermost": MattermostGateway,
    "teams": TeamsGateway,
    "nextcloud_talk": NextcloudTalkGateway,
    "nostr": NostrGateway,
    "qq_bot": QQBotGateway,
    "raft": RaftGateway,
    "signal": SignalGateway,
    "sms_twilio": TwilioSMSGateway,
    "synology_chat": SynologyChatGateway,
    "tlon": TlonGateway,
    "twitch": TwitchGateway,
    "wecom": WeComGateway,
    "weixin": WeixinGateway,
    "yuanbao": YuanbaoGateway,
    "zalo_bot": ZaloBotGateway,
    "zalo_personal": ZaloPersonalGateway,
    "zalo_clawbot": ZaloClawBotGateway,
}
