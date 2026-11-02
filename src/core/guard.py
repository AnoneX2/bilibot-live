# -*- coding: utf-8 -*-
"""回复安全与频率控制。

两道闸门：
1. 时间闸门 —— 房间冷却、每小时/每分钟配额、全局发送间隔、静默时段；
2. 内容闸门 —— 长度、链接/@/联系方式、黑名单、自曝 AI、重复度。
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional

from ..config import Config
from ..util.log import get_logger
from ..util.text import char_len, has_at, has_contact, has_url, similarity

logger = get_logger("core.guard")

_SELF_DISCLOSURE = (
    "我是ai", "作为ai", "作为人工智能", "我是机器人", "我是模型", "语言模型",
    "人工智能模型", "ai助手", "我是虚拟",
)


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""


class GlobalRateLimiter:
    """跨房间共享的账号级限速。"""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._sends: Deque[float] = deque()
        self._last_send_ts: float = 0.0

    def check(self, now: Optional[float] = None) -> GuardResult:
        now = now or time.time()
        gap = self._cfg.app.global_min_interval_sec
        if self._last_send_ts and now - self._last_send_ts < gap:
            return GuardResult(False, f"全局间隔不足（{now - self._last_send_ts:.0f}s < {gap:.0f}s）")
        while self._sends and now - self._sends[0] > 60:
            self._sends.popleft()
        if len(self._sends) >= self._cfg.reply.max_per_minute:
            ready = time.strftime("%H:%M:%S", time.localtime(self._sends[0] + 60))
            return GuardResult(
                False,
                f"全局每分钟配额已满（{len(self._sends)}/{self._cfg.reply.max_per_minute} 条，"
                f"{ready} 后恢复）",
            )
        return GuardResult(True)

    def record(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        self._sends.append(now)
        self._last_send_ts = now


class ReplyGuard:
    def __init__(self, cfg: Config, room_id: int, global_limiter: GlobalRateLimiter,
                 data_dir: Path):
        self._cfg = cfg
        self._room_id = room_id
        self._global = global_limiter
        self._data_dir = data_dir
        self._sends: Deque[float] = deque()
        self._recent_texts: Deque[str] = deque(maxlen=30)
        self._last_send_ts: float = 0.0
        self._sent_log = data_dir / "sent" / f"{room_id}.jsonl"
        self._sent_log.parent.mkdir(parents=True, exist_ok=True)
        self._load_recent()

    # ---------------------------------------------------------- 持久化

    def _load_recent(self) -> None:
        if not self._sent_log.exists():
            return
        try:
            lines = self._sent_log.read_text(encoding="utf-8").splitlines()[-30:]
        except OSError:
            return
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = item.get("text", "")
            ts = float(item.get("ts", 0))
            if text:
                self._recent_texts.append(text)
            if ts and not item.get("dry_run"):
                # 只有真实发送才占配额、才算「刚发过」；演练记录仅用于去重，
                # 否则重启后会被上一条演练记录挡住房间冷却（约一分钟）
                self._sends.append(ts)
                self._last_send_ts = max(self._last_send_ts, ts)

    def _append_log(self, record: dict) -> None:
        try:
            with open(self._sent_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("写入发送日志失败: %s", exc)

    # ---------------------------------------------------------- 时间闸门

    @staticmethod
    def _in_quiet_hours(ranges, now: Optional[float] = None) -> bool:
        if not ranges:
            return False
        tm = time.localtime(now or time.time())
        current = tm.tm_hour * 60 + tm.tm_min
        for item in ranges:
            try:
                start_s, end_s = item.split("-")
                sh, sm = (int(x) for x in start_s.split(":"))
                eh, em = (int(x) for x in end_s.split(":"))
            except (ValueError, AttributeError):
                continue
            start, end = sh * 60 + sm, eh * 60 + em
            inside = start <= current < end if start <= end else (current >= start or current < end)
            if inside:
                return True
        return False

    def check_time(self, now: Optional[float] = None) -> GuardResult:
        now = now or time.time()
        if self._in_quiet_hours(self._cfg.reply.quiet_hours, now):
            return GuardResult(False, "静默时段")
        if self._last_send_ts and now - self._last_send_ts < self._cfg.reply.min_interval_sec:
            return GuardResult(
                False, f"房间冷却中（{now - self._last_send_ts:.0f}s < {self._cfg.reply.min_interval_sec:.0f}s）"
            )
        while self._sends and now - self._sends[0] > 3600:
            self._sends.popleft()
        if len(self._sends) >= self._cfg.reply.max_per_hour:
            ready = time.strftime("%H:%M:%S", time.localtime(self._sends[0] + 3600))
            return GuardResult(
                False,
                f"房间每小时配额已满（{len(self._sends)}/{self._cfg.reply.max_per_hour} 条，"
                f"{ready} 后恢复）",
            )
        return self._global.check(now)

    # ---------------------------------------------------------- 内容闸门

    def check_text(self, text: str) -> GuardResult:
        text = (text or "").strip()
        if not text:
            return GuardResult(False, "空文本")
        if char_len(text) > self._cfg.reply.max_chars:
            return GuardResult(False, f"超长（{char_len(text)} > {self._cfg.reply.max_chars}）")
        if has_url(text):
            return GuardResult(False, "包含链接")
        if has_at(text):
            return GuardResult(False, "包含 @")
        if has_contact(text):
            return GuardResult(False, "包含联系方式/引流词")
        lowered = text.lower()
        for bad in self._cfg.reply.banned_words:
            if bad and bad.lower() in lowered:
                return GuardResult(False, f"命中黑名单: {bad}")
        for phrase in _SELF_DISCLOSURE:
            if phrase in lowered:
                return GuardResult(False, "自曝身份")
        if text.startswith("/") or text.startswith("！"):
            return GuardResult(False, "命令/异常前缀")
        for prev in self._recent_texts:
            if similarity(text, prev) >= self._cfg.reply.dup_threshold:
                return GuardResult(False, "与最近发送内容重复")
        return GuardResult(True)

    # ---------------------------------------------------------- 记录

    def record(self, text: str, now: Optional[float] = None, dry_run: bool = False) -> None:
        now = now or time.time()
        self._last_send_ts = now
        self._recent_texts.append(text)
        if not dry_run:
            # 只有真实发送才计入每小时/每分钟配额，方便长时间 dry-run 观察
            self._sends.append(now)
            self._global.record(now)
        self._append_log({"ts": now, "room": self._room_id, "text": text, "dry_run": dry_run})

    def record_skipped(self, text: str, reason: str, now: Optional[float] = None) -> None:
        self._append_log(
            {"ts": now or time.time(), "room": self._room_id, "text": text, "skipped": reason}
        )
