"""DeerFlow sidecar 的 LangGraph 兼容 API 客户端（异步，基于 httpx）。

只封装平台需要的三件事：

* ``create_thread``        —— 开一个对话线程（DeerFlow 在自己的 checkpointer 里维护历史）
* ``stream_run``           —— 在某个线程上起一次 run，边跑边按 SSE 文本行吐回来
* 鉴权                     —— sidecar 之间 server-to-server 用 DeerFlow 的 Internal Auth 头，
                              绕开 PAT allowlist 与 CSRF（对应 DeerFlow 的
                              ``DEER_FLOW_INTERNAL_AUTH_TOKEN``）
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

logger = logging.getLogger("fashionops.orchestration.client")


class DeerFlowClient:
    """对 DeerFlow Gateway（``/api/langgraph``）的薄封装。"""

    def __init__(
        self,
        base_url: str,
        internal_token: str | None,
        owner_user_id: str,
    ) -> None:
        self.base = base_url.rstrip("/")
        self._headers = {
            # DeerFlow 内部鉴权：sidecar 互信，不走用户 PAT。
            "X-DeerFlow-Internal-Token": internal_token or "",
            "X-DeerFlow-Owner-User-Id": owner_user_id,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }

    async def create_thread(self) -> str:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{self.base}/threads",
                headers=self._headers,
                json={"metadata": {"source": "fashionops"}},
            )
            r.raise_for_status()
            return r.json()["thread_id"]

    async def stream_run(
        self,
        thread_id: str,
        messages: list[dict[str, Any]],
        *,
        model_name: str | None = None,
        is_plan_mode: bool = False,
    ) -> AsyncIterator[str]:
        """在 ``thread_id`` 上起一次 run，逐行产出原始 SSE 文本。

        我们订阅 ``messages``（打字机 token）、``values``（每步后的完整快照，
        用来稳定地提取工具调用/结果）和 ``custom``（子智能体等）三种流。
        """
        body = {
            "input": {"messages": messages},
            "config": {
                "configurable": {
                    "model_name": model_name,
                    "is_plan_mode": is_plan_mode,
                }
            },
            # 想要哪几种流就列哪几种；messages 给 token，values 给工具快照。
            "stream_mode": ["messages", "values", "custom"],
        }
        async with httpx.AsyncClient(timeout=None) as c, c.stream(
            "POST",
            f"{self.base}/threads/{thread_id}/runs/stream",
            headers=self._headers,
            json=body,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                yield line
