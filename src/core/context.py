# -*- coding: utf-8 -*-
"""直播间上下文：语音转写与弹幕的环形缓冲。"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

from ..config import Config


@dataclass
class TranscriptLine:
    ts: float
    text: str
    duration: float = 0.0
    skipped: bool = False


@dataclass
class DanmakuLine:
    ts: float
    uname: str
    text: str
    uid: int = 0
    kind: str = "danmaku"


@dataclass
class Trigger:
    kind: str                 # speech | ambient | mention
    reason: str = ""
    focus: str = ""           # 触发的语音原文 / 弹幕原文
    ts: float = field(default_factory=time.time)
    # 仅 mention：是哪个观众 @ 的（用于 Prompt 描述与日志）
    mention_user: str = ""
    mention_uid: int = 0


@dataclass
class ContextSnapshot:
    transcript: List[Tuple[float, str]]
    danmaku: List[str]
    trigger: Trigger
    last_activity_ts: float
    has_content: bool


_ONE_LINE_WS = re.compile(r"\s+")


def _one_line(text: str) -> str:
    """把内容压成单行：弹幕/语音里若混入换行，可能伪造出 "[语音] …" 之类的行结构。"""
    return _ONE_LINE_WS.sub(" ", (text or "").replace("\u3000", " ")).strip()


class PromptWindow:
    """给 Prompt 用的历史窗口：带迟滞，只在明显超预算时整块丢弃。

    逐条裁剪（来一条就丢掉最旧一条）会让 Prompt 的开头每次请求都不一样，
    服务端的前缀缓存就全废了——只有「从第一个 token 起逐字节相同」的部分才命中。
    这里改成：超过上限时一次性丢到只剩 keep_ratio（默认 60%），两次丢弃之间
    只往后追加，开头保持不动。代价是历史块会在 60%~100% 预算之间来回，可以接受。

    条目统一是 `(ts, kind, text)`：kind 为 "speech" / "danmaku"，用来单独限制
    弹幕条数（`context.max_danmaku`）。
    """

    # 每条渲染成 "- [语音] {text}\n"（标签 2 字）：按 len(text) + 8 估。
    # 必须和 format_timeline 的算法一致，否则窗口卡在预算边缘时渲染函数会再裁一条，
    # 前缀又会每条都变。
    LINE_COST = 8

    def __init__(self, keep_ratio: float = 0.6):
        self.keep_ratio = keep_ratio
        # 窗口起点用时间戳记：即使上游缓冲被裁掉，也不会错位
        self.start_ts = 0.0

    def reset(self) -> None:
        self.start_ts = 0.0

    def window(self, items: List[Tuple[float, str, str]]) -> List[Tuple[float, str, str]]:
        if not self.start_ts:
            return list(items)
        return [item for item in items if item[0] >= self.start_ts]

    def trim(
        self,
        items: List[Tuple[float, str, str]],
        max_chars: int,
        max_danmaku: Optional[int] = None,
    ) -> List[Tuple[float, str, str]]:
        """按需推进窗口起点，返回窗口内的条目。"""
        picked = self.window(items)
        chars = sum(len(text) + self.LINE_COST for _, _, text in picked)
        danmaku = sum(1 for _, kind, _ in picked if kind == "danmaku")
        if chars <= max_chars and (max_danmaku is None or danmaku <= max_danmaku):
            return picked

        keep_chars = max(0, int(max_chars * self.keep_ratio))
        keep_danmaku = (
            None if max_danmaku is None else max(1, int(max_danmaku * self.keep_ratio))
        )
        acc = 0
        dan = 0
        kept = 0
        idx = len(items)
        while idx > 0:
            cost = len(items[idx - 1][2]) + self.LINE_COST
            is_danmaku = 1 if items[idx - 1][1] == "danmaku" else 0
            if kept and (
                acc + cost > keep_chars
                or (keep_danmaku is not None and dan + is_danmaku > keep_danmaku)
            ):
                break
            idx -= 1
            kept += 1
            acc += cost
            dan += is_danmaku
        if idx < len(items):
            self.start_ts = items[idx][0]
        return self.window(items)


class RoomContext:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        # 缓冲整场（上限只是兜底，防止极端长播占内存）
        self.transcript: Deque[TranscriptLine] = deque(maxlen=4000)
        self.danmaku: Deque[DanmakuLine] = deque(maxlen=1500)
        self.started_at = time.time()
        self.last_transcript_ts = 0.0
        self.last_danmaku_ts = 0.0
        # Prompt 用的历史块：带迟滞，保证两次裁剪之间 Prompt 前缀不变（利于缓存命中）
        self._timeline_win = PromptWindow()
        self._transcript_win = PromptWindow()
        self._danmaku_win = PromptWindow()

    # ---------------------------------------------------------- 写入

    def add_transcript(self, text: str, ts: Optional[float] = None,
                       duration: float = 0.0, skipped: bool = False) -> None:
        ts = ts or time.time()
        self.transcript.append(TranscriptLine(ts=ts, text=text, duration=duration, skipped=skipped))
        if not skipped and text:
            self.last_transcript_ts = ts

    def add_danmaku(self, uname: str, text: str, ts: Optional[float] = None,
                    uid: int = 0, kind: str = "danmaku") -> None:
        ts = ts or time.time()
        self.danmaku.append(DanmakuLine(ts=ts, uname=uname, text=text, uid=uid, kind=kind))
        self.last_danmaku_ts = ts

    def add_self_text(self, text: str, ts: Optional[float] = None) -> None:
        """记下自己刚发出去的弹幕：下一轮 Prompt 里能看到「我刚才说过什么」。

        kind="self" 的条目不计入 context.max_danmaku（那是给观众弹幕的额度）。
        """
        text = (text or "").strip()
        if not text:
            return
        ts = ts or time.time()
        self.danmaku.append(DanmakuLine(ts=ts, uname="", text=text, kind="self"))
        self.last_danmaku_ts = ts

    # ---------------------------------------------------------- 读取

    def recent_transcript(self, window_sec: float, now: Optional[float] = None):
        now = now or time.time()
        cutoff = 0.0 if window_sec <= 0 else now - window_sec
        return [(l.ts, l.text) for l in self.transcript if l.ts >= cutoff and not l.skipped and l.text]

    @staticmethod
    def _danmaku_display(line: "DanmakuLine") -> str:
        if line.kind == "self":
            return f"（我）{line.text}"
        if line.kind == "danmaku":
            return f"{line.uname}: {line.text}"
        return f"[SC]{line.uname}: {line.text}"

    def recent_danmaku(self, window_sec: float, max_items: int, now: Optional[float] = None):
        now = now or time.time()
        cutoff = 0.0 if window_sec <= 0 else now - window_sec
        lines = [
            self._danmaku_display(l)
            for l in self.danmaku
            if l.ts >= cutoff
        ]
        return lines[-max_items:]

    def prompt_timeline(
        self, max_chars: int, max_danmaku: int
    ) -> Tuple[List[Tuple[float, str, str]], int]:
        """本场语音 + 弹幕合成一条时间线（给 Prompt 用），带迟滞裁剪。

        返回 ([(ts, kind, text)], 被省略条数)，kind 为 "speech" / "danmaku" / "self"。
        合成一条的好处：新内容永远只追加在末尾，窗口前缀不动，服务端缓存能一路命中；
        分成「语音块 + 弹幕块」时，语音块每长一句都会把后面的弹幕块整体推后。
        """
        now = time.time()
        t_cut = 0.0 if self._cfg.context.transcript_window_sec <= 0 else (
            now - self._cfg.context.transcript_window_sec
        )
        d_cut = 0.0 if self._cfg.context.danmaku_window_sec <= 0 else (
            now - self._cfg.context.danmaku_window_sec
        )
        items: List[Tuple[float, str, str]] = [
            (l.ts, "speech", _one_line(l.text))
            for l in self.transcript
            if not l.skipped and l.text and l.ts >= t_cut
        ]
        for line in self.danmaku:
            if line.ts < d_cut:
                continue
            if line.kind == "self":
                items.append((line.ts, "self", _one_line(line.text)))
            elif line.kind == "danmaku":
                items.append((line.ts, "danmaku", f"{_one_line(line.uname)}: {_one_line(line.text)}"))
            else:
                items.append((line.ts, "danmaku",
                              f"[SC]{_one_line(line.uname)}: {_one_line(line.text)}"))
        items.sort(key=lambda item: item[0])
        picked = self._timeline_win.trim(items, max_chars, max_danmaku)
        return picked, len(items) - len(picked)

    def prompt_transcript(self, max_chars: int) -> Tuple[List[Tuple[float, str]], int]:
        """只取语音（给自定义 user.md 里的 {{transcript}} 用）。"""
        now = time.time()
        window_sec = self._cfg.context.transcript_window_sec
        cutoff = 0.0 if window_sec <= 0 else now - window_sec
        items = [
            (l.ts, "speech", _one_line(l.text)) for l in self.transcript
            if not l.skipped and l.text and l.ts >= cutoff
        ]
        picked = self._transcript_win.trim(items, max_chars)
        return [(ts, text) for ts, _, text in picked], len(items) - len(picked)

    def prompt_danmaku(self, max_chars: int, max_count: int) -> Tuple[List[str], int]:
        """只取弹幕（给自定义 user.md 里的 {{danmaku}} 用）。"""
        window_sec = self._cfg.context.danmaku_window_sec
        now = time.time()
        cutoff = 0.0 if window_sec <= 0 else now - window_sec
        items = [
            (l.ts, "danmaku", _one_line(self._danmaku_display(l)))
            for l in self.danmaku
            if l.ts >= cutoff
        ]
        picked = self._danmaku_win.trim(items, max_chars, max_count)
        return [text for _, _, text in picked], len(items) - len(picked)

    # ---------------------------------------------------------- 存档 / 换场
    def export_session(self, room_id: int, title: str = "", live_time: str = "") -> dict:
        """导出本场明细，供落盘留存；同一场直播重启时会用它恢复上下文。"""
        now = time.time()
        return {
            "room_id": room_id,
            "title": title,
            # 开播时间：重启后拿它和接口返回值比对，相同就是同一场，可以接着用
            "live_time": live_time,
            "started_at": self.started_at,
            "ended_at": now,
            "duration_sec": round(now - self.started_at, 1),
            "transcript": [
                {"ts": l.ts, "text": l.text, "duration": l.duration, "skipped": l.skipped}
                for l in self.transcript
            ],
            "danmaku": [
                {"ts": l.ts, "uname": l.uname, "text": l.text, "kind": l.kind}
                for l in self.danmaku
            ],
            "counts": {
                "transcript": len([l for l in self.transcript if not l.skipped and l.text]),
                "danmaku": len(self.danmaku),
            },
        }

    def restore(self, dump: dict) -> bool:
        """从场次存档恢复上下文（同一场直播重启后接着用）。"""
        if not isinstance(dump, dict):
            return False
        transcript = dump.get("transcript") or []
        danmaku = dump.get("danmaku") or []
        restored = 0
        for item in transcript:
            try:
                self.transcript.append(TranscriptLine(
                    ts=float(item["ts"]),
                    text=str(item["text"]),
                    duration=float(item.get("duration") or 0.0),
                    skipped=bool(item.get("skipped")),
                ))
                restored += 1
            except (KeyError, TypeError, ValueError):
                continue
        for item in danmaku:
            try:
                self.danmaku.append(DanmakuLine(
                    ts=float(item["ts"]),
                    uname=str(item.get("uname") or ""),
                    text=str(item["text"]),
                    kind=str(item.get("kind") or "danmaku"),
                ))
                restored += 1
            except (KeyError, TypeError, ValueError):
                continue
        if not restored:
            return False
        try:
            self.started_at = float(dump.get("started_at") or self.started_at)
        except (TypeError, ValueError):
            pass
        self.last_transcript_ts = max((l.ts for l in self.transcript if not l.skipped), default=0.0)
        self.last_danmaku_ts = max((l.ts for l in self.danmaku), default=0.0)
        return True

    def snapshot(self, trigger: Trigger, now: Optional[float] = None) -> ContextSnapshot:
        now = now or time.time()
        transcript = self.recent_transcript(self._cfg.context.transcript_window_sec, now)
        danmaku = self.recent_danmaku(
            self._cfg.context.danmaku_window_sec, self._cfg.context.max_danmaku, now
        )
        last_activity = max(self.last_transcript_ts, self.last_danmaku_ts)
        return ContextSnapshot(
            transcript=transcript,
            danmaku=danmaku,
            trigger=trigger,
            last_activity_ts=last_activity,
            has_content=bool(transcript or danmaku),
        )
