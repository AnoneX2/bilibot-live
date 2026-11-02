# -*- coding: utf-8 -*-
"""回复决策引擎。

职责：把「上下文 + 触发」变成「一条经过校验的弹幕」，或决定不说。
不负责拉流/ASR/弹幕连接，这些由 RoomWorker 组装后注入。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Callable, Deque, List, Optional

from collections import deque

from ..ai.prompt_store import (
    PromptStore,
    format_danmaku,
    format_timeline,
    format_transcript,
)
from ..ai.prompts import Decision, parse_decision
from ..ai.provider import BaseProvider, LLMError
from ..bili.api import BiliAPI, BiliAPIError
from ..config import Config
from ..live.video import Frame
from ..util.log import get_logger
from ..util.text import clean_for_send
from .context import ContextSnapshot, RoomContext, Trigger
from .guard import ReplyGuard

logger = get_logger("core.reply")


@dataclass
class ReplyStats:
    triggers: int = 0
    mentions: int = 0
    llm_calls: int = 0
    says: int = 0
    skips: int = 0
    sends: int = 0
    blocked: int = 0
    low_confidence: int = 0
    muted: int = 0
    gate_blocks: int = 0
    last_gate_reason: str = ""
    errors: int = 0
    last_send_ts: float = 0.0
    last_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "triggers": self.triggers,
            "mentions": self.mentions,
            "llm_calls": self.llm_calls,
            "says": self.says,
            "skips": self.skips,
            "sends": self.sends,
            "blocked": self.blocked,
            "low_confidence": self.low_confidence,
            "muted": self.muted,
            "gate_blocks": self.gate_blocks,
            "last_gate_reason": self.last_gate_reason,
            "errors": self.errors,
            "last_send_ts": self.last_send_ts,
            "last_reason": self.last_reason,
        }


# 主播自我禁言最长时长（分钟），防止手滑写个巨大的数字把 BOT 关一整天
MAX_SELF_MUTE_MINUTES = 1440

# B 站「已在本房间被禁言」的错误码（也兼容文案里带「禁言」的返回）
MUTED_CODES = {1003}

_MUTE_CMD_RE = re.compile(r"^\s*/\s*mute\s+(\d{1,4})\s*$", re.IGNORECASE)
_UNMUTE_CMD_RE = re.compile(r"^\s*/\s*unmute\s*$", re.IGNORECASE)


def parse_mute_command(text: str) -> Optional[int]:
    """解析主播的 `/mute 100` 指令，返回分钟数；不是指令返回 None。"""
    match = _MUTE_CMD_RE.match((text or "").strip())
    if not match:
        return None
    minutes = int(match.group(1))
    if minutes <= 0:            # /mute 0 没有意义，当作没这条指令
        return None
    return min(minutes, MAX_SELF_MUTE_MINUTES)


def parse_unmute_command(text: str) -> bool:
    """解析主播的 `/unmute` 指令（提前解除自我禁言）。"""
    return bool(_UNMUTE_CMD_RE.match((text or "").strip()))


def is_muted_error(code: int, message: str = "") -> bool:
    return code in MUTED_CODES or "禁言" in (message or "")


class ReplyEngine:
    def __init__(
        self,
        cfg: Config,
        *,
        room_id: int,
        alias: str,
        title: str,
        api: BiliAPI,
        provider: BaseProvider,
        guard: ReplyGuard,
        ctx: RoomContext,
        frame_provider: Callable[[], Optional[Frame]],
        prompt_store: PromptStore,
        session_uid: int = 0,
        session_name: str = "",
        on_muted: Optional[Callable[[str], bool]] = None,
    ):
        self._cfg = cfg
        self.room_id = room_id
        self._alias = alias
        self._title = title
        self._api = api
        self._provider = provider
        self._guard = guard
        self._ctx = ctx
        self._frame_provider = frame_provider
        self._prompts = prompt_store
        self._session_uid = session_uid
        # 机器人自己登录的账号名：写进 Prompt 让它认得自己
        self._session_name = (session_name or "").strip()
        self._rng = random.Random(room_id)

        self._pending: Deque[Trigger] = deque(maxlen=3)
        self._mentions: Deque[Trigger] = deque(maxlen=3)
        self._mention_attempts: Deque[float] = deque(maxlen=200)
        self._lock = asyncio.Lock()
        self._last_reply_ts: float = 0.0
        self._last_ambient_ts: float = 0.0
        self._last_speech_submit: float = 0.0
        self._last_mention_submit: float = 0.0
        self._last_attempt_ts: float = 0.0
        self._last_gate_log_ts: float = 0.0
        self._suppress_until: float = 0.0
        self._last_has_frame: bool = False
        # 被 B 站 判定「本房间禁言」时通知上层（返回 True 表示已处理＝别再试了）
        self._on_muted = on_muted
        self._silenced = False
        # 主播用 /mute N 让 BOT 自我禁言：到期时间戳（0 表示没禁言）
        self._mute_until: float = 0.0
        self.stats = ReplyStats()
        self._decision_log = cfg.data_path / "decisions" / f"{room_id}.jsonl"
        self._decision_log.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- 触发

    def submit(self, trigger: Trigger) -> None:
        """接收触发：主播说话（speech）或有人在弹幕里 @ 了 BOT（mention）。"""
        if self.is_muted() or self._silenced:
            # 自我禁言 / 已被房间禁言：只听不说，连大模型都不打
            self.stats.muted += 1
            return
        if trigger.kind == "mention":
            self._submit_mention(trigger)
            return
        if self._cfg.reply.mode != "speech":
            return
        if trigger.kind != "speech":
            logger.debug("忽略非语音触发 room=%s kind=%s", self.room_id, trigger.kind)
            return
        text = (trigger.focus or "").strip()
        if len(text) < self._cfg.reply.speech_min_chars:
            return
        now = time.time()
        # 主播连续说话时，限制「多久才允许触发一次判断」
        if now - self._last_speech_submit < self._cfg.reply.speech_trigger_interval_sec:
            return
        if self._pending:
            # 只保留最新的一句，旧话已被后面的语音覆盖
            self._pending.clear()
        self._pending.append(trigger)
        self._last_speech_submit = now
        self.stats.triggers += 1
        logger.debug("收到语音触发 room=%s focus=%s", self.room_id, text[:40])

    def _submit_mention(self, trigger: Trigger) -> None:
        """有人 @ 了 BOT：额外两道闸门，保证「适度」而不是有求必应。"""
        cfg = self._cfg
        if not cfg.reply.mention_enabled:
            return
        if cfg.reply.mode == "off":
            logger.debug("room=%s reply.mode=off，忽略 @ 触发", self.room_id)
            return
        now = time.time()
        if now - self._last_mention_submit < cfg.reply.mention_min_interval_sec:
            logger.info(
                "room=%s 有人 @ 但距上次不足 %.0fs，先不回应（%s）",
                self.room_id, cfg.reply.mention_min_interval_sec, (trigger.focus or "")[:40],
            )
            return
        while self._mention_attempts and now - self._mention_attempts[0] > 3600:
            self._mention_attempts.popleft()
        if len(self._mention_attempts) >= cfg.reply.mention_max_per_hour:
            logger.info(
                "room=%s 本小时 @ 回应已达上限（%d 次），忽略",
                self.room_id, cfg.reply.mention_max_per_hour,
            )
            return
        self._mention_attempts.append(now)
        self._last_mention_submit = now
        self._mentions.append(trigger)
        self.stats.mentions += 1
        logger.info(
            "room=%s 收到 @ 触发：%s → %s",
            self.room_id, trigger.mention_user or "观众", (trigger.focus or "")[:60],
        )
    # ---------------------------------------------------------- 主循环

    async def run(self, stop: asyncio.Event) -> None:
        tick = 3.0
        while not stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                logger.exception("room=%s 回复引擎异常", self.room_id)
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = time.time()
        if self._silenced:
            return
        if self._mute_until:
            if not self._cfg.reply.mute_command_enabled:
                logger.info("room=%s /mute 功能已关闭，自动解除自我禁言", self.room_id)
                self._mute_until = 0.0
            elif now < self._mute_until:
                return
            else:
                logger.info("room=%s 自我禁言结束，恢复判断", self.room_id)
                self._mute_until = 0.0
        if self._cfg.reply.mode == "off":
            return
        time_gate = self._guard.check_time(now)
        if not time_gate.ok:
            self.stats.gate_blocks += 1
            self.stats.last_gate_reason = time_gate.reason
            # 频率门禁每 60 秒提示一次，避免持续刷屏
            if now - self._last_gate_log_ts >= 60:
                self._last_gate_log_ts = now
                logger.info("room=%s 暂不判断：%s", self.room_id, time_gate.reason)
            else:
                logger.debug("room=%s 暂不发送: %s", self.room_id, time_gate.reason)
            return
        trigger = self._pick_trigger(now)
        if trigger is None:
            return
        async with self._lock:
            await self._handle(trigger, time.time())

    def _pick_trigger(self, now: float) -> Optional[Trigger]:
        # @ 提及优先于语音触发（更有时效性；超过 60 秒就作废，别拿旧话答非所问）
        while self._mentions:
            mention = self._mentions.popleft()
            if now - mention.ts > 60:
                logger.debug("丢弃过期 @ 触发 room=%s", self.room_id)
                continue
            return mention

        mode = self._cfg.reply.mode
        if mode == "speech":
            # 只认主播说话；超过 90 秒的旧话丢弃
            while self._pending:
                trigger = self._pending.popleft()
                if now - trigger.ts > 90:
                    logger.debug("丢弃过期语音触发 room=%s", self.room_id)
                    continue
                return trigger
            return None

        # ambient：不看语音触发，按固定节奏巡检一次
        self._pending.clear()
        if mode != "ambient":
            return None
        if not self._ctx.snapshot(Trigger("ambient"), now).has_content:
            return None
        if now - self._last_ambient_ts < self._cfg.reply.ambient_interval_sec:
            return None
        # 至少要有一个新事件发生在上次回复之后，避免对着空场说话
        last_activity = max(self._ctx.last_transcript_ts, self._ctx.last_danmaku_ts)
        if last_activity <= max(self._last_reply_ts, self._ctx.started_at):
            return None
        self._last_ambient_ts = now
        return Trigger("ambient", reason="定时巡检")

    # ---------------------------------------------------------- 单次决策

    async def _handle(self, trigger: Trigger, now: float) -> None:
        # 连续触发时限制「尝试」频率：被拦截/报错也不至于每 3 秒打一次模型
        if now - self._last_attempt_ts < 15:
            return
        if now < self._suppress_until:
            return
        self._last_attempt_ts = now

        # 只有 ambient 需要掷骰子；speech 是主播说完后的一次判断
        prob = self._cfg.reply.probability if trigger.kind == "ambient" else 1.0
        if prob < 1.0 and self._rng.random() > prob:
            logger.debug("room=%s 概率过滤未通过", self.room_id)
            return

        snapshot = self._ctx.snapshot(trigger, now)
        if not snapshot.has_content:
            return

        frame = self._frame_provider()
        self._last_has_frame = frame is not None
        frame_age = (time.time() - frame.ts) if frame else None
        if frame is None:
            frame_desc = "本次没有附带画面，只依据主播的话和现场氛围判断。"
        else:
            frame_desc = f"已附带一帧直播截图（约 {int(frame_age)} 秒前），可参考画面内容。"

        budget = max(300, self._cfg.context.max_chars - 300)
        # 语音 + 弹幕合成一条时间线：新内容只追加在末尾，配合迟滞窗口能让
        # Prompt 前缀在两次裁剪之间完全不变，服务端前缀缓存才吃得满
        timeline, omitted_timeline = self._ctx.prompt_timeline(
            budget, self._cfg.context.max_danmaku
        )
        values = {
            "title": self._title,
            "alias": self._alias,
            "alias_part": f"（{self._alias}）" if self._alias else "",
            "bot_name": self._session_name or "（未登录）",
            "clock": time.strftime("%H:%M:%S", time.localtime(now)),
            "frame_desc": frame_desc,
            "timeline": format_timeline(timeline, budget, omitted=omitted_timeline),
            "trigger": self._describe_trigger(trigger),
            "max_chars": self._cfg.reply.max_chars,
            "room_id": self.room_id,
        }
        # 兼容自己改过 user.md 的人：模板里用到 {{transcript}} / {{danmaku}} 才去算
        raw_user = self._prompts.raw("user")
        if "{{transcript}}" in raw_user.replace(" ", ""):
            lines, omitted = self._ctx.prompt_transcript(int(budget * 0.65))
            values["transcript"] = format_transcript(
                lines, now, int(budget * 0.65), omitted=omitted
            )
        if "{{danmaku}}" in raw_user.replace(" ", ""):
            lines, omitted = self._ctx.prompt_danmaku(
                int(budget * 0.35), self._cfg.context.max_danmaku
            )
            values["danmaku"] = format_danmaku(
                lines, int(budget * 0.35), omitted=omitted
            )
        user_prompt = self._prompts.user(**values)
        system_prompt = self._prompts.system()

        started = time.time()
        self.stats.llm_calls += 1
        try:
            response = await asyncio.wait_for(
                self._provider.complete(
                    system_prompt, user_prompt, frame.jpeg if frame else None
                ),
                timeout=self._cfg.llm.timeout_sec + 10,
            )
        except (asyncio.TimeoutError, LLMError) as exc:
            self.stats.errors += 1
            self._suppress_until = time.time() + 60
            logger.warning("room=%s LLM 调用失败: %s", self.room_id, exc)
            self._log_decision(trigger, snapshot, None, sent=False, error=str(exc),
                               elapsed=time.time() - started)
            return

        decision = parse_decision(response.text, self._cfg.llm.lenient_parse)
        elapsed = time.time() - started
        logger.info(
            "room=%s LLM→%s conf=%.2f 「%s」%s (%.1fs)",
            self.room_id, decision.action, decision.confidence,
            decision.text or decision.reason, f" {decision.parse_error}" if decision.parse_error else "",
            elapsed,
        )

        if not decision.should_say:
            self.stats.skips += 1
            self.stats.last_reason = decision.reason or "模型选择沉默"
            self._log_decision(trigger, snapshot, decision, sent=False, elapsed=elapsed)
            return

        # 被 @ 是「有人点名要你接话」：该不该回已经由 mention 的间隔/配额决定了，
        # 这边不再用置信度拦（那是模型对自己措辞的把握，不是「要不要回应」的信号）
        if self._cfg.reply.confidence_gate and trigger.kind != "mention":
            threshold = self._cfg.reply.confidence_threshold
            if decision.confidence < threshold:
                # 模型自己都没把握，就别接话了（比发错更划算）
                self.stats.skips += 1
                self.stats.low_confidence += 1
                self.stats.last_reason = (
                    f"置信度不足 {decision.confidence:.2f} < {threshold:.2f}"
                )
                logger.info(
                    "room=%s 置信度不足(%.2f<%.2f)，不发送：%s",
                    self.room_id, decision.confidence, threshold, decision.text,
                )
                self._log_decision(
                    trigger, snapshot, decision, sent=False, elapsed=elapsed,
                    error=f"low_confidence {decision.confidence:.2f}<{threshold:.2f}",
                )
                return

        self.stats.says += 1
        text = clean_for_send(decision.text)
        content_gate = self._guard.check_text(text)
        if not content_gate.ok:
            self.stats.blocked += 1
            self.stats.last_reason = content_gate.reason
            self._suppress_until = time.time() + 45
            logger.warning("room=%s 回复被拦截: %s | %s", self.room_id, content_gate.reason, text)
            self._guard.record_skipped(text, content_gate.reason)
            self._log_decision(trigger, snapshot, decision, sent=False,
                               error=f"guard: {content_gate.reason}", elapsed=elapsed)
            return

        if self._cfg.app.dry_run:
            self.stats.sends += 1
            self.stats.last_send_ts = time.time()
            self.stats.last_reason = "dry-run 演练模式"
            self._last_reply_ts = time.time()
            self._guard.record(text, dry_run=True)
            logger.info("room=%s [dry-run 演练模式] 将要发送: %s", self.room_id, text)
            self._log_decision(trigger, snapshot, decision, sent=False, dry_run=True, elapsed=elapsed)
            return

        try:
            result = await self._api.send_danmaku(self.room_id, text)
        except BiliAPIError as exc:
            self.stats.errors += 1
            logger.warning("room=%s 发送失败: %s", self.room_id, exc)
            self._log_decision(trigger, snapshot, decision, sent=False, error=str(exc), elapsed=elapsed)
            return

        if result.ok:
            self.stats.sends += 1
            self.stats.last_send_ts = time.time()
            self.stats.last_reason = "已发送"
            self._last_reply_ts = time.time()
            self._guard.record(text)
            # 把自己说过的话也放进上下文：下一轮能看见「我刚才说过什么」，
            # 不至于揪着同一个话题反复说
            self._ctx.add_self_text(text)
            logger.info("room=%s 已发送: %s", self.room_id, text)
            self._log_decision(trigger, snapshot, decision, sent=True, elapsed=elapsed)
        else:
            self.stats.errors += 1
            self._suppress_until = time.time() + 60
            logger.warning("room=%s 发送被拒: code=%s %s", self.room_id, result.code, result.message)
            self._guard.record_skipped(text, f"发送失败:{result.message}")
            self._log_decision(trigger, snapshot, decision, sent=False,
                               error=f"send: {result.code} {result.message}", elapsed=elapsed)
            if is_muted_error(result.code, result.message):
                handled = False
                if self._on_muted is not None:
                    try:
                        handled = bool(self._on_muted(f"code={result.code} {result.message}".strip()))
                    except Exception:  # noqa: BLE001 - 回调出错不能影响收尾
                        logger.exception("room=%s 处理禁言回调失败", self.room_id)
                if handled:
                    self._silenced = True   # 房间已停用，别再折腾
                else:
                    # 开关关掉：不停用、也不静默，照常按节奏继续发，
                    # 靠发送结果探测禁言是否解除（这里只保持通用失败后的 60 秒冷却）
                    logger.info(
                        "room=%s 禁言期间仍会按正常节奏尝试发送，用来探测禁言是否解除",
                        self.room_id,
                    )

    # ---------------------------------------------------------- 禁言

    def is_muted(self) -> bool:
        """自我禁言中返回 True。

        指令功能在面板被关掉时一律返回 False：关掉开关就等于立即解除，
        否则会出现「开关关了却还被锁着」的死角。
        """
        if not self._cfg.reply.mute_command_enabled:
            return False
        return self._mute_until > time.time()

    async def _send_ack(self, text: str) -> bool:
        """发送控制指令的确认语（演练模式不发）。"""
        if self._cfg.app.dry_run:
            logger.info("room=%s [dry-run 演练模式] 不发确认语：%s", self.room_id, text)
            return False
        try:
            result = await self._api.send_danmaku(self.room_id, text)
        except BiliAPIError as exc:
            logger.warning("room=%s 确认语发送失败: %s", self.room_id, exc)
            return False
        if result.ok:
            self.stats.sends += 1
            self.stats.last_send_ts = time.time()
            self._last_reply_ts = self.stats.last_send_ts
            self._guard.record(text)
            self._ctx.add_self_text(text)
            logger.info("room=%s 已发送: %s", self.room_id, text)
            return True
        logger.warning("room=%s 确认语被拒: code=%s %s",
                       self.room_id, result.code, result.message)
        if is_muted_error(result.code, result.message):
            self._silenced = True
            if self._on_muted is not None:
                try:
                    self._on_muted(f"code={result.code} {result.message}".strip())
                except Exception:  # noqa: BLE001
                    logger.exception("room=%s 处理禁言回调失败", self.room_id)
        return False

    async def apply_mute_command(self, minutes: int, *, user: str = "") -> bool:
        """主播发来 /mute N：先回一句确认，然后自我禁言 N 分钟（只听不说）。

        期间仍然接收并记录弹幕/语音，只是不触发回复（不打模型、不发弹幕）。
        """
        if not self._cfg.reply.mute_command_enabled:
            logger.debug("room=%s /mute 指令已在面板关闭，忽略", self.room_id)
            return False
        if self.is_muted():
            logger.info("room=%s 已在自我禁言中，忽略重复的 /mute 指令", self.room_id)
            return False
        minutes = max(1, min(int(minutes), MAX_SELF_MUTE_MINUTES))
        await self._send_ack(f"收到，我安静 {minutes} 分钟")
        self._mute_until = time.time() + minutes * 60
        logger.warning(
            "room=%s 主播「%s」要求自我禁言 %d 分钟（到 %s）：期间只听不说，不再接话",
            self.room_id, user or "主播", minutes,
            time.strftime("%H:%M:%S", time.localtime(self._mute_until)),
        )
        return True

    async def apply_unmute_command(self, *, user: str = "") -> bool:
        """主播发 /unmute：提前解除自我禁言。没在禁言中就什么也不做。"""
        if not self._cfg.reply.mute_command_enabled:
            # 指令功能关掉了：静默清掉残留状态，不往直播间发任何消息
            # （面板关开关本身就等于解除，这里只是兜底）
            self._mute_until = 0.0
            logger.debug("room=%s /mute 功能已关闭，/unmute 不回应", self.room_id)
            return False
        if not self._mute_until or self._mute_until <= time.time():
            logger.info("room=%s 收到 /unmute，但当前没有自我禁言", self.room_id)
            return False
        leave = max(1, int((self._mute_until - time.time()) // 60))
        self._mute_until = 0.0
        await self._send_ack("好，我回来了")
        logger.warning("room=%s 主播「%s」用 /unmute 提前解除禁言（原计划还剩 %d 分钟），恢复接话",
                       self.room_id, user or "主播", leave)
        return True

    # ---------------------------------------------------------- 辅助

    def _describe_trigger(self, trigger: Trigger) -> str:
        text = (trigger.focus or "").strip()
        if trigger.kind == "mention":
            who = trigger.mention_user or "一位观众"
            return (
                f"观众「{who}」在弹幕里 @ 了你，原话：「{text[:80]}」。"
                "这属于被点名：这一条请回应这位观众，而不是继续对主播说话。"
                "不要 @ 回去，不要答应任何要求或承诺，也不要暴露自己的身份信息。"
            )
        if trigger.kind == "speech":
            return f"主播刚说了：「{text[:80]}」，请判断要不要接一句"
        return "定时巡检：请结合主播刚才说的内容和现场氛围，判断是否值得说一句"

    def _log_decision(
        self,
        trigger: Trigger,
        snapshot: ContextSnapshot,
        decision: Optional[Decision],
        *,
        sent: bool,
        elapsed: float,
        dry_run: bool = False,
        error: str = "",
    ) -> None:
        record = {
            "ts": time.time(),
            "room": self.room_id,
            # 场次标记：同一场直播的记录共用它，面板据此把历史记录分场浏览
            "session_started_at": round(self._ctx.started_at, 1),
            "trigger": trigger.kind,
            "focus": trigger.focus[:120],
            "action": decision.action if decision else "error",
            "text": decision.text if decision else "",
            "confidence": decision.confidence if decision else 0.0,
            "reason": decision.reason if decision else "",
            "model_raw": (decision.raw[:500] if decision else ""),
            "sent": sent,
            "dry_run": dry_run,
            "has_frame": self._last_has_frame,
            "elapsed": round(elapsed, 2),
            "error": error,
            "transcript_tail": [t for _, t in snapshot.transcript[-6:]],
            "danmaku_tail": snapshot.danmaku[-8:],
        }
        try:
            with open(self._decision_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("写入决策日志失败: %s", exc)
