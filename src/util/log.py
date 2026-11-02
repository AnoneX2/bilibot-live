# -*- coding: utf-8 -*-
"""日志：控制台 + 轮转文件，UTF-8。

除了 logging 输出的记录，`print()` / 标准错误里直接写的内容（sherpa-onnx、ffmpeg、
未捕获异常的 traceback 等）也会被捕获进同一个日志文件，这样：
* 终端里能看到什么，日志文件里就有同样内容（面板的「实时日志」不会漏）；
* 面板里查看日志与在终端直接跑 BOT 的观感一致。
"""

from __future__ import annotations

import io
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_CONFIGURED = False


class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[37m",
        "INFO": "\033[36m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[41m",
    }
    RESET = "\033[0m"

    def __init__(self, fmt: str, use_color: bool):
        super().__init__(fmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{text}{self.RESET}" if color else text


class _StreamCapture(io.TextIOBase):
    """把 print()/stderr 的内容同时抄一份到日志文件。

    原始 stream 照旧输出（终端里不变），另外按行转发给 `_sink`（一个只挂文件
    handler 的 logger）。控制台的 StreamHandler 绑定的是「原始 stream」，
    所以不会出现「日志→控制台→再写日志」的递归。
    """

    def __init__(self, stream, sink: logging.Logger, level: int = logging.INFO):
        self._stream = stream
        self._sink = sink
        self._level = level
        self._pending = ""
        self._busy = False

    def write(self, data):  # noqa: D102
        text = data if isinstance(data, str) else str(data)
        try:
            self._stream.write(text)
        except Exception:  # noqa: BLE001 - 终端可能已经关了
            pass
        if self._busy:
            return len(text)
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._emit(line)
        return len(text)

    def _emit(self, line: str) -> None:
        line = line.rstrip("\r")
        if not line.strip():
            return
        self._busy = True
        try:
            self._sink.log(self._level, "%s", line)
        except Exception:  # noqa: BLE001 - 日志写失败不能影响业务
            pass
        finally:
            self._busy = False

    def flush(self) -> None:  # noqa: D102
        if self._pending:
            line, self._pending = self._pending, ""
            self._emit(line)
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass

    # -- 其它 TextIOBase 接口 --

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._stream, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str:  # type: ignore[override]
        return getattr(self._stream, "errors", "replace") or "replace"

    def fileno(self):  # noqa: D102
        raise io.UnsupportedOperation("fileno")


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    filename: str = "bot.log",
    capture_stdout: bool = True,
) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return

    # 先记住真实 stdout/stderr：控制台 handler 绑在它们上面，
    # 否则下面的替换会形成「日志 → 控制台 → 再写日志」的递归。
    real_stdout, real_stderr = sys.stdout, sys.stderr

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
    datefmt = "%H:%M:%S"

    console = logging.StreamHandler(stream=real_stdout)
    console.setFormatter(
        _ColorFormatter(fmt, use_color=sys.platform == "win32" and real_stdout.isatty())
    )
    root.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / filename, maxBytes=8 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(fh)

        if capture_stdout:
            sink = logging.getLogger("console")
            sink.handlers = [fh]
            sink.propagate = False
            sink.setLevel(logging.INFO)
            sys.stdout = _StreamCapture(real_stdout, sink)
            sys.stderr = _StreamCapture(real_stderr, sink)

    # 第三方库降噪
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("blivedm").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
