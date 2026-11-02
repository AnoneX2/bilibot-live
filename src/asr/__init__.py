# -*- coding: utf-8 -*-
"""语音识别：X-ASR（sherpa-onnx，流式 / 逐句两种模式）。"""

from __future__ import annotations

from .base import BaseASR
from .types import ASRResult
from .xasr_engine import (
    CHUNK_CHOICES,
    DEFAULT_CHUNK_MS,
    XASREngine,
    XASRStreamSession,
    resolve_model_dir,
)

__all__ = [
    "ASRResult",
    "BaseASR",
    "CHUNK_CHOICES",
    "DEFAULT_CHUNK_MS",
    "XASREngine",
    "XASRStreamSession",
    "resolve_model_dir",
]
