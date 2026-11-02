# -*- coding: utf-8 -*-
"""运行记录的保留策略：只留最近 `app.retention_days` 天（默认 7）。

`data/` 下这几类是只追加、不轮转的，不清理会一直涨（播 8 小时约 1MB 决策记录）：

  decisions/<room>.jsonl       每次判断一条（~1.3 KB）
  transcript/<room>.jsonl      每句语音一条（~150 B）
  sent/<room>.jsonl            每次真实发送一条（~115 B）
  sessions/<room>-<时间>.json  每场一个文件（~20 KB）

清理方式：
* jsonl 按 `ts` 丢掉过期行，剩下的原样写回（顺序不变、坏行保留）；
* 场次存档按文件名里的时间戳（`<room>-YYYYmmdd-HHMMSS.json`）删除过期文件。

并发：BOT 是「每条记录 open(..., 'a') 写一次再关」，不长期持有句柄，所以
`tmp + os.replace` 换文件是安全的；但为免和刚刚的写入抢，最近 60 秒内改过的
文件这一轮跳过（启动时没有并发写入，用 force=True 直接清）。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional

from .config import Config
from .util.log import get_logger

log = get_logger("retention")

# 最近多少秒内被写过的文件，这一轮先不动（避免和 BOT 的追加互相踩）
ACTIVE_WRITE_GUARD_SEC = 60.0
# 多久跑一次
PRUNE_INTERVAL_SEC = 6 * 3600

JSONL_KINDS = ("decisions", "transcript", "sent")

_SESSION_NAME_RE = re.compile(r"-(\d{8})-(\d{6})\.json$")


def cutoff_ts(days: int, now: Optional[float] = None) -> float:
    return (now or time.time()) - max(0, int(days)) * 86400.0


def _record_ts(record: object) -> float:
    if not isinstance(record, dict):
        return 0.0
    try:
        return float(record.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def trim_jsonl(path: Path, cutoff: float, guard: float = ACTIVE_WRITE_GUARD_SEC) -> int:
    """删掉 ts 早于 cutoff 的行，返回删除行数。

    返回 -1 表示这一轮跳过（文件正被写）；写失败按 0 处理，不动原文件。
    """
    try:
        if guard > 0 and time.time() - path.stat().st_mtime < guard:
            return -1
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0

    kept = []
    removed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)  # 坏行保留，别顺手删掉别人的数据
            continue
        ts = _record_ts(record)
        if ts and ts < cutoff:
            removed += 1
        else:
            kept.append(line)

    if not removed:
        return 0

    tmp = path.with_suffix(path.suffix + ".trim")
    try:
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("清理 %s 失败: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return 0

    if not kept:
        # 整份都过期了：删掉文件，面板的房间列表也不会再显示这个空记录
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return removed


def session_file_start(name: str) -> Optional[float]:
    """从 `<room>-YYYYmmdd-HHMMSS.json` 里取出场次开始时间。"""
    match = _SESSION_NAME_RE.search(name)
    if not match:
        return None
    try:
        return time.mktime(time.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S"))
    except (ValueError, OverflowError):
        return None


def prune_sessions(cfg: Config, cutoff: float) -> int:
    removed = 0
    directory = cfg.data_path / "sessions"
    try:
        files = list(directory.glob("*.json"))
    except OSError:
        return 0
    for file in files:
        started = session_file_start(file.name)
        if started is None:
            # 文件名不认识：退回读内容，读不出来就不动它
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                started = float((data or {}).get("started_at") or 0) or None
            except (OSError, ValueError, TypeError):
                started = None
        if started is None or started >= cutoff:
            continue
        try:
            file.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def prune(cfg: Config, now: Optional[float] = None, force: bool = False) -> Dict[str, int]:
    """清理过期记录。返回各类删掉了多少（0 表示没动）。"""
    days = int(getattr(cfg.app, "retention_days", 0) or 0)
    if days <= 0:
        return {}
    cutoff = cutoff_ts(days, now)
    guard = 0.0 if force else ACTIVE_WRITE_GUARD_SEC

    stats: Dict[str, int] = {kind: 0 for kind in JSONL_KINDS}
    for kind in JSONL_KINDS:
        directory = cfg.data_path / kind
        try:
            files = list(directory.glob("*.jsonl"))
        except OSError:
            continue
        for file in files:
            count = trim_jsonl(file, cutoff, guard)
            if count > 0:
                stats[kind] += count
    stats["sessions"] = prune_sessions(cfg, cutoff)

    total = sum(stats.values())
    if total:
        log.info(
            "已清理 %d 天前的记录：决策 %d 条、转写 %d 条、发送 %d 条、场次存档 %d 个",
            days, stats["decisions"], stats["transcript"], stats["sent"], stats["sessions"],
        )
    else:
        log.debug("没有需要清理的旧记录（保留 %d 天）", days)
    return stats
