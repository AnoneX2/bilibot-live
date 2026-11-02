# -*- coding: utf-8 -*-
"""音频采集与 VAD 切句。

链路：
    ffmpeg(常驻) --PCM16/16k/mono--> AudioPuller --bytes--> VadSegmenter --Utterance--> ASR

为什么用常驻 ffmpeg 而不是手工解析 m3u8：
  * ffmpeg 自动处理分片刷新、重连、AAC 解码；
  * 实测 10 秒音频 8.9 秒完成，延迟可接受；
  * 代码量从「下载→排序→concat→清理」降为一个管道。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

import numpy as np

from ..config import Config
from ..util.log import get_logger

logger = get_logger("live.audio")

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # s16le
BYTES_PER_SEC = SAMPLE_RATE * SAMPLE_WIDTH


@dataclass
class Utterance:
    audio: np.ndarray  # int16, mono, 16k
    start_ts: float
    end_ts: float

    @property
    def duration(self) -> float:
        return len(self.audio) / SAMPLE_RATE

    def to_float32(self) -> np.ndarray:
        return (self.audio.astype(np.float32) / 32768.0).clip(-1.0, 1.0)


def pcm_args(url: str, cfg: Config) -> List[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-user_agent", cfg.bili.user_agent,
        "-headers", "Referer: https://live.bilibili.com/\r\n",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", url,
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "pipe:1",
    ]


def merge_utterances(items: List[Utterance]) -> Utterance:
    """把相邻短句拼成一句，减少 ASR 的「半句」误差。"""
    audio = np.concatenate([u.audio for u in items]).astype(np.int16)
    return Utterance(audio=audio, start_ts=items[0].start_ts, end_ts=items[-1].end_ts)


class AudioPuller:
    """常驻 ffmpeg 音频拉流，断流自动重建。"""

    READ_CHUNK = 3200  # 100 ms

    def __init__(
        self,
        cfg: Config,
        room_id: int,
        url_provider: Callable[[], Awaitable[str]],
        on_pcm: Callable[[bytes, float], None],
        on_dead_url: Optional[Callable[[str], None]] = None,
    ):
        self._cfg = cfg
        self._room_id = room_id
        self._url_provider = url_provider
        self._on_pcm = on_pcm
        # 某个地址 15 秒一点数据都没有时回调：换 CDN / 刷新播放地址
        self._on_dead_url = on_dead_url
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stopping = False
        self.bytes_total = 0
        self.last_data_ts = 0.0
        self.restarts = 0

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"audio-{self._room_id}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._kill_proc()

    # ---------------------------------------------------------------- 内部

    async def _kill_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except (ProcessLookupError, asyncio.CancelledError):
            pass

    async def _run(self) -> None:
        backoff = 2.0
        while not self._stopping:
            url = ""
            data_started = False
            try:
                url = await self._url_provider()
                args = pcm_args(url, self._cfg)
                logger.info("room=%s 启动音频管道", self._room_id)
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._proc = proc
                stderr_task = asyncio.create_task(self._drain_stderr(proc))

                while not self._stopping:
                    try:
                        chunk = await asyncio.wait_for(
                            proc.stdout.read(self.READ_CHUNK),  # type: ignore[union-attr]
                            timeout=self._cfg.stream.stall_timeout_sec,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("room=%s 音频 %.0fs 无数据，重建管道",
                                       self._room_id, self._cfg.stream.stall_timeout_sec)
                        break
                    if not chunk:
                        break
                    if not data_started:
                        data_started = True
                        backoff = 2.0
                        logger.info("room=%s 音频流已连通", self._room_id)
                    self.bytes_total += len(chunk)
                    self.last_data_ts = time.time()
                    try:
                        self._on_pcm(chunk, time.time())
                    except Exception:  # noqa: BLE001
                        logger.exception("room=%s PCM 处理异常", self._room_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("room=%s 音频管道异常: %s", self._room_id, exc)
            finally:
                await self._kill_proc()

            if self._stopping:
                break
            if url and not data_started and self._on_dead_url is not None:
                # 一整轮一个字节都没拿到：多半是这个 CDN 节点/格式不行，
                # 让它下次换个地址（否则会一直重试同一个死地址）
                try:
                    self._on_dead_url(url)
                except Exception:  # noqa: BLE001
                    logger.debug("room=%s 报告坏地址失败", self._room_id)
            self.restarts += 1
            logger.info("room=%s %.0fs 后重启音频管道（第 %d 次）",
                        self._room_id, backoff, self.restarts)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.6, 30.0)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        tail: List[str] = []
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                tail.append(text)
                tail[:] = tail[-5:]
                logger.debug("ffmpeg[a]: %s", text)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
        if tail:
            logger.debug("room=%s ffmpeg 最后输出: %s", self._room_id, " | ".join(tail))


class VadSegmenter:
    """RMS 能量门限 VAD（阈值随环境噪声自适应）。

    判定只看音量的 RMS，不做频率特征；阈值 = max(vad.min_rms, 噪声底 ×
    vad.noise_factor)，噪声底只在静音帧更新。

    * 帧长 20 ms；
    * 连续 vad.start_ms（默认 60 ms）有声才进入语音状态（抗突发噪声）；
    * 连续 vad.silence_ms（默认 1000 ms）静音结束一句；
    * 保留 vad.pre_roll_ms 前置音频，避免吃字；
    * 超过 vad.max_speech_ms（默认 12 s）强制切分，控制 ASR 延迟。
    """

    FRAME_MS = 20
    FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320
    FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH      # 640

    def __init__(self, cfg: Config, on_utterance: Callable[[Utterance], None]):
        self._cfg = cfg
        self._on_utterance = on_utterance

        self._start_frames = max(1, cfg.vad.start_ms // self.FRAME_MS)
        self._silence_frames = max(1, cfg.vad.silence_ms // self.FRAME_MS)
        self._min_frames = max(1, cfg.vad.min_speech_ms // self.FRAME_MS)
        self._max_frames = max(self._min_frames + 1, cfg.vad.max_speech_ms // self.FRAME_MS)
        self._pre_roll = max(0, cfg.vad.pre_roll_ms // self.FRAME_MS)

        self._pending = bytearray()
        self._pre_buf: List[np.ndarray] = []
        self._cur_frames: List[np.ndarray] = []
        self._cur_start_ts: float = 0.0
        self._speech = False
        self._silence_run = 0
        self._voiced_run = 0
        self._noise_floor = 120.0

        self.utterances = 0
        self.dropped_short = 0

    # ---------------------------------------------------------------- 状态

    def _rms(self, frame: np.ndarray) -> float:
        if frame.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))

    def _threshold(self) -> float:
        return max(self._cfg.vad.min_rms, self._noise_floor * self._cfg.vad.noise_factor)

    def reset(self) -> None:
        self._pending.clear()
        self._pre_buf.clear()
        self._cur_frames.clear()
        self._speech = False
        self._silence_run = 0
        self._voiced_run = 0

    # ---------------------------------------------------------------- 输入

    def feed(self, pcm: bytes, recv_ts: float) -> None:
        if not pcm:
            return
        self._pending.extend(pcm)
        # 估算当前缓冲区首帧的时间戳（回调里 chunk 是刚收到的）
        chunk_seconds = (len(self._pending) / BYTES_PER_SEC)
        frame_seconds = self.FRAME_MS / 1000.0

        while len(self._pending) >= self.FRAME_BYTES:
            raw = bytes(self._pending[: self.FRAME_BYTES])
            del self._pending[: self.FRAME_BYTES]
            frame = np.frombuffer(raw, dtype=np.int16)
            self._process_frame(frame, recv_ts - chunk_seconds + frame_seconds)
            chunk_seconds -= frame_seconds

    def _process_frame(self, frame: np.ndarray, ts: float) -> None:
        rms = self._rms(frame)
        threshold = self._threshold()
        voiced = rms >= threshold

        if not voiced:
            # 只在静音时更新噪声底
            self._noise_floor = 0.98 * self._noise_floor + 0.02 * rms

        if not self._speech:
            self._pre_buf.append(frame)
            if len(self._pre_buf) > self._pre_roll:
                del self._pre_buf[0]
            if voiced:
                self._voiced_run += 1
                if self._voiced_run >= self._start_frames:
                    self._speech = True
                    self._silence_run = 0
                    self._cur_frames = list(self._pre_buf)
                    self._cur_start_ts = ts - len(self._cur_frames) * (self.FRAME_MS / 1000.0)
                    self._pre_buf.clear()
            else:
                self._voiced_run = 0
            return

        self._cur_frames.append(frame)
        if voiced:
            self._silence_run = 0
        else:
            self._silence_run += 1

        too_long = len(self._cur_frames) >= self._max_frames
        ended = self._silence_run >= self._silence_frames
        if too_long or ended:
            self._finish(ts)

    def _finish(self, ts: float) -> None:
        frames = self._cur_frames
        # 去掉尾部静音
        if self._silence_run > 0:
            frames = frames[: max(0, len(frames) - self._silence_run)]
        self._speech = False
        self._silence_run = 0
        self._voiced_run = 0
        self._cur_frames = []
        self._pre_buf.clear()

        if len(frames) < self._min_frames:
            self.dropped_short += 1
            return

        audio = np.concatenate(frames).astype(np.int16)
        start_ts = max(0.0, ts - len(frames) * self.FRAME_MS / 1000.0)
        utterance = Utterance(audio=audio, start_ts=start_ts, end_ts=ts)
        self.utterances += 1
        try:
            self._on_utterance(utterance)
        except Exception:  # noqa: BLE001
            logger.exception("VAD 回调异常")
