"""把一回合对话交给 DeerFlow 的 LangGraph 图跑，并把它的事件翻成平台自己的帧。

设计要点：

* **每个平台会话 = 一个 DeerFlow 线程**（进程内映射，见 ``_THREADS``；生产应落库）。
  DeerFlow 在自己的 checkpointer 里维护历史，平台只在每回合把"新用户消息"喂进去。
* **平台仍是消息的唯一真源**：DeerFlow 产出的 token / 工具调用 / 工具结果，在流里
  实时翻译成 ``token`` / ``tool_call`` / ``tool_result`` / ``approval_required`` /
  ``done`` 帧推给前端，同时逐条落对话库（与 legacy 后端落库逻辑一致）。
* **审批闸口保留在平台侧**：平台 MCP 工具在被 DeerFlow 调用、且需要人工确认时返回
  ``needs_approval`` 而不执行；这里识别到后落一张审批卡并暂停这一轮，等用户在平台
  审批卡片上确认/驳回后，由 ``run_resume`` 把结果喂回同一 DeerFlow 线程继续。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fashionops.chat import store
from fashionops.config import get_settings
from fashionops.db.models_chat import MSG_ROLE_ASSISTANT, MSG_ROLE_TOOL
from fashionops.gateway.registry import from_llm_name
from fashionops.orchestration.client import DeerFlowClient
from fashionops.orchestration.translator import (
    iter_sse,
    message_role,
    parse_messages,
    parse_values,
    safe_json,
)

logger = logging.getLogger("fashionops.orchestration")

# 流结束哨兵：worker 跑完后放进队列，消费者据此结束 SSE。
SENTINEL = object()

# 阶段一占位：DeerFlow 线程 id 仅存进程内存。生产应在 conversations 表加一列持久化。
_THREADS: dict[str, str] = {}
# (conversation_id, tool_call_id) -> (工具名, 参数)，用于把工具结果与调用对上。
_PENDING: dict[tuple[int, str | None], tuple[str, dict]] = {}


def _client() -> DeerFlowClient:
    s = get_settings()
    return DeerFlowClient(s.deerflow_base_url, s.deerflow_internal_token, s.deerflow_owner_user_id)


async def _get_thread(conversation_no: str) -> str:
    tid = _THREADS.get(conversation_no)
    if tid:
        return tid
    tid = await _client().create_thread()
    _THREADS[conversation_no] = tid
    return tid


# --------------------------------------------------------------------------- #
# 对外入口（worker 目标，同步签名，内部 asyncio.run）
# --------------------------------------------------------------------------- #
def run_turn(*, conversation: dict, setup: Any, user_text: str, events: Any) -> None:
    """对话侧一回合的 DeerFlow 编排入口。``events`` 是 ``queue.Queue``。"""
    try:
        asyncio.run(_arun_turn(conversation, setup, user_text, events))
    except Exception as exc:  # noqa: BLE001
        logger.exception("DeerFlow run_turn 失败")
        events.put({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        events.put(SENTINEL)


def run_resume(
    *,
    conversation: dict,
    setup: Any,
    approval_no: str,
    approved: bool,
    principal: Any,
    payload: dict,
    tool_message: dict,
    events: Any,
) -> None:
    """审批结论回来后，把结果喂回同一 DeerFlow 线程继续这一轮。"""
    try:
        asyncio.run(
            _arun_resume(conversation, setup, approval_no, approved, principal, payload, tool_message, events)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("DeerFlow run_resume 失败")
        events.put({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        events.put(SENTINEL)


# --------------------------------------------------------------------------- #
# 核心异步驱动
# --------------------------------------------------------------------------- #
async def _stream_and_translate(
    thread_id: str,
    messages: list[dict[str, Any]],
    conversation_id: int,
    setup: Any,
    events: Any,
) -> str:
    """流式跑一次 DeerFlow run，返回结束状态（``ok`` / ``waiting_approval`` / ``error``）。"""
    settings = get_settings()
    text_buf: list[str] = []
    seen_len = 0
    try:
        # 注意这里是 iter_sse(...) 直接吃整个行流，**不能**写成逐行
        # `split_sse([line])`：一条事件跨三行，逐行调用会把事件名和 data
        # 拆到两次调用里，事件名全丢、并且一声不响（理由详见 translator.py）。
        async for ev_name, ev_data in iter_sse(
            _client().stream_run(thread_id, messages, model_name=settings.model_name)
        ):
            if ev_name == "end":
                await _finalize(conversation_id, text_buf, events, "ok")
                return "ok"
            if ev_name == "messages":
                _handle_messages(ev_data, text_buf, events)
            elif ev_name == "values":
                new_msgs = parse_values(ev_data)[seen_len:]
                seen_len += len(new_msgs)
                for m in new_msgs:
                    if _handle_value_message(m, conversation_id, setup, events, text_buf):
                        await _finalize(conversation_id, text_buf, events, "waiting_approval")
                        return "waiting_approval"
    except Exception as exc:  # noqa: BLE001
        logger.exception("DeerFlow 流处理异常")
        events.put({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
        await _finalize(conversation_id, text_buf, events, "error")
        return "error"
    await _finalize(conversation_id, text_buf, events, "ok")
    return "ok"


async def _arun_turn(conversation: dict, setup: Any, user_text: str, events: Any) -> None:
    conversation_id = int(conversation["id"])
    thread_id = await _get_thread(conversation["no"])
    await _stream_and_translate(
        thread_id,
        [{"role": "user", "content": user_text}],
        conversation_id,
        setup,
        events,
    )


async def _arun_resume(
    conversation: dict,
    setup: Any,
    approval_no: str,  # noqa: ARG001
    approved: bool,
    principal: Any,  # noqa: ARG001
    payload: dict,
    tool_message: dict,
    events: Any,
) -> None:
    conversation_id = int(conversation["id"])
    thread_id = await _get_thread(conversation["no"])
    verdict = "批准" if approved else "驳回"
    cont = (
        f"用户已{verdict}了工具 `{tool_message.get('tool_name')}` 的调用。"
        f"执行结果（JSON）：{json.dumps(payload, ensure_ascii=False)}。"
        f"请基于该结果继续回答用户，必要时继续调用其他工具。"
    )
    await _stream_and_translate(
        thread_id,
        [{"role": "user", "content": cont}],
        conversation_id,
        setup,
        events,
    )


# --------------------------------------------------------------------------- #
# 事件 → 平台帧 + 落库
# --------------------------------------------------------------------------- #
def _handle_messages(ev_data: str, text_buf: list[str], events: Any) -> None:
    """``messages`` 流只负责打字机 token（增量 content）。"""
    msg = parse_messages(ev_data)
    if not msg:
        return
    if message_role(msg) != "assistant":
        return
    content = msg.get("content")
    if isinstance(content, str) and content:
        text_buf.append(content)
        events.put({"type": "token", "text": content})


def _handle_value_message(
    m: dict,
    conversation_id: int,
    setup: Any,  # noqa: ARG001
    events: Any,
    text_buf: list[str],
) -> bool:
    """处理 ``values`` 快照里新增的一条消息；返回 True 表示触发了审批、应停下。"""
    role = message_role(m)
    if role == "user":
        return False

    if role == "assistant":
        tcs = m.get("tool_calls") or []
        if tcs:
            for tc in tcs:
                name = _tc_name(tc)
                args = _tc_args(tc)
                assistant = store.append_message(
                    conversation_id,
                    role=MSG_ROLE_ASSISTANT,
                    content="",
                    tool_call_id=tc.get("id"),
                    tool_name=name,
                    tool_arguments=args,
                    finish_reason="tool_calls",
                )
                events.put({"type": "message", "message": assistant})
                events.put({"type": "tool_call", "name": name, "arguments": args})
                _PENDING[(conversation_id, tc.get("id"))] = (name, args)
            return False
        # 纯文本终稿（无工具调用）
        content = m.get("content")
        if isinstance(content, str) and content:
            text_buf.append(content)
        return False

    if role == "tool":
        tool_call_id = m.get("tool_call_id")
        payload = safe_json(m.get("content"), {"ok": True, "raw": m.get("content")})
        name, args = _PENDING.pop((conversation_id, tool_call_id), (None, None))
        name = name or "unknown"
        tool_message = store.append_message(
            conversation_id,
            role=MSG_ROLE_TOOL,
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=tool_call_id,
            tool_name=name,
            tool_result=payload,
            tool_ok=bool(payload.get("ok")),
        )
        events.put({"type": "message", "message": tool_message})
        events.put(
            {"type": "tool_result", "name": name, "ok": bool(payload.get("ok")), "message": tool_message}
        )
        if payload.get("needs_approval"):
            approval = store.create_approval(
                conversation_id=conversation_id,
                message_id=tool_message["id"],
                tool_call_id=tool_call_id or "",
                tool_name=name,
                arguments=args,
                preview=str(payload.get("preview") or ""),
                reason="写操作需要人工确认（来自 DeerFlow 编排）",
            )
            events.put({"type": "approval_required", "approval": approval})
            return True
        return False

    return False


def _finalize(conversation_id: int, text_buf: list[str], events: Any, status: str) -> None:
    """落最终助手消息（若还有待写回的文本），并收尾。"""
    settings = get_settings()
    final_text = "".join(text_buf)
    if final_text.strip():
        assistant = store.append_message(
            conversation_id, role=MSG_ROLE_ASSISTANT, content=final_text, finish_reason="stop"
        )
        events.put({"type": "message", "message": assistant})
    try:
        store.bump_llm(conversation_id, calls=1, model=settings.model_name)
    except Exception:  # noqa: BLE001
        logger.warning("bump_llm 失败", exc_info=True)
    events.put({"type": "done", "status": status})


# --------------------------------------------------------------------------- #
# 工具调用字段归一化（DeerFlow 走 LangChain，tool_calls 字段形态可能不同）
# --------------------------------------------------------------------------- #
def _tc_name(tc: dict) -> str:
    if tc.get("name"):
        return from_llm_name(tc["name"]) or tc["name"]
    fn = tc.get("function") or {}
    return from_llm_name(fn.get("name", "")) or fn.get("name", "")


def _tc_args(tc: dict) -> dict:
    if isinstance(tc.get("args"), dict):
        return tc["args"]
    fn = tc.get("function") or {}
    a = fn.get("arguments")
    if isinstance(a, str):
        return safe_json(a, {})
    return a if isinstance(a, dict) else {}
