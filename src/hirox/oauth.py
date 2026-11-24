"""PKCE OAuth 2.0 flow, token storage/refresh, and dynamic model discovery for Hirox."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

def _hirox_home() -> Path:
    env = os.environ.get("HIROX_HOME", "").strip()
    return Path(env).expanduser() if env else (Path.home() / ".hirox")

OAUTH_PROFILES: dict[str, dict[str, Any]] = {
    "openai_codex_oauth": {
        "auth_url": "https://auth.openai.com/oauth/authorize",
        "token_url": "https://auth.openai.com/oauth/token",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "scopes": "openid profile email offline_access",
        "callback_path": "/auth/callback",
        "callback_host": "localhost",
        "callback_port": 1455,
        "store_file": "openai-auth.json",
    },
    "antigravity_oauth": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id": "",
        "scopes": "https://www.googleapis.com/auth/cloud-platform openid email",
        "callback_path": "/oauth2callback",
        "store_file": "antigravity_oauth.json",
    },
    "gemini_oauth": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id": "",
        "scopes": "https://www.googleapis.com/auth/generative-language openid email",
        "callback_path": "/oauth2callback",
        "store_file": "gemini_oauth.json",
    },
    "grok_oauth": {
        "auth_url": "https://accounts.x.ai/oauth/authorize",
        "token_url": "https://accounts.x.ai/oauth/token",
        "client_id": "",
        "scopes": "openid profile offline_access",
        "callback_path": "/callback",
        "store_file": "grok_oauth.json",
    },
}


def generate_pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) using S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def get_oauth_store_path(provider_id: str) -> Path:
    profile = OAUTH_PROFILES.get(provider_id, {})
    fname = str(profile.get("store_file") or f"{provider_id}.json")
    return _hirox_home() / fname


def save_oauth_credentials(provider_id: str, creds: dict[str, Any]) -> Path:
    path = get_oauth_store_path(provider_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def load_oauth_credentials(provider_id: str) -> dict[str, Any]:
    path = get_oauth_store_path(provider_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_oauth_authorize_url(
    provider_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    client_id_override: str = "",
) -> str:
    profile = OAUTH_PROFILES.get(provider_id, {})
    auth_url = str(profile.get("auth_url", ""))
    client_id = client_id_override or str(profile.get("client_id", ""))
    params: dict[str, str] = {
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if client_id:
        params["client_id"] = client_id
    if profile.get("scopes"):
        params["scope"] = str(profile["scopes"])
    if "accounts.google.com" in auth_url:
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    elif "auth.openai.com" in auth_url:
        params["id_token_add_organizations"] = "true"
        params["codex_cli_simplified_flow"] = "true"
        params["originator"] = "codex_cli_rs"
    return f"{auth_url}?{urllib.parse.urlencode(params)}"


def extract_chatgpt_account_id(token: str) -> str:
    """Extract chatgpt_account_id from an OpenAI OAuth JWT access/id token."""
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    try:
        b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8"))
        auth_claim = payload.get("https://api.openai.com/auth")
        if isinstance(auth_claim, dict):
            return str(auth_claim.get("chatgpt_account_id", "")).strip()
    except Exception:
        pass
    return ""


def exchange_oauth_code(
    provider_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_id_override: str = "",
) -> dict[str, Any]:
    profile = OAUTH_PROFILES.get(provider_id, {})
    token_url = str(profile.get("token_url", ""))
    client_id = client_id_override or str(profile.get("client_id", ""))
    if not token_url or not client_id:
        raise RuntimeError(f"OAuth token exchange requires client_id for {provider_id}.")
    payload = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    expires_in = int(data.get("expires_in") or 3600)
    access_tok = str(data.get("access_token", ""))
    id_tok = str(data.get("id_token", ""))
    account_id = extract_chatgpt_account_id(access_tok) or extract_chatgpt_account_id(id_tok)
    creds = {
        "access_token": access_tok,
        "refresh_token": str(data.get("refresh_token", "")),
        "id_token": id_tok,
        "account_id": account_id,
        "expires_at": int(time.time()) + expires_in,
        "token_type": str(data.get("token_type", "Bearer")),
    }
    save_oauth_credentials(provider_id, creds)
    return creds


def refresh_oauth_token(provider_id: str, client_id_override: str = "") -> str:
    creds = load_oauth_credentials(provider_id)
    refresh_tok = str(creds.get("refresh_token", "")).strip()
    profile = OAUTH_PROFILES.get(provider_id, {})
    token_url = str(profile.get("token_url", ""))
    client_id = client_id_override or str(profile.get("client_id", ""))
    if not refresh_tok or not token_url or not client_id:
        return str(creds.get("access_token", ""))
    payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": client_id,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        new_access = str(data.get("access_token", "")).strip()
        if new_access:
            creds["access_token"] = new_access
            if data.get("refresh_token"):
                creds["refresh_token"] = str(data["refresh_token"])
            creds["expires_at"] = int(time.time()) + int(data.get("expires_in") or 3600)
            save_oauth_credentials(provider_id, creds)
            return new_access
    except Exception:
        pass
    return str(creds.get("access_token", ""))


def get_valid_oauth_token(provider_id: str) -> str:
    creds = load_oauth_credentials(provider_id)
    access = str(creds.get("access_token", "")).strip()
    if not access:
        return ""
    expires_at = int(creds.get("expires_at") or 0)
    if expires_at and time.time() >= expires_at - 60:
        return refresh_oauth_token(provider_id)
    return access


def start_loopback_callback_server(
    callback_path: str = "/callback",
    expected_state: str = "",
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[HTTPServer, str, dict[str, str], threading.Event]:
    """Start a local loopback HTTP server to capture OAuth authorization code."""
    result: dict[str, str] = {}
    done = threading.Event()

    class _ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True

    class _CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [""])[0]
            state = qs.get("state", [""])[0]
            err = qs.get("error", [""])[0]
            if expected_state and state != expected_state:
                result["error"] = "state_mismatch"
            elif err:
                result["error"] = err
            elif code:
                result["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = (
                "<h2>Hirox OAuth Connected</h2><p>You can close this tab and return to Hirox.</p>"
                if "code" in result
                else f"<h2>Hirox OAuth Error</h2><p>{result.get('error', 'missing code')}</p>"
            )
            self.wfile.write(msg.encode("utf-8"))
            done.set()

    try:
        server = _ReusableHTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError:
        server = _ReusableHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    actual_port = int(server.server_address[1])
    redirect_uri = f"http://{host}:{actual_port}{callback_path}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, redirect_uri, result, done


_MODEL_CONTEXT_CACHE: dict[str, int] = {}


def get_cached_context_window(model: str) -> int | None:
    """Return real context window cached from provider /models metadata, if known."""
    return _MODEL_CONTEXT_CACHE.get(model)


def fetch_provider_models(base_url: str, api_key: str, timeout: float = 5.0) -> list[str]:
    """Fetch live model IDs from Codex backend or OpenAI-compatible /models endpoint."""
    base = base_url.rstrip("/")
    if "chatgpt.com/backend-api/codex" in base:
        url = f"{base}/models?client_version=0.101.0"
        headers = {
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs/0.101.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            acct = extract_chatgpt_account_id(api_key) or str(load_oauth_credentials("openai_codex_oauth").get("account_id", ""))
            if acct:
                headers["ChatGPT-Account-Id"] = acct
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            items = data.get("models") or data.get("data") or []
            models: list[str] = []
            for m in items:
                if isinstance(m, dict):
                    mid = str(m.get("slug") or m.get("id") or "").strip()
                    if mid:
                        ctx = m.get("context_window") or m.get("context_length") or m.get("max_context_tokens")
                        if isinstance(ctx, int) and ctx > 0:
                            _MODEL_CONTEXT_CACHE[mid] = ctx
                        if mid not in models:
                            models.append(mid)
            return models
        except Exception:
            return []

    url = f"{base}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") or []
        models = []
        for m in items:
            if isinstance(m, dict) and m.get("id"):
                mid = str(m.get("id", "")).strip()
                ctx = m.get("context_length") or m.get("context_window") or m.get("max_context_tokens")
                if isinstance(ctx, int) and ctx > 0:
                    _MODEL_CONTEXT_CACHE[mid] = ctx
                models.append(mid)
        return sorted(set(models))
    except Exception:
        return []
