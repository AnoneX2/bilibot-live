# -*- coding: utf-8 -*-
"""ASR 引擎的公共骨架。

子类只需要实现：
- `_load_sync()`            同步加载模型
- `_transcribe_sync(audio)` 同步转写 int16 单声道 16k 音频，返回 (文本, 语言, no_speech)

其余（懒加载、串行执行、结果判定、日志、统计）都在这里，保证两种识别方式行为一致。
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

import numpy as np

from ..config import Config
from ..live.audio import SAMPLE_RATE, Utterance
from ..util.log import get_logger
from ..util.text import is_meaningful_text
from .postprocess import clean_transcript, looks_like_hallucination
from .types import ASRResult


class BaseASR:
    name = "base"
    log_name = "asr"

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._model = None
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, cfg.asr.max_workers), thread_name_prefix="asr"
        )
        self._load_lock = asyncio.Lock()
        self.load_seconds = 0.0
        self.total_audio_sec = 0.0
        self.total_elapsed_sec = 0.0
        self.logger = get_logger(self.log_name)

    # ------------------------------------------------------------ 子类实现

    def _load_sync(self):
        raise NotImplementedError

    def _transcribe_sync(self, audio_int16: np.ndarray) -> Tuple[str, str, float]:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name

    def check_assets(self) -> List[str]:
        """启动前的静态检查：返回问题列表，空表示可用。"""
        return []

    # ------------------------------------------------------------ 生命周期

    async def load(self) -> None:
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.get_running_loop().run_in_executor(
                    self._executor, self._load_sync
                )

    async def warmup(self, seconds: float = 3.0) -> None:
        """用静音音频跑一遍，触发首次推理的算子初始化。"""
        await self.load()
        silence = np.zeros(int(SAMPLE_RATE * seconds), dtype=np.int16)
        started = time.time()
        await self.transcribe(Utterance(audio=silence, start_ts=0.0, end_ts=time.time()))
        self.logger.info("ASR 预热完成，耗时 %.1fs", time.time() - started)

    def close(self) -> None:
        self._model = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ 结果判定

    def judge(self, text: str, duration: float, elapsed: float,
              language: str = "", no_speech: float = 0.0) -> ASRResult:
        """统一的清洗/幻觉过滤/日志，流式与逐句都走这里。"""
        text = clean_transcript(text or "")
        self.total_audio_sec += duration
        self.total_elapsed_sec += elapsed
        result = ASRResult(
            text=text,
            language=language,
            duration=duration,
            elapsed=elapsed,
            no_speech_prob=no_speech,
        )
        if not text:
            result.skipped = True
            result.reason = "空结果"
        elif self._cfg.asr.filter_hallucination:
            reason = looks_like_hallucination(text)
            if reason:
                result.skipped = True
                result.reason = reason
        elif not is_meaningful_text(text):
            result.skipped = True
            result.reason = "无意义文本"

        if not result.skipped:
            self.logger.info("ASR[%.1fs→%.1fs] %s", duration, elapsed, text)
        else:
            self.logger.debug("ASR 丢弃(%.1fs): %s -> %s", duration, text or "(空)", result.reason)
        return result

    # ------------------------------------------------------------ 逐句转写

    async def transcribe(self, utterance: Utterance) -> ASRResult:
        await self.load()
        audio = utterance.audio
        max_samples = int(self._cfg.asr.max_audio_sec * SAMPLE_RATE)
        if len(audio) > max_samples:
            audio = audio[-max_samples:]
        loop = asyncio.get_running_loop()
        duration = len(audio) / SAMPLE_RATE
        started = time.perf_counter()
        try:
            text, language, no_speech = await loop.run_in_executor(
                self._executor, self._transcribe_sync, audio
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("ASR 转写失败")
            return ASRResult(
                text="", duration=duration, elapsed=time.perf_counter() - started,
                skipped=True, reason=f"异常: {exc}",
            )
        return self.judge(
            text, duration, time.perf_counter() - started, language, no_speech
        )
