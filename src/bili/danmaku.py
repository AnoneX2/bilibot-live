# -*- coding: utf-8 -*-
"""弹幕 WebSocket 接入。

blivedm 依赖两个已经失效的接口：
  * `getInfoByRoom`（取真实房间号/主播 uid）→ 现在返回 -352
  * `getDanmuInfo`（取弹幕服务器 + token）→ 无 WBI 签名返回 -352
  * `data.bilibili.com/v/`（取 buvid）→ 403

这里不修改第三方库源码，而是继承 `BLiveClient` 覆盖上述三个初始化步骤，
其余（心跳、解包、重连、消息模型）复用 blivedm。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp
import yarl

import blivedm
import blivedm.models.web as web_models
from blivedm import ws_base

from ..config import Config
from ..util.log import get_logger
from .wbi import WbiSigner

logger = get_logger("bili.danmaku")

ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init"


@dataclass
class DanmakuEvent:
    room_id: int
    uid: int
    uname: str
    text: str
    ts: float
    kind: str = "danmaku"  # danmaku | super_chat
    medal_level: int = 0
    is_admin: bool = False
    # 「回复/被 @」不是弹幕正文里的 @，而是协议里的 reply_mid / reply_uname：
    # 有人回复你时，这条弹幕的 extra 里会带上你的 mid。
    reply_mid: int = 0
    reply_uname: str = ""

    def render(self, base_ts: Optional[float] = None) -> str:
        if base_ts is None:
            prefix = time.strftime("%H:%M:%S", time.localtime(self.ts))
        else:
            prefix = f"+{max(0, int(self.ts - base_ts))}s"
        tag = "SC" if self.kind == "super_chat" else ""
        name = f"[{tag}]{self.uname}" if tag else self.uname
        return f"{prefix} {name}: {self.text}"


class PatchedBLiveClient(blivedm.BLiveClient):
    """修复了房间初始化 / buvid / 弹幕服务器签名的客户端。"""

    def __init__(self, room_id: int, *, session: aiohttp.ClientSession, user_agent: str,
                 heartbeat_interval: float = 30):
        super().__init__(room_id, session=session, heartbeat_interval=heartbeat_interval)
        self._ua = user_agent
        self._signer = WbiSigner(session, user_agent)

    # -------------------------------------------------- buvid

    async def _init_buvid(self) -> bool:
        cookies = self._session.cookie_jar.filter_cookies(yarl.URL("https://api.bilibili.com/"))
        if cookies.get("buvid3") and cookies["buvid3"].value:
            return True
        try:
            async with self._session.get(
                "https://api.bilibili.com/x/frontend/finger/spi",
                headers={"User-Agent": self._ua},
            ) as res:
                data = await res.json(content_type=None)
            b3 = (data.get("data") or {}).get("b_3", "")
            b4 = (data.get("data") or {}).get("b_4", "")
            if b3:
                import http.cookies

                ck = http.cookies.SimpleCookie()
                ck["buvid3"] = b3
                ck["buvid3"]["domain"] = ".bilibili.com"
                ck["buvid3"]["path"] = "/"
                ck["buvid4"] = b4
                ck["buvid4"]["domain"] = ".bilibili.com"
                ck["buvid4"]["path"] = "/"
                self._session.cookie_jar.update_cookies(ck)
            return bool(b3)
        except Exception as exc:  # noqa: BLE001
            logger.warning("room=%s 初始化 buvid 失败: %s", self._tmp_room_id, exc)
            return False

    # -------------------------------------------------- 房间号

    async def _init_room_id_and_owner(self) -> bool:
        try:
            async with self._session.get(
                ROOM_INIT_URL,
                headers={"User-Agent": self._ua},
                params={"id": self._tmp_room_id},
            ) as res:
                data = await res.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("room=%s room_init 请求失败: %s", self._tmp_room_id, exc)
            return False
        if data.get("code") != 0:
            logger.warning("room=%s room_init 失败: %s", self._tmp_room_id, data)
            return False
        self._room_id = int(data["data"]["room_id"])
        self._room_owner_uid = int(data["data"].get("uid") or 0)
        return True

    # -------------------------------------------------- 弹幕服务器

    async def _init_host_server(self) -> bool:
        try:
            params = await self._signer.sign({"id": self._room_id, "type": 0})
            async with self._session.get(
                "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
                headers={"User-Agent": self._ua},
                params=params,
            ) as res:
                data = await res.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("room=%s getDanmuInfo 请求失败: %s", self._room_id, exc)
            return False

        if data.get("code") != 0:
            logger.warning("room=%s getDanmuInfo 失败: %s", self._room_id, data)
            return False
        payload = data.get("data") or {}
        hosts = payload.get("host_list") or []
        if not hosts:
            return False
        self._host_server_list = hosts
        self._host_server_token = payload.get("token")
        logger.debug("room=%s 弹幕服务器: %s", self._room_id, [h.get("host") for h in hosts])
        return True


def _reply_target(message: web_models.DanmakuMessage) -> Tuple[int, str]:
    """从弹幕的 mode_info.extra 里取「这条弹幕回复了谁」。

    B 站的「@ 某人」在直播弹幕里并不出现在正文，而是 extra.reply_mid / reply_uname；
    正文里通常没有 @ 字样，也不能靠 print 出来的文本判断。
    """
    info = message.mode_info or {}
    extra = info.get("extra") if isinstance(info, dict) else None
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (TypeError, ValueError):
            return 0, ""
    if not isinstance(extra, dict):
        return 0, ""
    return _to_int(extra.get("reply_mid")), str(extra.get("reply_uname") or "")


class _HubHandler(blivedm.BaseHandler):
    def __init__(self, hub: "DanmakuHub", room_id: int):
        self._hub = hub
        self._room_id = room_id

    def _on_danmaku(self, client: ws_base.WebSocketClientBase, message: web_models.DanmakuMessage):
        reply_mid, reply_uname = _reply_target(message)
        self._hub._emit(
            DanmakuEvent(
                room_id=self._room_id,
                uid=int(message.uid or 0),
                uname=message.uname or "",
                text=message.msg or "",
                ts=time.time(),
                medal_level=_to_int(message.medal_level),
                is_admin=bool(message.admin),
                reply_mid=reply_mid,
                reply_uname=reply_uname,
            )
        )

    def _on_super_chat(
        self, client: ws_base.WebSocketClientBase, message: web_models.SuperChatMessage
    ):
        self._hub._emit(
            DanmakuEvent(
                room_id=self._room_id,
                uid=int(message.uid or 0),
                uname=message.uname or "",
                text=message.message or "",
                ts=time.time(),
                kind="super_chat",
                is_admin=False,
            )
        )

    def on_client_stopped(self, client: ws_base.WebSocketClientBase, exception: Optional[Exception]):
        self._hub._on_stopped(self._room_id, exception)


def _to_int(value) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


class DanmakuHub:
    """单房间弹幕客户端容器：事件队列 + 最近弹幕缓存。"""

    def __init__(self, cfg: Config, session: aiohttp.ClientSession,
                 on_event: Optional[Callable[[DanmakuEvent], None]] = None):
        self._cfg = cfg
        self._session = session
        self._on_event = on_event
        self._client: Optional[PatchedBLiveClient] = None
        self._handler: Optional[_HubHandler] = None
        self.room_id: int = 0
        self.queue: asyncio.Queue[DanmakuEvent] = asyncio.Queue(maxsize=1000)
        self.recent: List[DanmakuEvent] = []
        self.connected_since: float = 0.0
        self._stopped_exc: Optional[Exception] = None

    # -------------------------------------------------- 生命周期

    async def start(self, room_id: int) -> None:
        self.room_id = room_id
        self._client = PatchedBLiveClient(
            room_id, session=self._session, user_agent=self._cfg.bili.user_agent
        )
        self._handler = _HubHandler(self, room_id)
        self._client.set_handler(self._handler)
        self._client.start()
        self.connected_since = time.time()
        logger.info("弹幕连接已启动 room=%s", room_id)

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop_and_close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭弹幕客户端异常: %s", exc)
            self._client = None
            self._handler = None

    @property
    def is_running(self) -> bool:
        return self._client is not None and self._client.is_running

    # -------------------------------------------------- 事件

    def _emit(self, event: DanmakuEvent) -> None:
        self.recent.append(event)
        if len(self.recent) > 200:
            del self.recent[: len(self.recent) - 200]
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("room=%s 弹幕队列已满，丢弃一条", self.room_id)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                logger.exception("弹幕回调异常")

    def _on_stopped(self, room_id: int, exc: Optional[Exception]) -> None:
        self._stopped_exc = exc
        if exc is not None:
            logger.warning("room=%s 弹幕连接停止: %s", room_id, exc)
        else:
            logger.info("room=%s 弹幕连接已关闭", room_id)

    async def next_event(self, timeout: float = 5.0) -> Optional[DanmakuEvent]:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
