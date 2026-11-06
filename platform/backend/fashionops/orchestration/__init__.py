"""DeerFlow（LangGraph）编排适配层。

平台自研的对话编排（chat/agent.py 的 run_tool_loop / _drive）保留为 ``legacy`` 后端；
这里把对话侧接到 DeerFlow 的 LangGraph 图（作为独立 sidecar 跑在 :8011）。

数据流：

    平台前端  ──SSE──>  平台后端(chat/agent)  ──队列──>  orchestration.run_turn
                                                              │
                                                              ▼
                                                    DeerFlowClient ──SSE──> DeerFlow :8011
                                                                              │ (经 MCP/SSE 调平台工具)
                                                                              ▼
                                                              平台 ToolGateway(限流/熔断/审计/审批)
    平台后端把 DeerFlow 的事件翻译成自己的 SSE 帧回给前端，并落到对话库。

开关见 config.py 的 ``orchestrator_backend``（legacy | deerflow）。
"""

from fashionops.orchestration.client import DeerFlowClient
from fashionops.orchestration.orchestrator import (
    SENTINEL,
    run_resume,
    run_turn,
)

__all__ = [
    "DeerFlowClient",
    "SENTINEL",
    "run_resume",
    "run_turn",
]
