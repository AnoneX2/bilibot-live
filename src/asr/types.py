# -*- coding: utf-8 -*-
"""ASR 的公共结果类型与引擎接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass
class ASRResult:
    text: str
    language: str = ""
    duration: float = 0.0      # 音频时长
    elapsed: float = 0.0       # 转写耗时
    no_speech_prob: float = 0.0
    skipped: bool = False
    reason: str = ""

    @property
    def rtf(self) -> float:
        return self.elapsed / self.duration if self.duration > 0 else 0.0


TranscriptTuple = Tuple[str, str, float]
