"""Security primitives, redaction, path/command guards, and audit logging for Hirox."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hirox.config import get_home_dir

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL | re.IGNORECASE), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\bssh-(rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]+", re.IGNORECASE), "[REDACTED_SSH_KEY]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(password|parola|sifre|şifre|api[_ -]?key|secret[_ -]?key|private[_ -]?key|access[_ -]?token|auth[_ -]?token)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
)

_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
)

_CATASTROPHIC_CMD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+(/|/\*|~|\$HOME)\b"), "catastrophic recursive delete"),
    (re.compile(r"(?i)\brm\s+-[a-z]*f[a-z]*r[a-z]*\s+(/|/\*|~|\$HOME)\b"), "catastrophic recursive delete"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;"), "fork bomb"),
    (re.compile(r"(?i)\b(mkfs(\.[a-z0-9]+)?|wipefs|fdisk|parted)\b"), "disk formatting/partitioning"),
    (re.compile(r"(?i)\bdd\s+.*\bof=/dev/(sd|nvme|mmcblk|hd|vd)"), "raw disk overwrite"),
    (re.compile(r"(?i)\b(shutdown|reboot|poweroff|halt)\b"), "system power state change"),
)

_DESTRUCTIVE_CMD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\brm\s+-[a-z]*r"), "recursive file deletion"),
    (re.compile(r"(?i)\bchmod\s+-R\s+777\b"), "insecure recursive chmod 777"),
    (re.compile(r"(?i)\bgit\s+reset\s+--hard\b"), "destructive git reset --hard"),
    (re.compile(r"(?i)\bgit\s+push\b.*(\s--force\b|\s-f\b|\s--force-with-lease\b)"), "destructive git force push"),
    (re.compile(r"(?i)\bgit\s+clean\s+-[a-z]*f"), "destructive git clean -f"),
    (re.compile(r"(?i)\bgit\s+checkout\s+--\s+"), "destructive git checkout --"),
    (re.compile(r"(?i)\bgit\s+branch\s+-D\b"), "destructive git branch -D"),
)

_SENSITIVE_PATH_PARTS = (".ssh", ".gnupg", ".aws", ".kube", ".docker")
_SENSITIVE_FILES = ("/etc/shadow", "/etc/passwd", "/etc/sudoers")


def redact_secrets(text: str) -> str:
    """Redact API keys, tokens, private keys, and credentials from text."""
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def is_sensitive_text(text: str) -> bool:
    """Return True if text contains secrets, credentials, or sensitive PII."""
    if any(pat.search(text) for pat, _ in _SECRET_PATTERNS):
        return True
    if any(pat.search(text) for pat in _PII_PATTERNS):
        return True
    return False


def check_command_safety(command: str) -> tuple[str, str]:
    """Classify a shell command as ('safe'|'confirm'|'block', reason)."""
    cmd = command.strip()
    for pat, reason in _CATASTROPHIC_CMD_PATTERNS:
        if pat.search(cmd):
            return "block", reason
    for pat, reason in _DESTRUCTIVE_CMD_PATTERNS:
        if pat.search(cmd):
            return "confirm", reason
    return "safe", ""


def wrap_untrusted_content(source: str, content: str) -> str:
    """Wrap external/untrusted content to mitigate prompt injection."""
    safe_src = re.sub(r"[^a-zA-Z0-9_.:/-]", "_", source)[:80]
    return (
        f"<untrusted_content source=\"{safe_src}\">\n"
        f"{content}\n"
        f"</untrusted_content>"
    )


def validate_path(
    raw_path: str,
    workspace_root: Path | None = None,
    allow_outside: bool = False,
) -> tuple[bool, Path | None, str]:
    """Resolve and validate a filesystem path against traversal and sensitive locations."""
    if not raw_path or not raw_path.strip():
        return False, None, "Error: empty path."
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except OSError as exc:
        return False, None, f"Error: invalid path: {exc}"

    norm_str = str(resolved)
    if norm_str in _SENSITIVE_FILES or any(part in resolved.parts for part in _SENSITIVE_PATH_PARTS):
        return False, None, f"Security blocked: access to sensitive path '{resolved}' is denied."

    if not allow_outside:
        root = (workspace_root or Path.cwd()).resolve()
        home_hirox = get_home_dir().resolve()
        in_workspace = resolved == root or root in resolved.parents
        in_hirox_home = resolved == home_hirox or home_hirox in resolved.parents
        if not (in_workspace or in_hirox_home):
            return False, None, f"Security blocked: path '{resolved}' is outside workspace '{root}'."

    return True, resolved, ""


def backup_file(target: Path) -> Path | None:
    """Create a timestamped backup of an existing file before modification."""
    if not target.exists() or not target.is_file():
        return None
    backup_dir = get_home_dir() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", target.name)
    backup_path = backup_dir / f"{safe_name}.{stamp}.bak"
    shutil.copy2(target, backup_path)
    try:
        os.chmod(backup_path, 0o600)
    except OSError:
        pass
    return backup_path


def latest_backup_for(target: Path) -> Path | None:
    """Return the most recent backup file for target, if any."""
    backup_dir = get_home_dir() / "backups"
    if not backup_dir.exists():
        return None
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", target.name)
    matches = sorted(backup_dir.glob(f"{safe_name}.*.bak"))
    return matches[-1] if matches else None


def log_security_event(
    event_type: str,
    detail: str,
    gateway: str = "",
    severity: str = "info",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a structured JSONL security event to ~/.hirox/security.log without secrets."""
    try:
        log_path = get_home_dir() / "security.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "severity": severity,
            "event": event_type,
            "gateway": gateway,
            "detail": redact_secrets(detail)[:500],
        }
        if extra:
            record["meta"] = {
                str(k): redact_secrets(str(v))[:200] for k, v in extra.items()
            }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
    except Exception:
        pass


_PENDING_CHALLENGES: dict[str, dict[str, Any]] = {}


def get_pairing_secret() -> bytes:
    """Return (or create) the local 32-byte HMAC secret for gateway pairing."""
    key_path = get_home_dir() / "pairing_secret.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        raw = key_path.read_text(encoding="utf-8").strip()
        if raw:
            return raw.encode("utf-8")
    secret_hex = secrets.token_hex(32)
    key_path.write_text(secret_hex + "\n", encoding="utf-8")
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return secret_hex.encode("utf-8")


def compute_pairing_code(challenge: str) -> str:
    """Compute the 8-char XXXX-XXXX HMAC-SHA256 verification code for a challenge."""
    digest = hmac.new(get_pairing_secret(), challenge.strip().encode("utf-8"), hashlib.sha256).hexdigest().upper()
    return f"{digest[:4]}-{digest[4:8]}"


def _paired_senders_path() -> Path:
    return get_home_dir() / "paired_senders.json"


def _load_paired_senders() -> dict[str, Any]:
    p = _paired_senders_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_sender_paired(gateway: str, sender: str) -> bool:
    data = _load_paired_senders()
    return f"{gateway}:{sender}" in data


def mark_sender_paired(gateway: str, sender: str) -> None:
    p = _paired_senders_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _load_paired_senders()
    data[f"{gateway}:{sender}"] = {"paired_at": datetime.now(timezone.utc).isoformat()}
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def has_pending_challenge(gateway: str, sender: str) -> bool:
    key = f"{gateway}:{sender}"
    entry = _PENDING_CHALLENGES.get(key)
    if not entry:
        return False
    if time.time() > float(entry.get("expires_at", 0)):
        _PENDING_CHALLENGES.pop(key, None)
        return False
    return True


def create_pairing_challenge(gateway: str, sender: str) -> str:
    """Create a 5-minute one-time challenge token for an unpaired gateway sender."""
    challenge = secrets.token_hex(6).upper()
    _PENDING_CHALLENGES[f"{gateway}:{sender}"] = {
        "challenge": challenge,
        "expected": compute_pairing_code(challenge),
        "expires_at": time.time() + 300.0,
        "attempts": 0,
    }
    log_security_event(
        "gateway_pairing_challenge",
        f"Issued pairing challenge for sender '{sender}'",
        gateway=gateway,
    )
    return challenge


def verify_pairing_response(gateway: str, sender: str, response_text: str) -> tuple[bool, str]:
    """Verify a sender's pairing code response."""
    key = f"{gateway}:{sender}"
    entry = _PENDING_CHALLENGES.get(key)
    if not entry or time.time() > float(entry.get("expires_at", 0)):
        _PENDING_CHALLENGES.pop(key, None)
        new_ch = create_pairing_challenge(gateway, sender)
        return False, (
            "Challenge expired. Run this command in your Hirox terminal:\n"
            f"  hirox pair {new_ch}\n"
            "Then reply here with the 8-character code."
        )
    cleaned = re.sub(r"[^A-Za-z0-9]", "", response_text.strip().upper())
    expected_clean = re.sub(r"[^A-Za-z0-9]", "", str(entry["expected"]))
    if cleaned and hmac.compare_digest(cleaned, expected_clean):
        _PENDING_CHALLENGES.pop(key, None)
        mark_sender_paired(gateway, sender)
        log_security_event(
            "gateway_sender_paired",
            f"Sender '{sender}' verified and paired",
            gateway=gateway,
        )
        return True, "Verified! Gateway paired. You can now chat with Hirox."
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    log_security_event(
        "gateway_pairing_failed",
        f"Invalid pairing code from sender '{sender}' (attempt {entry['attempts']})",
        gateway=gateway,
        severity="warn",
    )
    if entry["attempts"] >= 5:
        _PENDING_CHALLENGES.pop(key, None)
        new_ch = create_pairing_challenge(gateway, sender)
        return False, (
            "Too many invalid attempts. New challenge issued.\n"
            f"Run in your Hirox terminal:\n  hirox pair {new_ch}\n"
            "Then reply here with the 8-character code."
        )
    return False, (
        f"Invalid verification code ({entry['attempts']}/5). "
        f"Run `hirox pair {entry['challenge']}` in your Hirox terminal and reply with the code."
    )
