# -*- coding: utf-8 -*-
"""HTTP 会话：统一 UA、cookie 注入、buvid 初始化。

B 站 2024 年后对匿名请求的风控更严格，`buvid3/buvid4` 几乎是必需项。
这两个值来自 `https://api.bilibili.com/x/frontend/finger/spi`（旧地址
`https://data.bilibili.com/v/` 现在返回 403）。
"""

from __future__ import annotations

import http.cookies
from typing import Dict, Optional

import aiohttp
import yarl

from ..config import Config
from ..util.log import get_logger

logger = get_logger("bili.session")

BUVID_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
COOKIE_DOMAIN = ".bilibili.com"


def apply_cookies(session: aiohttp.ClientSession, cookies: Dict[str, str]) -> None:
    """把 cookie 写入 jar，并显式绑定到 .bilibili.com（跨子域可用）。"""
    for name, value in cookies.items():
        if value is None:
            continue
        jar_cookie = http.cookies.SimpleCookie()
        jar_cookie[name] = str(value)
        jar_cookie[name]["domain"] = COOKIE_DOMAIN
        jar_cookie[name]["path"] = "/"
        try:
            session.cookie_jar.update_cookies(jar_cookie)
        except Exception as exc:  # pragma: no cover - 极少见
            logger.warning("写入 cookie %s 失败: %s", name, exc)


def dump_cookies(session: aiohttp.ClientSession) -> Dict[str, str]:
    """导出与 bilibili 相关的 cookie（用于持久化登录态）。"""
    out: Dict[str, str] = {}
    for url in (
        "https://live.bilibili.com/",
        "https://api.bilibili.com/",
        "https://api.live.bilibili.com/",
        "https://passport.bilibili.com/",
    ):
        for name, morsel in session.cookie_jar.filter_cookies(yarl.URL(url)).items():
            out.setdefault(name, morsel.value)
    return out


async def ensure_buvid(session: aiohttp.ClientSession, user_agent: str) -> Dict[str, str]:
    """确保 jar 里有 buvid3/buvid4，返回本次拿到的值。"""
    existing = session.cookie_jar.filter_cookies(yarl.URL("https://api.bilibili.com/"))
    if existing.get("buvid3") and existing["buvid3"].value:
        return {"buvid3": existing["buvid3"].value}

    try:
        async with session.get(BUVID_SPI_URL, headers={"User-Agent": user_agent}) as res:
            data = await res.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.warning("获取 buvid 失败: %s", exc)
        return {}

    if data.get("code") != 0:
        logger.warning("获取 buvid 失败: %s", data)
        return {}

    payload = data.get("data") or {}
    buvid3, buvid4 = payload.get("b_3", ""), payload.get("b_4", "")
    if buvid3:
        apply_cookies(session, {"buvid3": buvid3, "buvid4": buvid4})
    logger.debug("buvid3=%s...", buvid3[:12])
    return {"buvid3": buvid3, "buvid4": buvid4}


async def create_session(
    cfg: Config, cookies: Optional[Dict[str, str]] = None
) -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(total=cfg.bili.http_timeout_sec, sock_connect=10)
    session = aiohttp.ClientSession(
        timeout=timeout,
        headers={
            "User-Agent": cfg.bili.user_agent,
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    if cookies:
        apply_cookies(session, cookies)
    await ensure_buvid(session, cfg.bili.user_agent)
    return session
