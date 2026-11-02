# -*- coding: utf-8 -*-
"""面板里的扫码登录：申请二维码 → 轮询 → 保存 cookie。

和终端版 `python -m src.app login` 走同一套接口（见 bili/auth.py），
区别只是二维码以 PNG（base64 data URL）返回给浏览器，状态由后台任务推进。
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from typing import Callable, Dict, Optional

from ..bili.auth import (
    QR_STATE_EXPIRED,
    QR_STATE_OK,
    QR_STATE_SCANNED,
    QR_STATE_WAITING,
    check_login,
    poll_qr,
    qr_state_of,
    request_qr,
    save_cookies,
)
from ..bili.session import create_session, dump_cookies
from ..config import Config
from ..util.log import get_logger

logger = get_logger("panel.login")

QR_TTL_SEC = 180
POLL_INTERVAL_SEC = 1.5


def render_qr_png(url: str) -> bytes:
    """把二维码链接渲染成 PNG 字节（依赖 qrcode/Pillow）。"""
    import qrcode

    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    return buf.getvalue()


class LoginManager:
    def __init__(self, cfg_provider: Callable[[], Config]):
        self._cfg_provider = cfg_provider
        self._state = "idle"          # idle | waiting | scanned | ok | expired | error
        self._message = "尚未开始"
        self._url = ""
        self._png: Optional[bytes] = None
        self._uname = ""
        self._mid = 0
        self._expire_at = 0.0
        self._task: Optional[asyncio.Task] = None
        self._session = None
        self._started_at = 0.0

    # ---------------------------------------------------------------- 状态

    def _payload(self) -> Dict:
        qr = ""
        if self._png and self._state in (QR_STATE_WAITING, QR_STATE_SCANNED):
            qr = "data:image/png;base64," + base64.b64encode(self._png).decode("ascii")
        return {
            "state": self._state,
            "message": self._message,
            "uname": self._uname,
            "mid": self._mid,
            "qr": qr,
            "url": self._url,
            "started_at": self._started_at,
            "expires_in": max(0, int(self._expire_at - time.time())) if self._expire_at else 0,
        }

    async def status(self) -> Dict:
        return self._payload()

    # ---------------------------------------------------------------- 流程

    async def start(self) -> Dict:
        await self.cancel()
        cfg = self._cfg_provider()
        started = time.time()
        try:
            session = await create_session(cfg)
            url, key = await request_qr(session)
            png = render_qr_png(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("申请二维码失败: %s", exc)
            self._state = "error"
            self._message = f"申请二维码失败：{exc}"
            try:
                await session.close()  # type: ignore[name-defined]
            except Exception:  # noqa: BLE001
                pass
            return self._payload()

        cfg.data_path.mkdir(parents=True, exist_ok=True)
        try:
            (cfg.data_path / "login_qr.png").write_bytes(png)
        except OSError:
            pass

        self._session = session
        self._url = url
        self._png = png
        self._state = QR_STATE_WAITING
        self._message = "等待扫码"
        self._uname = ""
        self._mid = 0
        self._started_at = started
        self._expire_at = started + QR_TTL_SEC
        self._task = asyncio.create_task(self._poll(cfg, session, key))
        logger.info("已生成登录二维码（%ds 内有效）", QR_TTL_SEC)
        return self._payload()

    async def _poll(self, cfg: Config, session, key: str) -> None:
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                if time.time() > self._expire_at:
                    self._state = QR_STATE_EXPIRED
                    self._message = "二维码已过期，请重新获取"
                    return
                code, msg = await poll_qr(session, key)
                state = qr_state_of(code)
                if state == QR_STATE_SCANNED:
                    self._state = QR_STATE_SCANNED
                    self._message = "已扫码，请在手机上确认登录"
                elif state == QR_STATE_OK:
                    cookies = dump_cookies(session)
                    save_cookies(cfg.cookie_path, cookies)
                    ok, uname, mid = await check_login(session)
                    self._uname = uname if ok else ""
                    self._mid = int(mid or 0)
                    self._state = QR_STATE_OK
                    self._message = f"登录成功：{self._uname or '（未知昵称）'}"
                    logger.info("扫码登录成功：%s (mid=%s)", self._uname, self._mid)
                    return
                elif state == QR_STATE_EXPIRED:
                    self._state = QR_STATE_EXPIRED
                    self._message = "二维码已过期，请重新获取"
                    return
                else:
                    self._state = QR_STATE_WAITING
                    self._message = msg or "等待扫码"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("扫码轮询异常: %s", exc)
            self._state = "error"
            self._message = f"扫码失败：{exc}"
        finally:
            await self._close_session()

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

    async def cancel(self) -> Dict:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._close_session()
        if self._state not in (QR_STATE_OK, "error"):
            self._state = "idle"
            self._message = "已取消"
        self._png = None
        self._expire_at = 0.0
        return self._payload()
