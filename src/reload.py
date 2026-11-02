# -*- coding: utf-8 -*-
"""配置热加载的辅助：变更 diff、状态落盘、退出码。

BOT 进程内有一个轻量监听器（见 `app._watch_config`）：配置文件一变，
校验通过后写一份 `data/reload_state.json`，然后以 `RELOAD_EXIT_CODE` 退出；
`cmd_run` 的循环会立刻用新配置重新拉起（进程不换，PID 不变）。

面板读这份状态，展示「上次热加载时间 + 改了哪些字段」。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config

# 约定退出码：告诉 cmd_run「配置变了，重新加载再来一轮」
RELOAD_EXIT_CODE = 42

STATE_FILE = "reload_state.json"

MAX_FIELDS = 60


def _diff(old: Any, new: Any, prefix: str, out: List[str]) -> None:
    if len(out) >= MAX_FIELDS:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in old:
                out.append(f"{path}（新增）")
            elif key not in new:
                out.append(f"{path}（删除）")
            else:
                _diff(old[key], new[key], path, out)
        return
    if isinstance(old, list) and isinstance(new, list):
        if old == new:
            return
        if len(old) == len(new):
            for index, (before, after) in enumerate(zip(old, new)):
                if isinstance(before, dict) and isinstance(after, dict):
                    _diff(before, after, f"{prefix}[{index}]", out)
                elif before != after:
                    out.append(f"{prefix}[{index}]")
        else:
            out.append(f"{prefix}（{len(old)} → {len(new)} 项）")
        return
    if old != new:
        out.append(prefix)


def config_diff(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """返回发生变化的字段路径，例如 ['reply.confidence_threshold', 'app.rooms[0].alias']。"""
    out: List[str] = []
    _diff(old, new, "", out)
    return out


# ---------------------------------------------------------------- 状态文件


def state_path(cfg: Config) -> Path:
    return cfg.data_path / STATE_FILE


def read_state(cfg: Config) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(cfg: Config, *, changed: List[str], error: str = "",
                at: Optional[float] = None) -> Path:
    path = state_path(cfg)
    stamp = at or time.time()
    data = {
        "at": stamp,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)),
        "changed": list(changed),
        "count": len(changed),
        "error": error,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return path
