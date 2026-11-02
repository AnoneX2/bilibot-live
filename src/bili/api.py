# -*- coding: utf-8 -*-
"""B 站 HTTP 接口封装。

设计原则：
1. 只做「调用 + 解析 + 错误分类」，不做业务编排；
2. 所有异常统一为 BiliAPIError，调用方可按 code 决定退避策略；
3. 需要 WBI 的接口（getDanmuInfo）自动签名。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
import yarl

from ..config import Config
from ..util.log import get_logger
from .wbi import WbiSigner

logger = get_logger("bili.api")

ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init"
ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
ROOM_INFO_OLD_URL = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
PLAYURL_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
DANMAKU_HISTORY_URL = "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory"
DANMAKU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
SEND_MSG_URL = "https://api.live.bilibili.com/msg/send"
ROOM_STATUS_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
ANCHOR_INFO_URL = "https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room"

# 需要退避的错误码
RISK_CODES = {-352, -412, -509}
AUTH_CODES = {-101, -111}


class BiliAPIError(RuntimeError):
    def __init__(self, code: int, message: str, endpoint: str = ""):
        super().__init__(f"[{endpoint}] code={code} message={message}")
        self.code = code
        self.message = message
        self.endpoint = endpoint

    @property
    def is_risk(self) -> bool:
        return self.code in RISK_CODES

    @property
    def is_auth(self) -> bool:
        return self.code in AUTH_CODES


@dataclass
class RoomInfo:
    room_id: int
    uid: int = 0
    live_status: int = 0
    title: str = ""
    uname: str = ""
    online: int = 0
    area_name: str = ""
    # 本场开播时间（get_info 的 live_time），用于识别「上一场结束、新一场开始」
    live_time: str = ""

    @property
    def is_live(self) -> bool:
        return self.live_status == 1


@dataclass
class StreamCandidate:
    protocol: str          # http_stream / http_hls
    format: str            # flv / ts / fmp4
    codec: str             # avc / hevc
    url: str
    qn: int = 0
    expires_at: float = 0.0

    @property
    def is_hls(self) -> bool:
        return self.protocol == "http_hls"


@dataclass
class SendResult:
    ok: bool
    code: int = 0
    message: str = ""
    forbidden: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoryDanmaku:
    uid: int
    uname: str
    text: str
    ts: float


def _parse_expires(url: str) -> float:
    """从播放地址里解析过期时间。B 站常见为十进制时间戳，个别为十六进制。"""
    try:
        qs = parse_qs(urlparse(url).query)
        raw = (qs.get("expires") or qs.get("expire") or [""])[0]
        if not raw:
            return 0.0
        value = int(raw, 10)
        # 明显不是时间戳时尝试十六进制
        if value < 1_000_000_000 or value > 4_000_000_000:
            try:
                value = int(raw, 16)
            except ValueError:
                return 0.0
        return float(value)
    except (ValueError, IndexError):
        return 0.0


class BiliAPI:
    def __init__(self, session: aiohttp.ClientSession, cfg: Config):
        self._session = session
        self._cfg = cfg
        self._ua = cfg.bili.user_agent
        self._wbi = WbiSigner(session, cfg.bili.user_agent)
        self.uid: int = 0
        self.uname: str = ""

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    # ------------------------------------------------------------ 基础

    async def _get(self, url: str, params: Optional[Dict[str, Any]] = None,
                   need_wbi: bool = False) -> Dict[str, Any]:
        if need_wbi and params is not None:
            params = await self._wbi.sign(params)
        try:
            async with self._session.get(url, params=params) as res:
                data = await res.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise BiliAPIError(-1, f"网络/解析错误: {exc}", url) from exc

        code = data.get("code", -1)
        if code != 0:
            raise BiliAPIError(code, str(data.get("message", "")), url)
        return data

    async def _post(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with self._session.post(url, data=data) as res:
                payload = await res.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise BiliAPIError(-1, f"网络/解析错误: {exc}", url) from exc
        if payload.get("code") not in (0, None):
            raise BiliAPIError(int(payload["code"]), str(payload.get("message", "")), url)
        return payload

    # ------------------------------------------------------------ 房间

    async def room_init(self, room_id_or_short: int) -> RoomInfo:
        data = await self._get(ROOM_INIT_URL, {"id": room_id_or_short})
        payload = data["data"]
        return RoomInfo(
            room_id=int(payload["room_id"]),
            uid=int(payload.get("uid") or 0),
            live_status=int(payload.get("live_status") or 0),
        )

    async def anchor_info(self, room_id: int) -> tuple[int, str]:
        """取主播的 (uid, 昵称)。get_info 不返回昵称，得单独查这个接口。"""
        try:
            data = await self._get(ANCHOR_INFO_URL, {"roomid": room_id})
        except BiliAPIError as exc:
            logger.debug("get_anchor_in_room 失败 room=%s: %s", room_id, exc)
            return 0, ""
        info = (data.get("data") or {}).get("info") or {}
        return int(info.get("uid") or 0), str(info.get("uname") or "")

    async def room_info(self, room_id: int) -> RoomInfo:
        """房间详情。

        两个接口都失败时抛 BiliAPIError，**不要**返回 live_status=0 的空对象：
        调用方会据此判定「主播已下播」并清空整场上下文，一次 15s 超时或一个
        -352 就能把一场正在进行的直播拆掉，日志还写成「主播已下播」误导排查。
        """
        info = RoomInfo(room_id=room_id)
        loaded = False
        last_exc: Optional[BiliAPIError] = None
        try:
            data = await self._get(ROOM_INFO_URL, {"room_id": room_id})
            try:
                payload = data["data"]
                if not isinstance(payload, dict):
                    raise TypeError(f"data 不是对象: {type(payload).__name__}")
                info.uid = int(payload.get("uid") or 0)
                info.title = payload.get("title", "")
                info.live_status = int(payload.get("live_status") or 0)
                info.online = int(payload.get("online") or 0)
                info.area_name = payload.get("area_name", "")
                info.live_time = str(payload.get("live_time") or "")
                info.uname = str(payload.get("uname") or "")
            except (KeyError, TypeError, ValueError) as exc:
                raise BiliAPIError(-1, f"响应格式异常: {exc}", "get_info") from exc
            loaded = True
        except BiliAPIError as exc:
            last_exc = exc
            logger.debug("get_info 失败，回退 getRoomInfoOld: %s", exc)
            try:
                # 需要用主播 mid 查询
                init = await self.room_init(room_id)
                old = await self._get(ROOM_INFO_OLD_URL, {"mid": init.uid})
                try:
                    payload = old["data"]
                    if not isinstance(payload, dict):
                        raise TypeError(f"data 不是对象: {type(payload).__name__}")
                    info.uid = init.uid
                    info.title = payload.get("title", "")
                    info.live_status = int(payload.get("liveStatus") or 0)
                    info.online = int(payload.get("online") or 0)
                except (KeyError, TypeError, ValueError) as exc2:
                    raise BiliAPIError(
                        -1, f"响应格式异常: {exc2}", "getRoomInfoOld"
                    ) from exc2
                loaded = True
            except BiliAPIError as exc2:
                last_exc = exc2
                logger.debug("房间信息获取失败 room=%s: %s", room_id, exc2)

        if not loaded:
            raise last_exc or BiliAPIError(-1, "房间信息获取失败", "get_info")

        if not info.uname:
            uid, uname = await self.anchor_info(room_id)
            info.uname = uname
            if not info.uid:
                info.uid = uid
        return info

    async def room_status_by_uid(self, uid: int) -> int:
        data = await self._get(ROOM_STATUS_URL, {"uids[]": uid})
        payload = (data.get("data") or {}).get(str(uid)) or {}
        return int(payload.get("live_status") or 0)

    # ------------------------------------------------------------ 播放地址

    async def playurl(self, room_id: int, qn: int = 250) -> List[StreamCandidate]:
        params = {
            "room_id": room_id,
            "protocol": "0,1",
            "format": "0,1,2",
            "codec": "0",
            "qn": qn,
            "platform": "web",
            "ptype": 8,
            "dolby": 5,
            "panorama": 1,
        }
        data = await self._get(PLAYURL_URL, params)
        playurl_info = (data.get("data") or {}).get("playurl_info")
        if not playurl_info:
            raise BiliAPIError(-2, "未开播或没有可用的播放地址", PLAYURL_URL)

        candidates: List[StreamCandidate] = []
        for stream in playurl_info.get("playurl", {}).get("stream", []):
            for fmt in stream.get("format", []):
                for codec in fmt.get("codec", []):
                    for url_info in codec.get("url_info", []):
                        url = f"{url_info['host']}{codec['base_url']}{url_info.get('extra', '')}"
                        candidates.append(
                            StreamCandidate(
                                protocol=stream.get("protocol_name", ""),
                                format=fmt.get("format_name", ""),
                                codec=codec.get("codec_name", ""),
                                url=url,
                                qn=int(codec.get("current_qn") or 0),
                                expires_at=_parse_expires(url),
                            )
                        )
        return candidates

    # ------------------------------------------------------------ 弹幕

    async def danmaku_history(self, room_id: int) -> List[HistoryDanmaku]:
        """最近约 10 条弹幕。匿名时常为空，登录后成功率更高。"""
        try:
            data = await self._get(
                DANMAKU_HISTORY_URL, {"roomid": room_id, "room_type": 0}
            )
        except BiliAPIError as exc:
            logger.info("弹幕历史不可用 room=%s: %s", room_id, exc)
            return []

        out: List[HistoryDanmaku] = []
        for item in (data.get("data") or {}).get("room") or []:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            try:
                ts = float(item.get("timeline") or time.time())
            except (TypeError, ValueError):
                ts = time.time()
            out.append(
                HistoryDanmaku(
                    uid=int(item.get("uid") or 0),
                    uname=item.get("nickname", ""),
                    text=text,
                    ts=ts,
                )
            )
        return out

    async def danmaku_server(self, room_id: int) -> Dict[str, Any]:
        """WBI 签名的弹幕服务器配置，返回 {host_list, token}。"""
        data = await self._get(
            DANMAKU_INFO_URL, {"id": room_id, "type": 0}, need_wbi=True
        )
        return data.get("data") or {}

    # ------------------------------------------------------------ 发送

    @staticmethod
    def _csrf(session: aiohttp.ClientSession) -> str:
        morsel = session.cookie_jar.filter_cookies(
            yarl.URL("https://live.bilibili.com/")
        ).get("bili_jct")
        return morsel.value if morsel else ""

    async def send_danmaku(
        self, room_id: int, message: str, reply_mid: Optional[int] = None
    ) -> SendResult:
        csrf = self._csrf(self._session)
        if not csrf:
            return SendResult(ok=False, code=-101, message="缺少 bili_jct，未登录")

        payload: Dict[str, Any] = {
            "bubble": 0,
            "msg": message,
            "color": 16777215,
            "mode": 1,
            "fontsize": 25,
            "rnd": int(time.time()),
            "roomid": room_id,
            "csrf": csrf,
            "csrf_token": csrf,
        }
        if reply_mid:
            payload.update(
                {
                    "room_type": 0,
                    "jumpfrom": 0,
                    "reply_mid": reply_mid,
                    "reply_attr": 0,
                    "statistics": '{"appId":100,"platform":5}',
                }
            )
        try:
            data = await self._post(SEND_MSG_URL, payload)
        except BiliAPIError as exc:
            return SendResult(ok=False, code=exc.code, message=exc.message)

        msg = str(data.get("message") or "")
        if msg == "k":
            return SendResult(False, -3, "主播屏蔽词", forbidden=True, raw=data)
        if msg == "f":
            return SendResult(False, -4, "B站屏蔽词", forbidden=True, raw=data)
        if msg:
            return SendResult(False, -5, msg, raw=data)
        return SendResult(True, 0, "", raw=data)
