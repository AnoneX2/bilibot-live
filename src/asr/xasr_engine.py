# -*- coding: utf-8 -*-
"""X-ASR 引擎：复旦 / SII 的中英双语 zipformer，ONNX 流式模型 + sherpa-onnx。

两种识别方式（`asr.mode`）：
- `stream`  ：真流式，边说话边解码，端点处吐出一整句（延迟最低、上下文最完整）；
- `segment` ：VAD 断句后整句转写（能挡掉纯 BGM，代价是多等一次静音确认）。

模型来自用户下载的 X-ASR 仓库（`X-ASR-zh-en/deployment/models/chunk-<N>ms-model`），
已复制到 `data/models/xasr/`，运行时只读，不会修改 X-ASR 仓库。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import List

import numpy as np

from ..config import PROJECT_ROOT, Config
from ..live.audio import SAMPLE_RATE
from .base import BaseASR
from .types import ASRResult

# X-ASR 官方导出四种流式 chunk，越大越准（实测 1920ms 最准，仍 35 倍实时）
CHUNK_CHOICES = (160, 480, 960, 1920)
DEFAULT_CHUNK_MS = 1920

# 末尾补静音，让最后一个不完整的 chunk 也能被解出来（sherpa 官方示例同样做法）
TAIL_PAD_SEC = 0.66
# 流式会话 flush 时补的静音：够了就会触发一次端点
FLUSH_PAD_SEC = 0.8

# 一个可用的 chunk 目录必须同时具备这四个文件
_REQUIRED = ("tokens.txt", "decoder-{c}ms.onnx", "joiner-{c}ms.onnx", "encoder-{c}ms.onnx")


def _is_model_dir(path: Path, chunk: int) -> bool:
    if not path.is_dir():
        return False
    return all((path / name.format(c=chunk)).is_file() for name in _REQUIRED)


def model_search_dirs(cfg: Config) -> List[Path]:
    """按优先级列出候选目录：配置目录 → 环境变量 → 同级 X-ASR 仓库。"""
    chunk = int(cfg.asr.xasr_chunk_ms)
    roots: List[Path] = [cfg.path(cfg.asr.xasr_model_dir)]
    env = os.environ.get("XASR_MODEL_ROOT", "").strip()
    if env:
        roots.append(Path(env))
    # 用户下载的 X-ASR（与仓库目录同级），只读引用，方便直接试其它 chunk
    sibling = PROJECT_ROOT.parent / "X-ASR"
    roots.append(sibling / "X-ASR-zh-en" / "deployment" / "models")

    seen, ordered = set(), []
    for root in roots:
        # 既支持「指向模型的父目录」，也支持「直接指向 chunk-1920ms-model」
        for candidate in (root / f"chunk-{chunk}ms-model", root):
            key = str(candidate).lower()
            if key not in seen:
                seen.add(key)
                ordered.append(candidate)
    return ordered


def resolve_model_dir(cfg: Config) -> Path:
    """返回实际使用的模型目录；找不到时抛出带排查提示的异常。"""
    chunk = int(cfg.asr.xasr_chunk_ms)
    for candidate in model_search_dirs(cfg):
        if _is_model_dir(candidate, chunk):
            return candidate
    searched = "\n".join(f"    - {d}" for d in model_search_dirs(cfg))
    raise FileNotFoundError(
        f"找不到 X-ASR 模型（chunk={chunk}ms）。已查找：\n{searched}\n"
        f"  解决：把 X-ASR 的 X-ASR-zh-en/deployment/models/chunk-{chunk}ms-model "
        f"整个目录复制到 {cfg.path(cfg.asr.xasr_model_dir)} 下，"
        f"或用 XASR_MODEL_ROOT 指向已有的 models 目录。"
    )


class XASREngine(BaseASR):
    name = "xasr"
    log_name = "asr.xasr"

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        # 一个 recognizer 多 stream 并发在 sherpa-onnx 里没保证，串行最稳
        self._decode_lock = threading.Lock()

    def describe(self) -> str:
        chunk = int(self._cfg.asr.xasr_chunk_ms)
        try:
            where = str(resolve_model_dir(self._cfg))
        except FileNotFoundError:
            where = "(未找到模型)"
        return (
            f"X-ASR {self._cfg.asr.mode} chunk={chunk}ms "
            f"threads={self._cfg.asr.xasr_threads} model={where}"
        )

    def check_assets(self) -> List[str]:
        try:
            resolve_model_dir(self._cfg)
        except Exception as exc:  # noqa: BLE001
            return [str(exc)]
        return []

    def _load_sync(self):
        import sherpa_onnx

        chunk = int(self._cfg.asr.xasr_chunk_ms)
        model_dir = resolve_model_dir(self._cfg)
        threads = max(1, int(self._cfg.asr.xasr_threads))
        self.logger.info(
            "加载 X-ASR 模型 dir=%s chunk=%sms threads=%s mode=%s",
            model_dir, chunk, threads, self._cfg.asr.mode,
        )
        started = time.time()
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / f"encoder-{chunk}ms.onnx"),
            decoder=str(model_dir / f"decoder-{chunk}ms.onnx"),
            joiner=str(model_dir / f"joiner-{chunk}ms.onnx"),
            num_threads=threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            # 与 BOT 的 VAD 使用同一个断句阈值，两种模式切句一致
            rule2_min_trailing_silence=max(0.4, self._cfg.vad.silence_ms / 1000.0),
            rule3_min_utterance_length=float(self._cfg.asr.max_audio_sec),
            debug=False,
        )
        self.load_seconds = time.time() - started
        self.logger.info("X-ASR 模型加载完成，耗时 %.1fs", self.load_seconds)
        return recognizer

    def _transcribe_sync(self, audio_int16: np.ndarray):
        assert self._model is not None
        audio = (audio_int16.astype(np.float32) / 32768.0).clip(-1.0, 1.0)
        with self._decode_lock:
            stream = self._model.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio)
            stream.accept_waveform(
                SAMPLE_RATE, np.zeros(int(TAIL_PAD_SEC * SAMPLE_RATE), dtype=np.float32)
            )
            stream.input_finished()
            while self._model.is_ready(stream):
                self._model.decode_stream(stream)
            text = self._model.get_result(stream)
        return str(text or "").strip(), "zh-en", 0.0

    # ------------------------------------------------------------ 流式会话

    def open_stream(self) -> "XASRStreamSession":
        """为一个直播间开一路流式识别（模型共享，流各自独立）。"""
        return XASRStreamSession(self)


class XASRStreamSession:
    """持续喂 100ms PCM，端点处返回整句（可能一次返回 0 句或多句）。"""

    def __init__(self, engine: XASREngine):
        self._engine = engine
        self._stream = None
        self._pending_samples = 0
        # 管道读到的字节数不保证是 2 的倍数，这里缓存半个采样，下块补上
        self._carry = b""

    def _ensure_stream(self):
        if self._stream is None:
            assert self._engine._model is not None, "X-ASR 模型尚未加载"
            self._stream = self._engine._model.create_stream()
        return self._stream

    async def feed(self, pcm: bytes) -> List[ASRResult]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._engine._executor, self._feed_sync, pcm)

    async def flush(self) -> List[ASRResult]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._engine._executor, self._flush_sync)

    # -------------------------------------------------- 同步实现（执行器里跑）

    def _feed_sync(self, pcm: bytes) -> List[ASRResult]:
        data = (self._carry + pcm) if self._carry else pcm
        if len(data) % 2:
            # 保留最后半个采样，等下一块拼齐（否则 np.frombuffer 会抛 ValueError）
            self._carry = data[-1:]
            data = data[:-1]
        else:
            self._carry = b""
        audio = np.frombuffer(data, dtype=np.int16)
        if audio.size == 0:
            return []
        stream = self._ensure_stream()
        self._pending_samples += int(audio.size)
        started = time.perf_counter()
        with self._engine._decode_lock:
            stream.accept_waveform(
                SAMPLE_RATE, audio.astype(np.float32) / 32768.0
            )
            while self._engine._model.is_ready(stream):
                self._engine._model.decode_stream(stream)
            elapsed = time.perf_counter() - started
            if not self._engine._model.is_endpoint(stream):
                return []
            text = self._engine._model.get_result(stream)
            duration = self._pending_samples / SAMPLE_RATE
            self._pending_samples = 0
            self._engine._model.reset(stream)
        return [self._engine.judge(text, duration, elapsed, "zh-en")]

    def _flush_sync(self) -> List[ASRResult]:
        """补静音把最后一句逼出来，然后丢弃这条流（下次 feed 会新建）。"""
        if self._stream is None:
            return []
        stream = self._stream
        started = time.perf_counter()
        with self._engine._decode_lock:
            stream.accept_waveform(
                SAMPLE_RATE, np.zeros(int(FLUSH_PAD_SEC * SAMPLE_RATE), dtype=np.float32)
            )
            stream.input_finished()
            while self._engine._model.is_ready(stream):
                self._engine._model.decode_stream(stream)
            elapsed = time.perf_counter() - started
            text = self._engine._model.get_result(stream)
            duration = self._pending_samples / SAMPLE_RATE
        self._stream = None
        self._pending_samples = 0
        if not text or not str(text).strip():
            return []
        return [self._engine.judge(text, duration, elapsed, "zh-en")]
