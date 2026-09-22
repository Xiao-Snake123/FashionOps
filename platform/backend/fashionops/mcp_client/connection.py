"""把外部 MCP 服务器当 stdio 子进程拉起，充当它的 MCP 客户端。

## 为什么用 stdio 而不是 HTTP

stdio 是 MCP 客户端拉起服务器最稳的方式：不需要服务器先起好、不需要端口、
天然进程隔离（对方崩了不影响我们）。代价是每接入一个服务器就多一个常驻
子进程 —— 对「几个办公服务器」这个量级完全够用。

## 同步世界怎么调异步的 MCP

网关的 `ToolGateway.call` 是**同步**的，但 MCP 客户端是 **async** 的。
做法：每个连接独占一个后台线程跑一个 event loop，把 MCP 会话常驻在里面；
同步的 `call_tool` 通过 `run_coroutine_threadsafe` 把协程丢给那个 loop，
再用 `future.result(timeout)` 阻塞等地结果。这是「同步代码调异步服务」的标准
模式，关键是 **loop 和会话都常驻**，而不是每次调用现开一个（那样每次都起子进程，
慢且状态全丢）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("fashionops.mcp_client")


class McpConnectionError(RuntimeError):
    """连不上 / 初始化失败 / 连接已关闭。"""


class McpToolError(RuntimeError):
    """工具调用在协议层失败（``is_error=True``）。

    注意区分：办公服务器自己返回 ``{"ok": false, ...}`` 是**业务失败**，
    走正常返回（数据里带着原因和 hint），网关当成功处理；
    只有 ``is_error=True``（传输 / 协议级错误）才抛这个，让网关按上游错误重试。
    """

    def __init__(self, message: str, *, code: str = "tool_error", data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _coerce_env(env: dict[str, str] | None) -> dict[str, str] | None:
    if not env:
        return None
    return {str(k): str(v) for k, v in env.items()}


class McpStdioConnection:
    """一条到某个 MCP 服务器的 stdio 连接（常驻子进程 + 常驻会话）。"""

    def __init__(
        self,
        *,
        name: str,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        startup_timeout: float = 15.0,
        call_timeout: float = 30.0,
    ) -> None:
        self.name = name
        self._params = StdioServerParameters(
            command=command,
            args=list(args or []),
            cwd=cwd,
            env=_coerce_env(env),
        )
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._shutdown: asyncio.Event | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None

    # ---- 生命周期 ----
    def connect(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=f"mcp-{self.name}", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._startup_timeout):
            self.close()
            raise McpConnectionError(
                f"连接 {self.name} 超时（{self._startup_timeout}s 未初始化完成）"
            )
        if self._startup_error is not None:
            err = self._startup_error
            self.close()
            raise McpConnectionError(f"连接 {self.name} 初始化失败：{err}")

    def _run(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._lifecycle())
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP 连接 %s 的 loop 异常退出：%s", self.name, exc)
        finally:
            self._loop.close()

    async def _lifecycle(self) -> None:
        self._shutdown = asyncio.Event()
        try:
            async with stdio_client(self._params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._shutdown.wait()
        except Exception as exc:  # noqa: BLE001
            if not self._ready.is_set():
                self._startup_error = exc
            else:
                logger.warning("MCP 连接 %s 运行中断：%s", self.name, exc)
        finally:
            self._session = None

    def close(self) -> None:
        if self._shutdown is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.call_soon_threadsafe(self._shutdown.set)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._loop = None

    def __enter__(self) -> McpStdioConnection:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 同步调用 API ----
    def _submit(self, coro: Any):
        if self._loop is None or self._session is None:
            raise McpConnectionError(f"连接 {self.name} 尚未建立或已关闭")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(self._call_timeout)
        except TimeoutError as exc:
            raise McpConnectionError(
                f"调用 {self.name} 超时（>{self._call_timeout}s）"
            ) from exc

    def list_tools(self) -> list[Any]:
        result = self._submit(self._session.list_tools())
        return list(result.tools)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self._submit(self._session.call_tool(name, arguments or {}))
        if getattr(result, "is_error", False):
            raise McpToolError(_extract_text(result), code="tool_error")
        return _parse_content(result)


def _extract_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


def _parse_content(result: Any) -> Any:
    text = _extract_text(result)
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
