# -*- coding: utf-8 -*-
"""转写文本的清洗与幻觉过滤。

X-ASR 的流式与逐句共用这套规则：
直播语音里大量口头语、静音、复读，直接喂给模型会污染上下文。
"""

from __future__ import annotations

import re
from typing import Optional

# 中文直播场景常见的模型幻觉
_HALLUCINATION_PATTERNS = [
    r"字幕由.{0,20}提供",
    r"字幕组",
    r"请不吝点赞",
    r"订阅.{0,6}转发",
    r"明镜与点点",
    r"感谢观看",
    r"谢谢观看",
    r"amara\.org",
    r"优优独播剧场",
    r"由.{0,10}字幕",
]
_HALLUCINATION_RE = re.compile("|".join(_HALLUCINATION_PATTERNS), re.IGNORECASE)

# 常见语气词/填充词，全是这些则丢弃
_FILLERS = {"嗯", "啊", "呃", "哦", "唉", "诶", "哎", "呀", "哈", "呵", "额", "emmm", "emm"}
_FILLER_CHARS = set("嗯啊呃哦唉诶哎呀哈呵额")


def to_simplified(text: str) -> str:
    """中文输出常混入繁体，统一转简体（zhconv 缺失时原样返回）。"""
    try:
        import zhconv
    except ImportError:
        return text
    return zhconv.convert(text, "zh-cn")


def looks_like_hallucination(text: str) -> Optional[str]:
    """返回命中的问题类型；正常文本返回 None。"""
    if _HALLUCINATION_RE.search(text):
        return "字幕组/结尾套话"
    core = re.sub(r"[\s，。！？、,.!?~～…\-—]", "", text)
    if not core:
        return "空文本"
    if len(core) <= 1:
        return "单字"
    if core in _FILLERS or set(core) <= _FILLER_CHARS:
        return "语气词"
    # 短片段循环（"走过来走过来走过来…"）：同一片段连续出现 4 次以上
    if re.search(r"(.{2,10}?)\1{3,}", core):
        return "复读片段"
    # 同一字符重复超过 6 次且字符种类很少
    for ch in set(core):
        if core.count(ch) > 6 and len(set(core)) <= 2:
            return "复读"
    # 压缩比异常：正常中文文本压缩后通常 > 0.45
    try:
        import zlib

        raw = text.encode("utf-8")
        if len(raw) > 20 and len(zlib.compress(raw, 6)) / len(raw) < 0.35:
            return "低信息量/复读"
    except Exception:  # noqa: BLE001
        pass
    return None


def clean_transcript(text: str) -> str:
    """统一空白 + 转简体。"""
    from ..util.text import normalize

    text = normalize(text or "")
    return to_simplified(text) if text else text
