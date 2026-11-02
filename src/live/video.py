# -*- coding: utf-8 -*-
"""画面抓帧：周期性从直播流取一帧 JPEG，作为多模态输入。

抓帧和音频管道分离：音频进程只需 `-vn`，抓帧按需拉起、用完即退，
即使抓帧失败也不影响「听」。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Awaitable, Callable, Optional

from ..config import Config
from ..util.log import get_logger

logger = get_logger("live.video")


@dataclass
class Frame:
    jpeg: bytes
    ts: float
    width: int = 0
    height: int = 0

    def is_fresh(self, ttl: float) -> bool:
        return (time.time() - self.ts) <= ttl


def _downscale(jpeg: bytes, max_side: int) -> tuple[bytes, int, int]:
    """按最长边缩放并重新编码；Pillow 不可用时原样返回。"""
    try:
        from PIL import Image
    except ImportError:
        return jpeg, 0, 0

    try:
        img = Image.open(BytesIO(jpeg))
        width, height = img.size
    except Exception:  # noqa: BLE001
        return jpeg, 0, 0

    if max(width, height) > max_side:
        ratio = max_side / float(max(width, height))
        img = img.convert("RGB").resize((max(1, int(width * ratio)), max(1, int(height * ratio))))
        width, height = img.size
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue(), width, height
    return jpeg, width, height


class FrameGrabber:
    def __init__(
        self,
        cfg: Config,
        room_id: int,
        url_provider: Callable[[], Awaitable[str]],
    ):
        self._cfg = cfg
        self._room_id = room_id
        self._url_provider = url_provider
        self.latest: Optional[Frame] = None
        self.failures = 0
        self.successes = 0

    async def grab(self, timeout: float = 25.0) -> Optional[Frame]:
        try:
            url = await self._url_provider()
        except Exception as exc:  # noqa: BLE001
            logger.warning("room=%s 抓帧取流失败: %s", self._room_id, exc)
            return None

        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-user_agent", self._cfg.bili.user_agent,
            "-headers", "Referer: https://live.bilibili.com/\r\n",
            "-i", url,
            "-frames:v", "1",
            "-f", "mjpeg",
            "-q:v", "5",
            "pipe:1",
        ]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("room=%s 抓帧超时", self._room_id)
            if proc is not None and proc.returncode is None:
                proc.kill()
            self.failures += 1
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("room=%s 抓帧异常: %s", self._room_id, exc)
            self.failures += 1
            return None

        if not stdout:
            self.failures += 1
            logger.debug("room=%s 抓帧无输出: %s", self._room_id,
                         stderr.decode("utf-8", "replace")[-200:])
            return None

        jpeg, width, height = await asyncio.get_running_loop().run_in_executor(
            None, _downscale, stdout, self._cfg.llm.image_max_side
        )
        frame = Frame(jpeg=jpeg, ts=time.time(), width=width, height=height)
        self.latest = frame
        self.successes += 1
        logger.debug("room=%s 抓帧成功 %sx%s %d bytes", self._room_id, width, height, len(jpeg))
        return frame

    def latest_fresh(self) -> Optional[Frame]:
        if self.latest and self.latest.is_fresh(self._cfg.stream.frame_ttl_sec):
            return self.latest
        return None
