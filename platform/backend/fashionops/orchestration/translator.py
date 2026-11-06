"""把 DeerFlow 的原始 SSE 文本解析成结构化事件。

DeerFlow v2 的 SSE 用 LangGraph 的 stream modes：``messages`` / ``values`` /
``custom`` / ``metadata`` / ``end`` / ``gap`` 等。这里只做"解析"，不做持久化——
持久化在 orchestrator 里按平台自己的 SSE 帧格式做。

## 为什么解析器必须是**有状态**的

一条 SSE 事件跨三行：

    event: messages
    data: [{...}]
    <空行>            ← 空行才是"这条事件结束了"的标志

所以逐行解析必须把 ``event`` 攒到空行为止。**这个坑很隐蔽**：如果每读一行
就新建一个解析器，那么 ``event: messages`` 这一行会在"读到行尾、事件还没结束"
时被吐出来（事件名对、data 为空），而下一行 ``data: ...`` 又变成了一个
默认名为 ``message`` 的新事件。结果是上层永远匹配不到 ``messages`` /
``values`` / ``end``，一行有效内容都处理不到，**而且不报任何错** ——
表现就是"连上了，但什么都没发生"。

所以这里用 ``_SSEDecoder`` 显式持有状态，同步/异步两种喂法共用同一份实现。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Iterator


class _SSEDecoder:
    """SSE 文本行 → ``(事件名, data)`` 的状态机。

    用法：每读一行调一次 :meth:`feed`，读完调一次 :meth:`flush`。
    """
    __slots__ = ("_event", "_data")

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, line: str) -> tuple[str, str] | None:
        """喂一行。攒够一条事件就返回它，否则返回 ``None``。"""
        line = line.strip()

        if not line:
            # 空行 = 一条事件的结束（SSE 规范）。
            if self._event is None and not self._data:
                return None  # 连续空行，忽略
            return self._take()

        if line.startswith(":"):
            # 注释行。SSE 用它当心跳/保活（DeerFlow 的心跳是 ": ping" 之类），
            # 不是数据，直接丢。
            return None

        if line.startswith("event:"):
            self._event = line[6:].strip()
        elif line.startswith("data:"):
            # 一条事件的 data 可以有多行，按规范用 \n 拼回来。
            self._data.append(line[5:].strip())
        # 其余字段（id: / retry:）DeerFlow 不用，忽略。
        return None

    def flush(self) -> tuple[str, str] | None:
        """流结束时把最后一条没收尾的事件吐出来（对端没补空行的情况）。"""
        if self._event is None and not self._data:
            return None
        return self._take()

    def _take(self) -> tuple[str, str] | None:
        if self._event is None and not self._data:
            return None
        out = (self._event or "message", "\n".join(self._data))
        self._event, self._data = None, []
        return out


def split_sse(raw_lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """同步版：把 SSE 文本行切成 ``(事件名, data)``。

    注意入参是**整个**行序列，不是单行 —— 传 ``[line]`` 逐行调用会丢掉事件名，
    理由见模块顶部。
    """
    decoder = _SSEDecoder()
    for line in raw_lines:
        event = decoder.feed(line)
        if event is not None:
            yield event
    tail = decoder.flush()
    if tail is not None:
        yield tail


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """异步版：直接吃 ``httpx`` 的 ``aiter_lines()``，边收边切。

    和 :func:`split_sse` 共用同一个状态机 —— 两份实现迟早会分叉，
    而分叉的后果是"某一路解析出来的事件名不对"，非常难查。
    """
    decoder = _SSEDecoder()
    async for line in lines:
        event = decoder.feed(line)
        if event is not None:
            yield event
    tail = decoder.flush()
    if tail is not None:
        yield tail


def parse_messages(data: str) -> dict | None:
    """``messages`` 事件 data 是 ``[message, metadata]``，返回 message 字典。"""
    try:
        arr = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(arr, list) and arr:
        return arr[0] if isinstance(arr[0], dict) else None
    return arr if isinstance(arr, dict) else None


def parse_values(data: str) -> list[dict]:
    """``values`` 事件 data 是一个快照 dict，里面 ``messages`` 是当前完整消息列表。"""
    try:
        snap = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(snap, dict):
        msgs = snap.get("messages")
        if isinstance(msgs, list):
            return [m for m in msgs if isinstance(m, dict)]
    return []


def safe_json(text: str | None, default: dict) -> dict:
    if not isinstance(text, str):
        return default
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else default
    except (json.JSONDecodeError, ValueError):
        return default


def message_role(m: dict) -> str:
    """归一化 LangChain 消息的 role/type 到 human/user/assistant/tool。"""
    if "role" in m:
        return m["role"]
    t = m.get("type", "")
    return {"human": "user", "ai": "assistant", "tool": "tool"}.get(t, t)
