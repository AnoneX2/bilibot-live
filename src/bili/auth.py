# -*- coding: utf-8 -*-
"""登录态管理：cookie 持久化、登录检查、扫码登录。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiohttp

from ..config import Config
from ..util.log import get_logger
from .session import apply_cookies, dump_cookies

logger = get_logger("bili.auth")

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

_REQUIRED = ("SESSDATA", "bili_jct", "DedeUserID")


def load_cookies(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("cookie 文件读取失败: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def save_cookies(path: Path, cookies: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def has_login_cookies(cookies: Dict[str, str]) -> bool:
    return all(cookies.get(k) for k in _REQUIRED)


async def check_login(session: aiohttp.ClientSession) -> Tuple[bool, str, int]:
    """返回 (是否登录, 用户名, mid)。"""
    try:
        async with session.get(NAV_URL) as res:
            data = await res.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.warning("登录检查失败: %s", exc)
        return False, "", 0
    if data.get("code") != 0:
        return False, "", 0
    payload = data.get("data") or {}
    return bool(payload.get("isLogin")), payload.get("uname", ""), int(payload.get("mid") or 0)


def _print_qr(url: str, png_path: Path) -> None:
    try:
        import qrcode  # 可选依赖
    except ImportError:
        print("未安装 qrcode，请手动打开下列链接扫码：")
        print(url)
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except Exception:  # pragma: no cover - 终端编码问题
        pass
    try:
        qr.make_image().save(png_path)
        print(f"二维码已保存: {png_path}")
    except Exception as exc:  # pragma: no cover
        logger.warning("保存二维码失败: %s", exc)
    print(f"若二维码无法识别，请浏览器打开: {url}")


# 扫码状态码（B 站 passport 接口）
QR_STATE_WAITING = "waiting"      # 86101 未扫码
QR_STATE_SCANNED = "scanned"      # 86090 已扫码未确认
QR_STATE_OK = "ok"                # 0 登录成功
QR_STATE_EXPIRED = "expired"      # 86038 二维码过期


def qr_state_of(code: int) -> str:
    if code == 0:
        return QR_STATE_OK
    if code == 86038:
        return QR_STATE_EXPIRED
    if code == 86090:
        return QR_STATE_SCANNED
    if code == 86101:
        return QR_STATE_WAITING
    return "unknown"


async def request_qr(session: aiohttp.ClientSession) -> Tuple[str, str]:
    """申请一个登录二维码，返回 (扫码链接, qrcode_key)。"""
    async with session.get(QR_GENERATE_URL) as res:
        data = await res.json(content_type=None)
    if data.get("code") != 0:
        raise RuntimeError(f"获取二维码失败: {data}")
    payload = data.get("data") or {}
    url = payload.get("url") or ""
    key = payload.get("qrcode_key") or ""
    if not url or not key:
        raise RuntimeError(f"二维码响应缺少字段: {data}")
    return url, key


async def poll_qr(session: aiohttp.ClientSession, qrcode_key: str) -> Tuple[int, str]:
    """轮询扫码结果，返回 (状态码, 提示信息)。状态码见 qr_state_of()。"""
    async with session.get(QR_POLL_URL, params={"qrcode_key": qrcode_key}) as res:
        poll = await res.json(content_type=None)
    payload = poll.get("data") or {}
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = -1
    return code, str(payload.get("message") or "")


async def qr_login(session: aiohttp.ClientSession, cfg: Config, timeout_sec: int = 180) -> Dict[str, str]:
    """扫码登录（终端版），成功后返回 cookie 字典（并已写入 session）。"""
    qr_url, qrcode_key = await request_qr(session)

    _print_qr(qr_url, cfg.data_path / "login_qr.png")
    print("请使用 B 站手机客户端扫码并在手机上确认登录……")

    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2)
        code, message = await poll_qr(session, qrcode_key)
        if code == 0:
            cookies = dump_cookies(session)
            save_cookies(cfg.cookie_path, cookies)
            print(f"登录成功: {message}")
            return cookies
        if code == 86038:
            raise RuntimeError("二维码已过期，请重新执行登录")
        # 86101 未扫码 / 86090 已扫码未确认：继续轮询
    raise TimeoutError("扫码登录超时")


async def ensure_login(
    session: aiohttp.ClientSession, cfg: Config, interactive: bool = True
) -> Tuple[bool, str, int]:
    """检查登录态；必要时（interactive=True）走扫码登录。"""
    is_login, uname, mid = await check_login(session)
    if is_login:
        logger.info("登录态有效: %s (mid=%s)", uname, mid)
        return True, uname, mid

    if not interactive:
        logger.warning("未登录：只能读取公开接口，无法发送弹幕")
        return False, "", 0

    print("未检测到有效登录态，开始扫码登录……")
    cookies = await qr_login(session, cfg)
    apply_cookies(session, cookies)
    return await check_login(session)
