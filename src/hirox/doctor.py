"""System and environment diagnostics for Hirox."""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from hirox.config import get_config_path, get_home_dir, load_config
from hirox.gateway import build_registry_from_config


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str


def run_diagnostics() -> list[CheckResult]:
    """Run all Hirox readiness checks."""
    results: list[CheckResult] = []

    # 1. Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        results.append(CheckResult("python", "ok", f"Python {py_ver}"))
    else:
        results.append(CheckResult("python", "fail", f"Python {py_ver} (< 3.11 required)"))

    # 2. Core dependencies
    for pkg in ("click", "rich"):
        try:
            importlib.import_module(pkg)
            results.append(CheckResult(f"dep:{pkg}", "ok", "installed"))
        except ImportError:
            results.append(CheckResult(f"dep:{pkg}", "fail", "missing"))

    # 3. Home directory writability
    home_dir: Path = get_home_dir()
    try:
        home_dir.mkdir(parents=True, exist_ok=True)
        writable = os.access(home_dir, os.W_OK)
        if writable:
            results.append(CheckResult("home_dir", "ok", str(home_dir)))
        else:
            results.append(CheckResult("home_dir", "fail", f"not writable: {home_dir}"))
    except OSError as exc:
        results.append(CheckResult("home_dir", "fail", f"{home_dir}: {exc}"))

    # 4. Config file
    cfg_path = get_config_path()
    if cfg_path.exists():
        try:
            cfg = load_config(cfg_path)
            results.append(CheckResult("config", "ok", f"valid ({cfg_path})"))
        except Exception as exc:
            results.append(CheckResult("config", "fail", f"invalid ({cfg_path}): {exc}"))
            cfg = None
    else:
        results.append(
            CheckResult(
                "config",
                "warn",
                f"not initialized ({cfg_path}); run `hirox config init`",
            )
        )
        cfg = load_config(cfg_path)

    # 5. Gateways
    if cfg is not None:
        registry = build_registry_from_config(cfg)
        enabled = registry.enabled_gateways()
        if enabled:
            names = ", ".join(gw.name for gw in enabled)
            results.append(CheckResult("gateways", "ok", f"enabled: {names}"))
        else:
            results.append(CheckResult("gateways", "warn", "no gateways enabled"))

    return results
