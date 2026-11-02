# -*- coding: utf-8 -*-
"""播放地址解析与缓存。

优先级：HLS-fmp4(avc) > HLS-ts(avc) > HLS-fmp4(hevc) > HLS-ts(hevc) > FLV。
HLS 的分片更小、起播更快，适合「持续听 + 偶尔抓帧」；FLV 作为兜底。
"""

from __future__ import annotations

import time
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from ..bili.api import BiliAPI, BiliAPIError, StreamCandidate
from ..config import Config
from ..util.log import get_logger

logger = get_logger("live.stream")

# (protocol, format, codec) 优先级
_PREFERENCE = [
    ("http_hls", "fmp4", "avc"),
    ("http_hls", "ts", "avc"),
    ("http_hls", "fmp4", "hevc"),
    ("http_hls", "ts", "hevc"),
    ("http_stream", "flv", "avc"),
    ("http_stream", "flv", "hevc"),
]


def host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def pick_best(
    candidates: List[StreamCandidate], avoid: Optional[Iterable[str]] = None
) -> Optional[StreamCandidate]:
    """按优先级挑一个地址。`avoid` 是拉不到数据的 CDN 主机名，尽量避开。"""
    avoid_hosts = {h for h in (avoid or ()) if h}
    pools = [
        [c for c in candidates if host_of(c.url) not in avoid_hosts],
        list(candidates),  # 全都被避开过就退回全部，别把自己锁死
    ]
    for pool in pools:
        if not pool:
            continue
        by_key = {(c.protocol, c.format, c.codec): c for c in pool}
        for key in _PREFERENCE:
            if key in by_key:
                return by_key[key]
        # 没有完全匹配的就按 HLS 优先
        hls = [c for c in pool if c.is_hls]
        return (hls or pool)[0]
    return None


class StreamResolver:
    def __init__(self, api: BiliAPI, cfg: Config, room_id: int):
        self._api = api
        self._cfg = cfg
        self._room_id = room_id
        self._current: Optional[StreamCandidate] = None
        self._resolved_at: float = 0.0
        # 拉不到数据的 CDN 主机 → 避开的截止时间
        self._avoid: dict = {}

    @property
    def current(self) -> Optional[StreamCandidate]:
        return self._current

    def is_stale(self, now: Optional[float] = None) -> bool:
        if self._current is None:
            return True
        now = now or time.time()
        max_age = self._cfg.stream.refresh_sec
        if self._current.expires_at:
            # 过期前 5 分钟就刷新
            remaining = self._current.expires_at - now - 300
            max_age = min(max_age, max(60, remaining))
        return (now - self._resolved_at) > max_age

    def _live_avoid(self) -> set:
        now = time.time()
        self._avoid = {h: exp for h, exp in self._avoid.items() if exp > now}
        return set(self._avoid)

    def mark_bad(self, url: str, ttl: float = 900.0) -> None:
        """某个地址一直拉不到数据：记住它的主机，换一个 CDN 重来。

        B 站播放地址会分配不同 CDN 节点，偶尔会碰到连得上但一直不回数据的节点；
        以前这种情况会一直重试同一个地址，直到手动重启 BOT。
        """
        host = host_of(url)
        if not host:
            return
        self._avoid[host] = time.time() + ttl
        logger.warning("room=%s 这个 CDN 拉不到数据，%d 分钟内避开：%s",
                       self._room_id, int(ttl // 60), host)
        self.invalidate()

    async def get(self, force: bool = False) -> StreamCandidate:
        if not force and not self.is_stale():
            return self._current  # type: ignore[return-value]

        candidates = await self._api.playurl(self._room_id, qn=self._cfg.stream.qn)
        best = pick_best(candidates, self._live_avoid())
        if best is None:
            raise BiliAPIError(-2, "没有可用的播放地址")
        self._current = best
        self._resolved_at = time.time()
        expires = (
            time.strftime("%H:%M:%S", time.localtime(best.expires_at))
            if best.expires_at
            else "未知"
        )
        logger.info(
            "room=%s 播放地址: %s/%s/%s qn=%s 过期=%s 主机=%s",
            self._room_id, best.protocol, best.format, best.codec, best.qn, expires,
            host_of(best.url),
        )
        return best

    def invalidate(self) -> None:
        self._resolved_at = 0.0
