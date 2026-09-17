"""对话 Agent —— 用自然语言直接操作这个运营中台。

## 它在整个系统里的位置

原来的交互是"填个表单 → 提交任务 → 等 DAG 跑完"。这适合**已知要干什么**
的场景，但用户真实的第一句话往往是模糊的："最近退货是不是变多了？"
—— 这既不是一个任务，也不该逼用户先想清楚要哪几步。

所以对话是**入口**，不是替代：

    用户说话
      ├─ 简单问题 → 模型自己调查询工具 → 直接流式回答
      └─ 多步需求 → 调 task.submit → 落成真正的任务 → 走原来的 DAG
                     （那一套的 DAG 可视化、断点续跑、产物、审批留痕全都保留）

**刻意不把任务退化成"一堆对话消息"**：任务有 DAG、有断点、有产物、
有审批人留痕，这些是消息表达不了的。硬塞进去只会得到"看起来统一、
实际两边都表达不清"的数据结构。

## 一条不能破的规矩：数字必须来自工具

模型的强项是"理解和表达"，弱项是"记住准确的数字"。
一旦允许它凭印象说"退货率大概 15% 吧"，整个系统的可信度就没了 ——
而运营中台的第一价值就是**数字可信**。

所以系统提示里把这条写成铁律，而且工具查不到时要明说查不到，
不允许用"一般来说……"糊过去。

## 关于事件流

这个模块不直接写 SSE 帧，只产出 ``dict``。理由：SSE 的帧格式是
HTTP 层的事，而这一层可能被别的入口用（CLI、未来接别的 IM 通道）。
``api/routers/chat.py`` 负责把 dict 变成 ``data: {...}``。

### 事件类型

| type | 含义 |
| --- | --- |
| ``message`` | 一条已落库的消息（用户消息、助手消息、工具消息都走这个） |
| ``token`` | 助手正在生成的文本增量（前端做打字机效果） |
| ``tool_call`` | 模型决定调用某个工具（前端显示"正在查询库存…"） |
| ``tool_result`` | 工具执行完成 |
| ``task_created`` | 对话里启动了一个真实任务 |
| ``approval_required`` | 写操作被闸口拦住，等用户点确认 |
| ``done`` | 本回合结束 |
| ``ping`` | 心跳（长回合里防中间代理掐连接） |
| ``error`` | 出错了 |
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Iterator
from typing import Any

from fashionops.approval.policy import describe_preview, needs_approval
from fashionops.chat import store
from fashionops.chat.tools import TASK_SUBMIT, build_catalog
from fashionops.db import session_scope
from fashionops.db.models_chat import MSG_ROLE_ASSISTANT, MSG_ROLE_TOOL, MSG_ROLE_USER
from fashionops.gateway.core import CallContext, ToolGateway
from fashionops.gateway.registry import from_llm_name, registry
from fashionops.llm import ToolLoopPaused, get_model_client, run_tool_loop
from fashionops.security.service import Principal, effective_scopes

logger = logging.getLogger("fashionops.chat")

#: 对话 Agent 的权限集。它会作为"Agent 侧"参与权限求交 ——
#: 用户在别的 Agent 上有、但这里没声明的权限，对话里用不到（默认从严）。
CHAT_AGENT_NAME = "chat"

#: 单回合最多几轮工具调用。
#: 比 Agent 图里的 8 步小：对话是**交互式**的，用户就坐在屏幕前等着，
#: 让它自动跑 8 轮（每轮几秒）用户会以为卡死了。
MAX_TOOL_STEPS = 5

SYSTEM_PROMPT = """你是「服装电商多 Agent 运营中台」的运营助手。你服务的是服装电商的运营人员。

## 铁律：数字只能来自工具

**所有具体数字、库存、销售额、退货率、活动数据，都必须通过调用工具获得。**
绝对不要凭经验或印象编造数字。

* 工具返回了数据 → 引用具体数值，并说明来源（比如"ERP 里查到…"）
* 工具没查到 / 报错 → **如实说查不到**，并说明下一步可以怎么做
* 用户问的指标工具里没有 → 直接说"这个指标目前取不到"，不要用"一般来说"糊过去

**这条规矩存在的理由**：运营中台的第一价值是数字可信。
一次编造就会让人不再相信屏幕上的所有数字 —— 那时候工具做得再好也没用。

## 什么时候自己回答，什么时候建任务

* **一问一答**（查库存、查商品、查知识库、解释规则）→ 你自己调工具回答
* **需要多步完成的运营动作**（选品 + 出报表 + 发信这一类）→ 调 `task__submit`
  建一个任务，然后把任务号告诉用户，说明可以去「任务」页看实时进度

判断标准是：**如果这件事需要"先做 A 再拿 A 的结果做 B"，就建任务。**
不要试图在对话里一步步替用户跑完 —— 那样既没有进度可见性，
也没有断点续跑和审批留痕。

## 写操作会弹人工确认

发邮件这类改变外部世界的动作，系统会**拦住并要求用户点确认**。
所以：

* 不要说"我已经发出去了" —— 你还没发，在等人确认
* 要说"我准备发一封邮件给 XX，内容是这样，请确认"

这是设计如此，不是出错。**不要在回答里抱怨或解释这个机制**，自然地说
"请确认一下"就够了。

## 语气和格式

* 中文，简洁。运营人员在看数据，不是读文章
* 用短句、要点、必要时用表格
* **不要**写"好的，我将为您……"这种开场白，直接给结论
* 不要重复用户的话
"""


# ===========================================================================
# 一回合的上下文
# ===========================================================================
class TurnSetup:
    """一次对话回合需要的所有东西，全部在开跑之前算好。"""

    def __init__(self, *, conversation_no: str, principal: Principal) -> None:
        from fashionops.tools import AGENT_SCOPES

        self.conversation_no = conversation_no
        self.principal = principal
        #: 用户权限 ∩ 对话 Agent 权限集。
        #: **这是唯一真实的越权防线** —— 用户自己没有的权限，对话里的 Agent 也不能有。
        self.granted_scopes: frozenset[str] = effective_scopes(
            principal, AGENT_SCOPES[CHAT_AGENT_NAME]
        )
        self.permissions: frozenset[str] = frozenset(principal.permissions)


def _load_conversation(conversation_no: str) -> dict[str, Any]:
    with session_scope() as session:
        row = store.get_conversation(session, conversation_no)
        if row is None:
            raise LookupError(f"会话 {conversation_no} 不存在")
        return {
            "id": row.id,
            "no": row.conversation_no,
            "title": row.title,
            "user_id": row.user_id,
            "username": row.username,
            "status": row.status,
        }


# ===========================================================================
# 主入口
# ===========================================================================
def stream_turn(
    *, conversation_no: str, principal: Principal, user_text: str
) -> Iterator[dict[str, Any]]:
    """用户发一句话，跑一个回合。

    用户消息在**启动后台线程之前**就落库了 —— 这样即使模型调用立刻失败，
    用户的话也已经存下来了。否则用户会看到"我明明说了，它却像没听见"。
    """
    conversation = _load_conversation(conversation_no)
    conversation_id = int(conversation["id"])
    setup = TurnSetup(conversation_no=conversation_no, principal=principal)

    user_message = store.append_message(
        conversation_id, role=MSG_ROLE_USER, content=user_text
    )
    if conversation["title"] in ("", "新对话"):
        store.rename(conversation_id, user_text[:60])

    yield {"type": "message", "message": user_message}
    yield from _drain(conversation, setup, user_text)


def stream_resume(*, conversation_no: str, principal: Principal) -> Iterator[dict[str, Any]]:
    """接着把上一轮跑完 —— **不追加新的用户消息**。

    审批通过后走这条路：历史里已经有一条 tool 消息（执行结果或驳回理由），
    模型的下一轮会读到它，自然地继续说话或道别。
    """
    conversation = _load_conversation(conversation_no)
    setup = TurnSetup(conversation_no=conversation_no, principal=principal)
    yield from _drain(conversation, setup, None)


def _drain(
    conversation: dict[str, Any], setup: TurnSetup, user_text: str | None
) -> Iterator[dict[str, Any]]:
    """起后台线程跑循环，把队列里的事件逐个吐出来。"""
    events: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=_run_worker,
        args=(conversation, setup, user_text, events),
        name=f"chat-{conversation['no']}",
        daemon=True,
    )
    worker.start()

    # 心跳靠**取队列超时**实现，不用定时器：有事件就立刻推、没事件才 ping。
    # 这样正常回合里一个多余的帧都不会多。
    #
    # ⚠ 一个已知的取舍：客户端断开（关页面）时，生成器被关闭，
    # 但后台线程**不会被取消** —— 它会把这轮跑完并把消息落库。
    # 这是刻意的：那轮回答已经产生了、也已经花了 token，
    # 丢掉它反而让用户下次进来看到一段莫名其妙断掉的历史。
    while True:
        try:
            item = events.get(timeout=15.0)
        except queue.Empty:
            yield {"type": "ping"}
            continue
        if item is _SENTINEL:
            return
        if isinstance(item, dict) and item.get("type") == "__error__":
            yield {"type": "error", "detail": item["detail"]}
            yield {"type": "done", "status": "error"}
            return
        yield item


_SENTINEL = object()


# ===========================================================================
# 对话内审批
# ===========================================================================
def resolve_approval(
    *,
    conversation_no: str,
    principal: Principal,
    approval_no: str,
    approved: bool,
    reason: str = "",
) -> Iterator[dict[str, Any]]:
    """处理一次对话内审批。

    批准 → **真正执行那个工具** → 把结果写成 tool 消息 → 继续跑这一轮；
    驳回 → 也写一条 tool 消息（内容是说"用户拒绝了这个操作"）→ 继续跑。

    ## 驳回为什么也要写 tool 消息、也要继续跑

    因为模型已经发出了那个 tool_call，历史里就有一条
    ``assistant(tool_calls)`` 在等回应。不补上回应的话：

    1. 重放这段历史时，OpenAI 兼容接口会直接 400
       （``messages with role 'tool' must be a response to a preceding message``）
    2. 用户下次说话时，模型不知道自己上一轮想干什么、更不知道被拒了，
       会**再提一次同样的请求** —— 用户会觉得"它怎么又说一遍"

    所以驳回不是"什么都不做"，而是**把拒绝本身作为信息交给模型**，
    并明确告诉它不要重试同一个操作。
    """
    from fashionops.config import get_settings

    conversation = _load_conversation(conversation_no)
    conversation_id = int(conversation["id"])
    setup = TurnSetup(conversation_no=conversation_no, principal=principal)

    with session_scope() as session:
        row = store.get_approval(session, approval_no)
        if row is None or row.conversation_id != conversation_id:
            raise LookupError(f"审批单 {approval_no} 不存在")
        if row.status != "pending":
            raise ValueError(f"这条审批已经处理过了（当前状态：{row.status}）")
        tool_name = row.tool_name
        arguments = dict(row.tool_arguments or {})
        tool_call_id = row.tool_call_id
        owner_username = conversation["username"]

    settings = get_settings()

    if approved:
        # ---- 两道检查，都是"谁有权做这件事" ----
        required = _required_scopes(tool_name)
        if required and not (required & setup.granted_scopes):
            raise PermissionError(
                f"当前账号没有执行 {tool_name} 所需的权限：{' / '.join(sorted(required))}"
            )
        if settings.approval_require_separate_user and principal.username == owner_username:
            # 能发起和能批准必须是两个人，否则闸口退化成"点一下就过"。
            # 这条和任务审批走的是同一个配置开关，行为保持一致。
            raise PermissionError(
                "不能批准自己发起的操作（FASHIONOPS_APPROVAL_REQUIRE_SEPARATE_USER=true）。"
                "请让另一位有权限的同事处理"
            )

    if approved:
        payload = _execute_approved(tool_name, arguments, setup, principal)
    else:
        payload = {
            "ok": False,
            "denied": True,
            "error": {
                "code": "USER_REJECTED",
                "message": "用户拒绝了这次操作",
                "hint": (
                    "**不要重试同一个操作**。可以问用户希望怎么改，"
                    "或者改成一个不涉及该动作的方案。"
                ),
                "reason": reason,
            },
        }

    tool_message = store.append_message(
        conversation_id,
        role=MSG_ROLE_TOOL,
        content=json.dumps(payload, ensure_ascii=False, default=str),
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_result=payload,
        tool_ok=bool(payload.get("ok")),
    )
    _decide(
        approval_no,
        approved=approved,
        principal=principal,
        reason=reason,
        execution_result=payload,
    )

    yield {"type": "message", "message": tool_message}
    yield {
        "type": "approval_decided",
        "approval_no": approval_no,
        "approved": approved,
        "tool": tool_name,
        "ok": bool(payload.get("ok")),
    }
    yield from _drain(conversation, setup, None)


def _required_scopes(tool_name: str) -> frozenset[str]:
    from fashionops.gateway.registry import ToolNotFound

    try:
        return registry.get(tool_name).scopes
    except ToolNotFound:
        return frozenset()


def _execute_approved(
    tool_name: str, arguments: dict[str, Any], setup: TurnSetup, approver: Principal
) -> dict[str, Any]:
    """真正执行被批准的工具。

    ## 用谁的权限执行：**发起人 ∩ 批准人**

    单用批准人的：如果发起人的权限在"请求"到"批准"之间被撤销了，
    这次执行会绕开那个撤销 —— 而撤销权限的人恰恰认为自己已经收回了能力。

    单用发起人的：批准人可能根本没有这个权限，等于点一下"同意"就能
    借别人的权限做事，"谁有权批"这个检查失去意义。

    取交集两种漏洞都堵上，代价只是偶尔会拒掉一次"其实没问题"的操作 ——
    拒掉是安全的，放行不是。
    """
    from fashionops.security import service
    from fashionops.tools import AGENT_SCOPES

    granted = setup.granted_scopes
    owner_id = _owner_user_id(setup.conversation_no)
    if owner_id is not None:
        with session_scope() as session:
            owner = service.load_principal(session, owner_id)
        if owner is not None:
            owner_scopes = effective_scopes(owner, AGENT_SCOPES[CHAT_AGENT_NAME])
            granted = granted & owner_scopes

    gateway = ToolGateway(registry)
    ctx = CallContext(actor=f"chat-approval:{approver.username}", scopes=granted)
    return gateway.call(tool_name, arguments, ctx=ctx).to_dict()


def _owner_user_id(conversation_no: str) -> int | None:
    with session_scope() as session:
        row = store.get_conversation(session, conversation_no)
        return None if row is None else row.user_id


def _decide(
    approval_no: str,
    *,
    approved: bool,
    principal: Principal,
    reason: str,
    execution_result: dict[str, Any],
) -> None:
    from fashionops.db.base import utcnow
    from fashionops.db.models_chat import (
        APPROVAL_STATUS_APPROVED,
        APPROVAL_STATUS_REJECTED,
    )

    with session_scope() as session:
        row = store.get_approval(session, approval_no)
        if row is None:
            return
        row.status = APPROVAL_STATUS_APPROVED if approved else APPROVAL_STATUS_REJECTED
        row.decided_by_id = principal.user_id
        # 名字要存**快照**：用户被删/改名后，"当时是谁批的"不能跟着变。
        # 审计记录的铁律 —— 它记录的是当时发生了什么。
        row.decided_by = principal.username
        row.decided_at = utcnow()
        row.decision_reason = reason
        row.execution_result = execution_result


def _run_worker(
    conversation: dict[str, Any],
    setup: TurnSetup,
    user_text: str,
    events: queue.Queue,
) -> None:
    """在后台线程里跑工具循环，把事件塞进队列。

    ## 为什么必须开线程

    ``run_tool_loop`` 是**同步回调式**的：它在生成文本时调 ``on_token``。
    而我们要把每个 token 立刻 yield 出去（不然就没有打字机效果了），
    ``yield`` 只能出现在生成器里 —— 回调里 yield 不了。

    所以让循环在另一个线程里跑，事件经队列传回生成器。
    这也是为什么“只有一个循环实现”这件事值得坚持：如果为了避开线程
    再写一套内联的循环，两套逻辑迟早会漂移（比如流式那套忘了记账）。
    """
    conversation_id = int(conversation["id"])
    try:
        _drive(conversation_id, setup, user_text, events)
    except Exception as exc:  # noqa: BLE001
        logger.exception("对话 %s 回合失败", conversation["no"])
        detail = f"{type(exc).__name__}: {exc}"
        try:
            store.append_message(
                conversation_id,
                role=MSG_ROLE_ASSISTANT,
                content=f"这一轮没跑完：{detail}",
                finish_reason="error",
                interrupted=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("连失败消息都没写进去")
        events.put({"type": "__error__", "detail": detail})
    finally:
        events.put(_SENTINEL)


def _drive(
    conversation_id: int,
    setup: TurnSetup,
    user_text: str,
    events: queue.Queue,
) -> None:
    from fashionops.config import get_settings
    from fashionops.llm.client import ToolLoopResult
    from fashionops.tools import register_all_tools

    settings = get_settings()
    if not len(registry):
        register_all_tools(registry)

    # 网关每次新起一个：它是**请求级**的（限流器、熔断器都带状态），
    # 复用进程级单例会让不同用户的调用互相影响配额。
    gateway = ToolGateway(registry)
    catalog = build_catalog(
        granted_scopes=setup.granted_scopes, permission_codes=setup.permissions
    )

    with session_scope() as session:
        rows = store.history(session, conversation_id, limit=store.MAX_CONTEXT_MESSAGES)
    messages = store.to_llm_messages(rows, system_prompt=SYSTEM_PROMPT)

    # ---- 工具执行 ----
    def tool_runner(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        internal = from_llm_name(name) if "__" in name else name

        if internal == TASK_SUBMIT:
            return _submit_task(setup, arguments)

        if needs_approval(internal):
            # 需要人工确认：**不执行**，把整条循环挂起。
            # 注意这里用异常而不是返回一个"待确认"的 dict：
            # 返回 dict 会被回喂给模型，模型看到"失败"就会想办法绕过去。
            raise ToolLoopPaused(
                "approval",
                {
                    "tool": internal,
                    "arguments": arguments,
                    "preview": describe_preview(internal, arguments),
                },
            )

        ctx = CallContext(
            actor=f"chat:{setup.principal.username}",
            scopes=setup.granted_scopes,
            ip=None,
        )
        result = gateway.call(internal, arguments, ctx=ctx)
        return result.to_dict()

    # ---- 事件与落库 ----
    state: dict[str, Any] = {"text": [], "task_nos": []}

    def on_token(text: str) -> None:
        state["text"].append(text)
        events.put({"type": "token", "text": text})

    def on_tool_call(call: Any) -> None:
        events.put(
            {
                "type": "tool_call",
                "name": from_llm_name(call.name),
                "arguments": call.arguments,
            }
        )

    def on_tool_result(call: Any, payload: dict[str, Any]) -> None:
        # 顺序很重要：**先落 assistant（带 tool_call），再落 tool 结果**。
        # 反过来的话，重放历史时会得到一条"答在问之前"的消息序列，
        # 而 OpenAI 兼容接口要求 tool 消息必须紧跟在它响应的 tool_calls 之后。
        text = "".join(state["text"])
        state["text"].clear()
        internal = from_llm_name(call.name) if "__" in call.name else call.name

        assistant = store.append_message(
            conversation_id,
            role=MSG_ROLE_ASSISTANT,
            content=text,
            tool_call_id=call.id,
            tool_name=internal,
            tool_arguments=call.arguments,
            finish_reason="tool_calls",
        )
        events.put({"type": "message", "message": assistant})

        tool_message = store.append_message(
            conversation_id,
            role=MSG_ROLE_TOOL,
            content=json.dumps(payload, ensure_ascii=False, default=str),
            tool_call_id=call.id,
            tool_name=internal,
            tool_result=payload,
            tool_ok=bool(payload.get("ok")),
        )
        events.put(
            {
                "type": "tool_result",
                "name": internal,
                "ok": bool(payload.get("ok")),
                "message": tool_message,
            }
        )

        task_no = (payload.get("data") or {}).get("task_no") if isinstance(payload, dict) else None
        if task_no:
            state["task_nos"].append(task_no)
            events.put({"type": "task_created", "task_no": task_no})

    result: ToolLoopResult = run_tool_loop(
        get_model_client(settings),
        messages,
        tools=catalog,
        tool_runner=tool_runner,
        max_steps=MAX_TOOL_STEPS,
        stream=True,
        on_token=on_token,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )

    store.bump_llm(conversation_id, calls=result.steps, model=settings.model_name)

    if result.paused:
        _persist_approval(conversation_id, result, state, events)
        return

    _persist_final(conversation_id, result, state, events)


def _persist_approval(
    conversation_id: int,
    result: Any,
    state: dict[str, Any],
    events: queue.Queue,
) -> None:
    """把"等着人确认"这个状态落库，并推一张卡片给前端。"""
    call = result.pending_tool_call
    payload = result.paused_payload or {}
    text = "".join(state["text"]) or "这一步需要你确认一下。"
    internal = str(payload.get("tool") or (call.name if call else "unknown"))

    message = store.append_message(
        conversation_id,
        role=MSG_ROLE_ASSISTANT,
        content=text,
        tool_call_id=call.id if call else None,
        tool_name=internal,
        tool_arguments=payload.get("arguments"),
        finish_reason="approval_required",
    )
    events.put({"type": "message", "message": message})

    approval = store.create_approval(
        conversation_id=conversation_id,
        message_id=message["id"],
        tool_call_id=call.id if call else "",
        tool_name=internal,
        arguments=payload.get("arguments"),
        preview=str(payload.get("preview") or ""),
        reason="写操作需要人工确认",
    )
    events.put({"type": "approval_required", "approval": approval})
    events.put({"type": "done", "status": "waiting_approval"})


def _persist_final(
    conversation_id: int,
    result: Any,
    state: dict[str, Any],
    events: queue.Queue,
) -> None:
    text = "".join(state["text"]) or result.content
    usage = result.total_usage
    message = store.append_message(
        conversation_id,
        role=MSG_ROLE_ASSISTANT,
        content=text,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cost=result.total_cost,
        latency_ms=result.total_latency_ms,
        finish_reason="length" if not result.finished else "stop",
    )
    events.put({"type": "message", "message": message})

    if not result.finished:
        # 步数用尽 —— 这是**必须告诉用户**的状态。
        # 不说的话用户会以为"答案就这些了"，而实际上 Agent 是被截断的。
        events.put(
            {
                "type": "token",
                "text": (
                    f"\n\n（这个需求我用了 {result.steps} 轮工具调用还没处理完，"
                    "已暂停。建议改用任务方式跑，或者把需求拆小一点。）"
                ),
            }
        )
    events.put({"type": "done", "status": "ok", "steps": result.steps})


def _submit_task(setup: TurnSetup, arguments: dict[str, Any]) -> dict[str, Any]:
    """对话里启动一个真实任务。

    ## 为什么这里不做审批

    建任务只是**入队**，它本身不改变外部世界。而任务内部涉及发信、
    改预算之类的步骤时，那道闸口照样会拦。

    两道闸口叠在一起（建任务时确认一次、发信时再确认一次）会让人
    开始无脑点"同意"，闸口就退化成形式主义 —— 这正是这个项目一直在反对的。

    ## 为什么权限判的是 ``task:submit``

    因为"能不能开始干活"是身份域的权限，和网关那套网络作用域
    （``erp:read`` 这种）不是一回事。混着查会串。
    """
    if "task:submit" not in setup.permissions:
        return {
            "ok": False,
            "error": {
                "code": "PERMISSION_DENIED",
                "message": "当前账号没有提交任务的权限",
                "hint": "请让运营主管来发起，或者改用查询类问题。",
            },
        }

    from fashionops.agents import execution
    from fashionops.agents.execution import TASK_AGENT
    from fashionops.tools import AGENT_SCOPES

    request_text = str(arguments.get("request") or "").strip()
    if len(request_text) < 2:
        return {
            "ok": False,
            "error": {
                "code": "INVALID_ARGUMENTS",
                "message": "request 太短，看不出要做什么",
                "hint": "把用户的原话和约束条件完整写进 request。",
            },
        }

    # 权限继续向下传导：编排 Agent 只能用"这个人本来就有的"权限。
    # 对话这一层求过一次交，任务那一层再求一次 —— 两次都不能省：
    # 对话 Agent 的权限集和编排 Agent 的权限集本来就不同。
    granted = effective_scopes(setup.principal, AGENT_SCOPES[TASK_AGENT])

    try:
        task_no = execution.submit(
            user_request=request_text,
            task_type=str(arguments.get("task_type") or "generic"),
            created_by=setup.principal.username,
            created_by_id=setup.principal.user_id,
            scopes=granted,
        )
    except execution.TaskBusyError as exc:
        return {
            "ok": False,
            "error": {
                "code": "TASK_BUSY",
                "message": str(exc),
                "hint": "换一个任务号，或者等它跑完。",
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("对话里提交任务失败")
        return {
            "ok": False,
            "error": {
                "code": "TASK_SUBMIT_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
                "hint": "可以稍后重试；如果一直失败，去「任务」页手工提交。",
            },
        }

    return {
        "ok": True,
        "data": {
            "task_no": task_no,
            "status": "pending",
            "note": "任务已入队，正在后台执行。让用户去「任务」页看实时进度。",
        },
    }


__all__ = [
    "CHAT_AGENT_NAME",
    "MAX_TOOL_STEPS",
    "SYSTEM_PROMPT",
    "TurnSetup",
    "resolve_approval",
    "stream_resume",
    "stream_turn",
]
