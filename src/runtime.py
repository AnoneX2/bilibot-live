# -*- coding: utf-8 -*-
"""运行时标记：让设置面板知道「正在跑的那个 BOT」用的是什么配置。

BOT 支持配置热加载：监听 config.yaml，变更后自动重启（见 `reload.py`）。
这个文件记录本轮启动的关键状态（是否演练模式、配置 mtime、pid、是否支持自动重载），
面板据此提示「配置已修改，需重启 / 正在自动加载」，避免以为开了演练模式、
实际旧进程仍在真实发送。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__
from .config import Config

RUNTIME_FILE = "runtime.json"


def runtime_path(cfg: Config) -> Path:
    return cfg.data_path / RUNTIME_FILE


def write_runtime(cfg: Config) -> Path:
    path = runtime_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    config_mtime = 0.0
    if cfg.config_path:
        try:
            config_mtime = Path(cfg.config_path).stat().st_mtime
        except OSError:
            pass
    data: Dict[str, Any] = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "version": __version__,
        "dry_run": bool(cfg.app.dry_run),
        "rooms": [
            {"room_id": room.room_id, "alias": room.alias, "enabled": room.enabled}
            for room in cfg.app.rooms
        ],
        "config_path": cfg.config_path,
        "config_mtime": config_mtime,
        "auto_reload": True,
        "llm_provider": cfg.llm.provider,
        "llm_model": cfg.llm.model,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_runtime(cfg: Config) -> Optional[Dict[str, Any]]:
    path = runtime_path(cfg)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
