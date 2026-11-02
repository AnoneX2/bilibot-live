# -*- coding: utf-8 -*-
"""单直播间编排：把采集、ASR、弹幕、回复组装成一个可自愈的 Worker。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import List, Optional

import aiohttp

from ..ai.prompt_store import PromptStore
from ..asr.types import ASRResult
from ..asr.xasr_engine import XASREngine
from ..bili.api import BiliAPI, BiliAPIError, RoomInfo
from ..bili.danmaku import DanmakuHub
from ..config import Config, RoomSpec, load_config
from ..config_writer import save_config
from ..core.context import RoomContext, Trigger
from ..core.guard import GlobalRateLimiter, ReplyGuard
from ..core.reply_engine import (
    ReplyEngine,
    parse_mute_command,
    parse_unmute_command,
)
from ..live.audio import (
    BYTES_PER_SEC,
    SAMPLE_RATE,
    AudioPuller,
    Utterance,
    VadSegmenter,
    merge_utterances,
)
from ..live.stream import StreamResolver
from ..live.video import FrameGrabber
from ..util.log import get_logger

logger = get_logger("core.room")

OFFLINE_POLL_SEC = 60.0
STATUS_LOG_SEC = 300.0
# 运行中每隔多久把本场上下文落一次盘（重启后同一场可以接回来）
CHECKPOINT_SEC = 180.0
# 每隔多久确认一次「主播还在播」
LIVE_CHECK_SEC = 120.0
# get_info.live_status：0=未开播，2=轮播（都代表这一场结束了）；1=直播中
OFFLINE_STATUSES = (0, 2)
# 看到下播后再复检的间隔：接口偶尔会返回一个过期的 live_status
OFFLINE_CONFIRM_SEC = 5.0


class RoomWorker:
    def __init__(
        self,
        cfg: Config,
        spec: RoomSpec,
        *,
        api: BiliAPI,
        session: aiohttp.ClientSession,
        asr: XASREngine,
        provider,
        global_limiter: GlobalRateLimiter,
        stop_event: asyncio.Event,
    ):
        self._cfg = cfg
        self._spec = spec
        self._api = api
        self._session = session
        self._asr = asr
        self._provider = provider
        self._global_limiter = global_limiter
        self._stop = stop_event

        self.room_id: int = 0
        self.title: str = ""
        self._live_time: str = ""
        self._engine: Optional[ReplyEngine] = None
        # 主播（房主）的 uid：只有他能用 /mute 指令
        self._anchor_uid: int = 0
        # 因「本房间被禁言」自动停用了这个直播间：run() 里据此退出
        self._muted_out = False
        self._puller: Optional[AudioPuller] = None
        self._hub: Optional[DanmakuHub] = None
        self._utt_queue: Optional[asyncio.Queue] = None
        self._asr_task: Optional[asyncio.Task] = None
        self._last_asr_ts = 0.0
        # 流式模式队列满时会丢最旧的音频块，累计到 1 秒就提示一次
        self._pcm_dropped_sec = 0.0
        self._last_status_log = 0.0
        self.started_at = time.time()

    @property
    def label(self) -> str:
        return self._spec.alias or str(self._spec.room_id)

    # ---------------------------------------------------------------- 主循环

    async def run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set() and not self._muted_out:
            try:
                init = await self._api.room_init(self._spec.room_id)
            except BiliAPIError as exc:
                logger.warning("[%s] 房间初始化失败: %s（%.0fs 后重试）", self.label, exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue

            self.room_id = init.room_id
            if not init.is_live:
                logger.info("[%s] room=%s 未开播，%.0fs 后重试", self.label, self.room_id, OFFLINE_POLL_SEC)
                await self._sleep(OFFLINE_POLL_SEC)
                continue

            backoff = 5.0
            try:
                await self._run_live(init)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[%s] 直播会话异常退出", self.label)
            await self._sleep(5)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ---------------------------------------------------------------- 直播会话

    async def _run_live(self, init) -> None:
        try:
            info = await self._api.room_info(self.room_id)
        except BiliAPIError as exc:
            # 详情只是「标题/分区/在线人数」这些展示信息，拿不到不该拦着开播
            logger.warning("[%s] 房间详情获取失败（%s），按开播状态继续", self.label, exc)
            info = RoomInfo(room_id=self.room_id, uid=init.uid, live_status=1)
        self.title = info.title or info.uname or str(self.room_id)
        self._live_time = info.live_time
        self._anchor_uid = int(info.uid or init.uid or 0)
        logger.info(
            "[%s] 开播中 room=%s 标题=%s 在线≈%s 分区=%s",
            self.label, self.room_id, self.title, info.online, info.area_name,
        )

        # 同一场直播（开播时间没变）就接着用上次的上下文：热加载、手动重启、
        # 甚至异常退出重建会话，都不该把主播已经说过的内容丢掉
        ctx = self._restore_context(info)
        if ctx is None:
            # 换场（或第一次开播）：上下文从零开始，上一场不参与
            ctx = RoomContext(self._cfg)
            logger.info("[%s] 本场直播开始，上下文已重置（上一场不参与）", self.label)
        resolver = StreamResolver(self._api, self._cfg, self.room_id)
        grabber = FrameGrabber(self._cfg, self.room_id, self._url_provider(resolver))
        guard = ReplyGuard(self._cfg, self.room_id, self._global_limiter, self._cfg.data_path)
        prompt_store = PromptStore(
            self._cfg,
            prompt_dir=self._spec.prompt_dir,
            room_id=self.room_id,
        )
        engine = ReplyEngine(
            self._cfg,
            room_id=self.room_id,
            alias=self._spec.alias,
            title=self.title,
            api=self._api,
            provider=self._provider,
            guard=guard,
            ctx=ctx,
            frame_provider=(lambda: None) if self._cfg.stream.audio_only else grabber.latest_fresh,
            prompt_store=prompt_store,
            session_uid=self._api.uid,
            session_name=self._api.uname,
            on_muted=self._on_room_muted,
        )
        self._engine = engine

        utterance_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._utt_queue = utterance_queue
        internal_stop = asyncio.Event()
        stream_mode = self._cfg.asr.mode == "stream"

        if stream_mode:
            # 真流式：音频块直接进识别器，边说边出字
            puller = AudioPuller(
                self._cfg, self.room_id, self._url_provider(resolver),
                on_pcm=lambda chunk, ts: self._enqueue_pcm(utterance_queue, chunk),
                on_dead_url=resolver.mark_bad,
            )
        else:
            # 逐句：VAD 断句，整句结束后转写
            vad = VadSegmenter(self._cfg, lambda u: self._enqueue(utterance_queue, u))
            puller = AudioPuller(
                self._cfg, self.room_id, self._url_provider(resolver),
                on_pcm=lambda chunk, ts: vad.feed(chunk, ts),
                on_dead_url=resolver.mark_bad,
            )
        hub = DanmakuHub(self._cfg, self._session)
        self._puller, self._hub = puller, hub

        # 先解析地址，失败就直接退出本次会话（外层会重试）
        await resolver.get()

        if stream_mode:
            asr_task = asyncio.create_task(
                self._asr_stream_loop(utterance_queue, ctx, internal_stop), name="asr"
            )
        else:
            asr_task = asyncio.create_task(
                self._asr_loop(utterance_queue, ctx, internal_stop), name="asr"
            )
        self._asr_task = asr_task
        tasks: List[asyncio.Task] = [asr_task]
        if self._cfg.stream.audio_only:
            logger.info("[%s] stream.audio_only=true：不抓帧，只看语音和弹幕", self.label)
        else:
            tasks.append(asyncio.create_task(self._frame_loop(grabber, internal_stop), name="frame"))
        tasks += [
            asyncio.create_task(self._danmaku_loop(hub, ctx, engine, internal_stop), name="danmaku"),
            asyncio.create_task(engine.run(internal_stop), name="reply"),
            asyncio.create_task(self._monitor(resolver, puller, hub, engine, internal_stop), name="monitor"),
            asyncio.create_task(self._checkpoint_loop(ctx, internal_stop), name="checkpoint"),
        ]

        try:
            await puller.start()
            await hub.start(self.room_id)
            await self._load_history(ctx)
            self._log_mention_state()
            logger.info("[%s] 直播会话已就绪（dry_run=%s）", self.label, self._cfg.app.dry_run)
            await internal_stop.wait()
        finally:
            internal_stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await puller.stop()
            await hub.stop()
            self._puller = self._hub = None
            self._asr_task = None
            self._write_session(ctx, True)
            logger.info("[%s] 直播会话已结束：%s", self.label, engine.stats.as_dict())

    def _maybe_mute_command(self, uid: int, text: str):
        """只有主播本人发的指令才算：返回 ("mute", 分钟) / ("unmute", 0) / None。"""
        if not self._cfg.reply.mute_command_enabled:
            # 面板里关掉了指令功能：/mute、/unmute 都当普通弹幕处理，不回应
            return None
        anchor = int(getattr(self, "_anchor_uid", 0) or 0)
        if not anchor or int(uid or 0) != anchor:
            return None
        if parse_unmute_command(text):
            return ("unmute", 0)
        minutes = parse_mute_command(text)
        if minutes is None:
            return None
        return ("mute", minutes)

    def _on_room_muted(self, reason: str) -> bool:
        """B 站 说「已在本房间被禁言」：按配置自动停用这个直播间。

        返回 True 表示已处理（引擎不再继续尝试发送）。
        """
        if self._muted_out:
            return True
        if not self._cfg.app.auto_disable_muted:
            logger.warning(
                "[%s] 已被本房间禁言（%s）；配置里关掉了「被禁言时自动停用直播间」，"
                "继续只听不说，不再尝试发送", self.label, reason,
            )
            return False

        self._muted_out = True
        try:
            path = Path(self._cfg.config_path)
            target = load_config(str(path)) if path.exists() else self._cfg
            changed = False
            for spec in target.app.rooms:
                if int(spec.room_id) == int(self.room_id) and spec.enabled:
                    spec.enabled = False
                    changed = True
            if changed:
                save_config(target, path)
            logger.warning(
                "[%s] 已被本房间禁言（%s），已自动停用该直播间（enabled=false）。"
                "禁言解除后，在面板「直播间」里重新启用即可", self.label, reason,
            )
        except Exception as exc:  # noqa: BLE001 - 写配置失败也不能把进程搞崩
            logger.error("[%s] 自动停用直播间失败（%s）；继续只听不说", self.label, exc)
        return True

    async def _load_history(self, ctx: RoomContext) -> None:
        """历史弹幕只补充「直播氛围」，不会触发回复。

        默认不补（history_danmaku=0）：这是唯一可能把上一场内容带进本场的入口。
        """
        limit = int(self._cfg.context.history_danmaku)
        if limit <= 0:
            logger.info("[%s] 不载入历史弹幕（context.history_danmaku=0）", self.label)
            return
        history = await self._api.danmaku_history(self.room_id)
        if not history:
            logger.info("[%s] 无可用历史弹幕（接口可用但为空，或需要登录）", self.label)
            return
        now = time.time()
        for item in history[-limit:]:
            ctx.add_danmaku(item.uname, item.text, ts=now, uid=item.uid)
        logger.info("[%s] 载入历史弹幕 %d 条作为氛围参考", self.label, len(history))

    def _write_dump(self, dump: dict, final: bool) -> bool:
        """把一份存档写到 sessions/（同步，可能较慢，调用方自己决定放不放线程）。"""
        try:
            counts = dump.get("counts") or {}
            if not counts.get("transcript") and not counts.get("danmaku"):
                return False
            stamp = time.strftime(
                "%Y%m%d-%H%M%S", time.localtime(float(dump.get("started_at") or time.time()))
            )
            path = self._cfg.data_path / "sessions" / f"{self.room_id}-{stamp}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(dump, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            if final:
                logger.info(
                    "[%s] 本场结束：语音 %d 句 / 弹幕 %d 条，存档 %s",
                    self.label, counts.get("transcript", 0), counts.get("danmaku", 0), path,
                )
            return True
        except OSError as exc:
            logger.warning("[%s] 本场上下文存档失败: %s", self.label, exc)
            return False

    def _write_session(self, ctx: RoomContext, final: bool) -> bool:
        """把本场上下文落盘（结束时定稿，运行中定期存档作为重启恢复点）。"""
        return self._write_dump(
            ctx.export_session(self.room_id, self.title, live_time=self._live_time), final
        )

    def _restore_context(self, info: RoomInfo) -> Optional[RoomContext]:
        """开播时间没变 = 同一场直播，把上次的上下文接回来。"""
        if not info.live_time:
            return None
        directory = self._cfg.data_path / "sessions"
        try:
            files = sorted(
                directory.glob(f"{self.room_id}-*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:12]
        except OSError:
            return None
        for file in files:
            try:
                dump = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(dump, dict) or dump.get("live_time") != info.live_time:
                continue
            ctx = RoomContext(self._cfg)
            if not ctx.restore(dump):
                continue
            counts = dump.get("counts") or {}
            started = time.strftime("%H:%M:%S", time.localtime(ctx.started_at))
            logger.info(
                "[%s] 同一场直播（开播于 %s），已接回上次的上下文：语音 %d 句 / 弹幕 %d 条",
                self.label, info.live_time or started,
                counts.get("transcript", 0), counts.get("danmaku", 0),
            )
            return ctx
        return None

    async def _checkpoint_loop(self, ctx: RoomContext, stop: asyncio.Event) -> None:
        """运行中定期存档：进程被杀/机器重启后，同一场还能接上上下文。"""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=CHECKPOINT_SEC)
                return
            except asyncio.TimeoutError:
                pass
            try:
                # 导出在事件循环线程做（deque 正在被本线程追加，换线程读会撞
                # "deque mutated during iteration"）；只有写盘放线程
                dump = ctx.export_session(self.room_id, self.title, live_time=self._live_time)
                await asyncio.to_thread(self._write_dump, dump, False)
            except Exception as exc:  # noqa: BLE001 - 存档失败不影响直播
                logger.debug("[%s] 定期存档失败: %s", self.label, exc)

    # ---------------------------------------------------------------- 子任务

    async def _asr_loop(self, queue: asyncio.Queue, ctx: RoomContext, stop: asyncio.Event) -> None:
        transcript_log = self._cfg.data_path / "transcript" / f"{self.room_id}.jsonl"
        transcript_log.parent.mkdir(parents=True, exist_ok=True)
        pending: List[Utterance] = []
        last_add_ts = 0.0
        merge_wait = max(0.4, self._cfg.asr.merge_wait_sec)
        merge_gap_max = max(0.5, self._cfg.asr.merge_gap_max_sec)
        while not stop.is_set():
            try:
                utterance = await asyncio.wait_for(queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                utterance = None

            if utterance is not None:
                pending.append(utterance)
                last_add_ts = time.monotonic()
            if not pending:
                continue

            total_sec = sum(len(u.audio) for u in pending) / SAMPLE_RATE
            gap = (
                pending[-1].start_ts - pending[-2].end_ts
                if len(pending) >= 2
                else 0.0
            )
            quiet = (time.monotonic() - last_add_ts) >= merge_wait
            # 主播停顿时不要立刻切：等 merge_wait 秒看有没有下半句；
            # 明显的新句子（间隔 > merge_gap_max）或拼得太长时才提前落盘。
            should_flush = (
                total_sec >= self._cfg.asr.max_audio_sec * 0.9
                or (len(pending) >= 2 and gap > merge_gap_max)
                or (utterance is None and quiet)
                or (quiet and total_sec >= 4.0)
            )
            if not should_flush:
                continue

            merged = merge_utterances(pending) if len(pending) > 1 else pending[0]
            pending = []
            try:
                result = await asyncio.wait_for(
                    self._asr.transcribe(merged), timeout=self._cfg.asr.max_audio_sec + 30
                )
            except asyncio.TimeoutError:
                logger.error("[%s] 整句转写超时（%.0fs），丢弃这句", self.label,
                             self._cfg.asr.max_audio_sec + 30)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[%s] 整句转写异常", self.label)
                continue
            self._handle_asr_result(result, ctx, transcript_log)

    async def _asr_stream_loop(
        self, queue: asyncio.Queue, ctx: RoomContext, stop: asyncio.Event
    ) -> None:
        """真流式：把 100ms 音频块直接喂给 X-ASR，端点处吐出整句。"""
        transcript_log = self._cfg.data_path / "transcript" / f"{self.room_id}.jsonl"
        transcript_log.parent.mkdir(parents=True, exist_ok=True)
        session = self._asr.open_stream()
        logger.info("[%s] ASR 流式模式已启动（边说边出字）", self.label)
        last_chunk = time.monotonic()
        try:
            while not stop.is_set():
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    # 音频断流：把最后半句逼出来，避免一直挂着
                    if time.monotonic() - last_chunk > 2.0:
                        try:
                            for result in await session.flush():
                                self._handle_asr_result(result, ctx, transcript_log)
                        except Exception:  # noqa: BLE001
                            logger.exception("[%s] 流式 flush 异常，重建识别流", self.label)
                            session = self._asr.open_stream()
                        last_chunk = time.monotonic()
                    continue
                last_chunk = time.monotonic()
                try:
                    results = await asyncio.wait_for(session.feed(chunk), timeout=30)
                except asyncio.TimeoutError:
                    logger.error("[%s] 流式识别 30s 无响应，重建识别流", self.label)
                    session = self._asr.open_stream()
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    # 单块异常不能把整个 ASR 循环弄死（曾经因奇数长度 PCM 直接退出）
                    logger.exception("[%s] 流式识别异常，重建识别流", self.label)
                    session = self._asr.open_stream()
                    continue
                for result in results:
                    self._handle_asr_result(result, ctx, transcript_log)
        finally:
            try:
                for result in await session.flush():
                    self._handle_asr_result(result, ctx, transcript_log)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] 退出时 flush 异常", self.label)

    def _handle_asr_result(
        self, result: ASRResult, ctx: RoomContext, transcript_log: Path
    ) -> None:
        self._last_asr_ts = time.time()
        record = {
            "ts": time.time(),
            "duration": round(result.duration, 2),
            "elapsed": round(result.elapsed, 2),
            "text": result.text,
            "skipped": result.skipped,
            "skip_reason": result.reason,
        }
        try:
            with open(transcript_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if result.skipped:
            return
        ctx.add_transcript(result.text, ts=time.time(), duration=result.duration)
        # 只有主播说话才会触发判断；弹幕不进这里
        engine_ref = self._engine
        if engine_ref is not None:
            engine_ref.submit(Trigger("speech", focus=result.text, ts=time.time()))

    async def _frame_loop(self, grabber: FrameGrabber, stop: asyncio.Event) -> None:
        interval = self._cfg.stream.frame_interval_sec
        while not stop.is_set():
            try:
                await grabber.grab()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[%s] 抓帧异常", self.label)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _danmaku_loop(
        self, hub: DanmakuHub, ctx: RoomContext, engine: ReplyEngine, stop: asyncio.Event
    ) -> None:
        """弹幕写入上下文；只有「回复了 BOT 的弹幕」才额外触发一次回应判断。

        B 站的「@」不在弹幕正文里，而是 extra.reply_mid（这条弹幕在回复谁）；
        reply_mid 只有等于自己的 UID 才算 @ 了 BOT，所以必须先登录。
        其余弹幕依旧只是氛围：不触发、不打断、也不改变话题。
        """
        uid = int(getattr(self._api, "uid", 0) or 0)
        while not stop.is_set():
            event = await hub.next_event(timeout=2)
            if event is None:
                continue
            if self._api.uid and event.uid == self._api.uid:
                continue
            ctx.add_danmaku(event.uname, event.text, ts=event.ts, uid=event.uid, kind=event.kind)

            # 主播可以用 /mute N 让 BOT 自我禁言、/unmute 提前解除
            # （仍然记录弹幕/语音，只是不接话）
            command = self._maybe_mute_command(event.uid, event.text)
            if command is not None:
                action, minutes = command
                if action == "unmute":
                    await engine.apply_unmute_command(user=event.uname)
                else:
                    await engine.apply_mute_command(minutes, user=event.uname)
                continue

            if not self._cfg.reply.mention_enabled or not uid:
                continue
            if event.reply_mid and event.reply_mid == uid:
                logger.info(
                    "[%s] 弹幕回复了 BOT：%s → %s", self.label, event.uname, event.text[:80]
                )
                engine.submit(
                    Trigger(
                        "mention",
                        reason=f"@{event.uname}",
                        focus=event.text,
                        ts=event.ts,
                        mention_user=event.uname,
                        mention_uid=event.uid,
                    )
                )

    # ---------------------------------------------------------------- @ 提及

    def _log_mention_state(self) -> None:
        if not self._cfg.reply.mention_enabled:
            logger.info("[%s] 被 @ 回应已关闭（reply.mention_enabled=false）", self.label)
            return
        uid = int(getattr(self._api, "uid", 0) or 0)
        if not uid:
            logger.warning(
                "[%s] 被 @ 回应需要登录：登录后才知道自己的 UID，才能比对弹幕的 reply_mid",
                self.label,
            )
            return
        logger.info(
            "[%s] 被 @ 回应已开启：按 reply_mid 识别（mid=%d），最小间隔 %.0fs，每小时最多 %d 次",
            self.label, uid,
            self._cfg.reply.mention_min_interval_sec, self._cfg.reply.mention_max_per_hour,
        )

    async def _monitor(
        self,
        resolver: StreamResolver,
        puller: AudioPuller,
        hub: DanmakuHub,
        engine: ReplyEngine,
        stop: asyncio.Event,
    ) -> None:
        last_live_check = 0.0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
                break
            except asyncio.TimeoutError:
                pass

            now = time.time()
            # 音频断流：让 puller 自己重启，同时刷新播放地址
            stall = self._cfg.stream.stall_timeout_sec
            if puller.last_data_ts and now - puller.last_data_ts > stall * 3:
                logger.warning("[%s] 音频长时间无数据，刷新播放地址", self.label)
                resolver.invalidate()
            elif not puller.last_data_ts and puller.restarts >= 3:
                # 一整场都没收到过音频：不能只看 last_data_ts（它是 0，永远不触发）
                logger.warning("[%s] 开始播了但一直没收到音频，刷新播放地址", self.label)
                resolver.invalidate()

            # ASR 任务意外退出：结束本次直播会话，外层会重建（含识别流）
            asr_task = self._asr_task
            if asr_task is not None and asr_task.done() and not asr_task.cancelled():
                exc = asr_task.exception()
                logger.error("[%s] ASR 任务已退出（%s），重启直播会话", self.label, exc)
                stop.set()
                break

            # 弹幕连接彻底停止（InitError 等）：重启客户端
            if not hub.is_running:
                logger.warning("[%s] 弹幕连接已停止，尝试重启", self.label)
                try:
                    await hub.stop()
                    await hub.start(self.room_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] 重启弹幕失败: %s", self.label, exc)

            # 定期确认是否还在播
            if now - last_live_check > LIVE_CHECK_SEC:
                last_live_check = now
                try:
                    info = await self._api.room_info(self.room_id)
                except BiliAPIError as exc:
                    # 接口失败 ≠ 下播：保留本场上下文，下一轮再确认
                    logger.warning("[%s] 状态检查失败（保留本场上下文）: %s", self.label, exc)
                else:
                    if info.live_status in OFFLINE_STATUSES:
                        if await self._confirm_offline():
                            logger.info(
                                "[%s] 主播已下播（live_status=%s）", self.label, info.live_status
                            )
                            stop.set()
                            break
                    elif (self._live_time and info.live_time
                            and info.live_time != self._live_time):
                        logger.info(
                            "[%s] 检测到新的一场直播（%s → %s），结束本场以清空上下文",
                            self.label, self._live_time, info.live_time,
                        )
                        stop.set()
                        break

            if now - self._last_status_log > STATUS_LOG_SEC:
                self._last_status_log = now
                logger.info(
                    "[%s] 状态: 音频%.0fMB 重启%d 弹幕%s ASR队列=%d ASR最近=%s stats=%s",
                    self.label,
                    puller.bytes_total / 1024 / 1024,
                    puller.restarts,
                    "在线" if hub.is_running else "离线",
                    self._utt_queue.qsize() if self._utt_queue else 0,
                    (f"{now - self._last_asr_ts:.0f}s前" if self._last_asr_ts else "未出字"),
                    engine.stats.as_dict(),
                )

    async def _confirm_offline(self) -> bool:
        """下播前复检一次：单次 live_status=0/2 不足以拆掉一场直播。"""
        await asyncio.sleep(OFFLINE_CONFIRM_SEC)
        try:
            info = await self._api.room_info(self.room_id)
        except BiliAPIError as exc:
            logger.warning("[%s] 下播复检失败（按仍在播处理）: %s", self.label, exc)
            return False
        if info.live_status in OFFLINE_STATUSES:
            return True
        logger.info(
            "[%s] 复检仍在播（live_status=%s），不拆场", self.label, info.live_status
        )
        return False

    # ---------------------------------------------------------------- 工具

    def _url_provider(self, resolver: StreamResolver):
        async def provider() -> str:
            candidate = await resolver.get()
            return candidate.url

        return provider

    @staticmethod
    def _enqueue(queue: asyncio.Queue, utterance: Utterance) -> None:
        try:
            queue.put_nowait(utterance)
        except asyncio.QueueFull:
            logger.warning("ASR 队列已满，丢弃一句")

    def _enqueue_pcm(self, queue: asyncio.Queue, chunk: bytes) -> None:
        """流式模式：丢最旧的 100ms 块，保证识别跟得上直播。

        丢块会让识别器听到的音频在时间上不连续，所以累计丢了多少秒并定期提示，
        免得出现「句子莫名接在一起」时无从排查。
        """
        try:
            queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(chunk)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self._pcm_dropped_sec += len(chunk) / BYTES_PER_SEC
            if self._pcm_dropped_sec >= 1.0:
                logger.warning(
                    "[%s] 识别跟不上直播，累计丢弃约 %.1f 秒音频（可能出现断句/串句）",
                    self.label, self._pcm_dropped_sec,
                )
                self._pcm_dropped_sec = 0.0
