# -*- coding: utf-8 -*-
"""WBI 签名。

B 站 2023 起对多个 Web 接口启用 WBI 风控：请求必须带 `wts` + `w_rid`。
`w_rid = md5(sorted_query + mixin_key)`，mixin_key 由 nav 接口返回的
img_key/sub_key 按固定置换表生成，12 小时轮换。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Dict, Optional

import aiohttp

from ..util.log import get_logger

logger = get_logger("bili.wbi")

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_KEY_TTL = 12 * 3600


def get_mixin_key(img_key: str, sub_key: str) -> str:
    orig = img_key + sub_key
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB if i < len(orig))[:32]


def sign_params(params: Dict[str, object], mixin_key: str, wts: Optional[int] = None) -> Dict[str, str]:
    """返回带 wts / w_rid 的新字典（不修改入参）。"""
    signed: Dict[str, str] = {}
    for key, value in params.items():
        signed[key] = "".join(ch for ch in str(value) if ch not in "!'()*")
    signed["wts"] = str(int(wts if wts is not None else time.time()))
    ordered = dict(sorted(signed.items()))
    query = urllib.parse.urlencode(ordered)
    ordered["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return ordered


class WbiSigner:
    """缓存 mixin key，避免每次请求都打 nav 接口。"""

    def __init__(self, session: aiohttp.ClientSession, user_agent: str):
        self._session = session
        self._ua = user_agent
        self._mixin_key: str = ""
        self._fetched_at: float = 0.0

    async def _fetch(self) -> str:
        async with self._session.get(NAV_URL, headers={"User-Agent": self._ua}) as res:
            data = await res.json(content_type=None)
        # 未登录时 nav 返回 code=-101，但 wbi_img 依然可用（WBI 签名不要求登录）
        payload = data.get("data") or {}
        if data.get("code") not in (0, -101) or not payload.get("wbi_img"):
            raise RuntimeError(f"nav 接口失败: code={data.get('code')} msg={data.get('message')}")
        wbi = payload["wbi_img"]
        img_url, sub_url = wbi.get("img_url", ""), wbi.get("sub_url", "")
        if not img_url or not sub_url:
            raise RuntimeError("nav 接口未返回 wbi_img")
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        self._mixin_key = get_mixin_key(img_key, sub_key)
        self._fetched_at = time.time()
        logger.debug("WBI key 刷新完成: %s...", self._mixin_key[:8])
        return self._mixin_key

    async def mixin_key(self, force: bool = False) -> str:
        if force or not self._mixin_key or (time.time() - self._fetched_at) > _KEY_TTL:
            return await self._fetch()
        return self._mixin_key

    async def sign(self, params: Dict[str, object]) -> Dict[str, str]:
        return sign_params(params, await self.mixin_key())
