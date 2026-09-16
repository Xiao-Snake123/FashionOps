"""任务的提交与后台执行 —— 从 `api/routers/tasks.py` 抽出来。

## 为什么抽出来

这块逻辑原来长在 API 路由里，但它的**读者不止一个**：

* HTTP 路由（`POST /api/v1/tasks`）
* **对话 Agent**（用户在对话框里说"帮我出个报表"，Agent 要把这句话
  变成一个真正的任务 —— 见 `chat/agent.py` 的 `task.submit` 工具）

对话 Agent 去 `from fashionops.api.routers.tasks import ...` 是**依赖方向反了**：
业务逻辑不该依赖 HTTP 层。而且那条路会绕开"先落库再返回"这个约束，
两边的提交路径迟早会漂移。

## 这一层负责什么

* **去重**：同一个 task_no 不能被两个线程同时跑（同一个 LangGraph
  thread_id 上跑两个图，checkpoint 状态会互相覆盖）
* **先落库**：任务行必须在返回之前写好，否则前端拿到编号立刻查会 404
* **兜异常**：线程里抛出去的异常没人接，表现是"任务永远停在 running"
  而日志里什么都没有 —— 最难查的一类问题

## 这一层不负责什么

**不负责 HTTP 状态码。** 原本 `_claim` 直接 `raise HTTPException(409)`，
但那是 HTTP 层的概念 —— 对话 Agent 调用时不需要一个 409。
所以这里抛领域异常 :class:`TaskBusyError`，由各自的入口翻译成
自己的表达方式（路由翻成 409，对话翻成一句给模型看的错误）。
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select

from fashionops.db import session_scope
from fashionops.db.base import utcnow
from fashionops.db.models import Task

logger = logging.getLogger("fashionops.agents.execution")

#: 后台任务线程池。刻意开小：等模型是 IO 密集型，
#: 但并发太多会同时打满模型配额和内存，反而更慢。
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="task")

#: 正在跑的任务号。防止同一任务被并发提交两次 ——
#: 同一个 thread_id 上跑两个图会让 checkpoint 状态互相覆盖。
_running: set[str] = set()
_running_lock = threading.Lock()


class TaskBusyError(RuntimeError):
    """同一个 task_no 已经在跑了。"""

    def __init__(self, task_no: str) -> None:
        super().__init__(f"任务 {task_no} 正在执行中")
        self.task_no = task_no


# ---------------------------------------------------------------------------
# 组装 runner
# ---------------------------------------------------------------------------
def build_runner(
    task_no: str,
    *,
    task_id: int | None = None,
    scopes: frozenset[str] | None = None,
) -> Any:
    """组装一个能跑图并记账的 TaskRunner。抽成函数是为了让测试能直接调。

    ``scopes`` 是**用户权限 ∩ Agent 权限集**的结果（见 `api/routers/tasks.py`
    的模块 docstring）。传 ``None`` 时退回 Agent 的完整权限集 ——
    这是给脚本/演示用的路径，**对外入口必须传**，否则权限传导就断了。
    """
    from fashionops.agents.checkpoint import get_checkpointer
    from fashionops.agents.recorder import TaskRecorder
    from fashionops.agents.runner import TaskRunner
    from fashionops.agents.state import RunContext
    from fashionops.config import get_settings
    from fashionops.gateway import (
        ToolGateway,
        get_circuit_breaker,
        get_rate_limiter,
        registry,
    )
    from fashionops.llm import get_model_client
    from fashionops.tools import AGENT_SCOPES, register_all_tools

    # 注册表是进程级单例，重复注册会抛异常，所以先判空
    if not len(registry):
        register_all_tools(registry)

    settings = get_settings()
    gateway = ToolGateway(
        registry,
        rate_limiter=get_rate_limiter(settings.cache_backend),
        breaker=get_circuit_breaker(settings.cache_backend),
    )
    ctx = RunContext(
        gateway=gateway,
        model=get_model_client(settings),
        actor=f"orchestrator:{task_no}",
        scopes=AGENT_SCOPES["orchestrator"] if scopes is None else scopes,
        task_id=task_id,
    )
    return TaskRunner(ctx, recorder=TaskRecorder(), checkpointer=get_checkpointer(settings))


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def create_task_row(
    task_no: str,
    *,
    user_request: str,
    task_type: str,
    created_by: str,
    created_by_id: int | None = None,
) -> int | None:
    """插入 ``tasks`` 行（``pending``），返回它的 id。

    状态给 ``pending`` 而不是 ``running``：后台线程还没真的开跑，
    标成 running 会让"有线程在跑"和"只是排着队"两件事在界面上分不出来。
    ``pending`` 也在前端的 ``_RUNNING_STATES`` 里，所以照样会挂 SSE 等它。
    """
    with session_scope() as session:
        existing = session.scalar(select(Task).where(Task.task_no == task_no))
        if existing is not None:
            # 重跑同一个 task_no：把上一轮的终态清干净，否则界面会先显示旧结果
            existing.status = "pending"
            existing.progress = 0
            existing.result = None
            existing.error_message = None
            existing.approval_payload = None
            existing.finished_at = None
            existing.started_at = utcnow()
            # 重跑换人时也要把提交人改掉，否则审计里是上一个人的名字
            existing.created_by = created_by
            existing.created_by_id = created_by_id
            # 上一轮的审批结果必须清掉。不清的话，这一轮的审计里会带着
            # 上一位审批人的名字 —— 而这一轮的决策者可能完全是另一个人，
            # "谁批准了这封邮件"就答错了。**审计里答错比没有更危险。**
            existing.approved_by = None
            existing.approved_by_id = None
            existing.approved_at = None
            existing.approval_reason = None
            return existing.id
        task = Task(
            task_no=task_no,
            title=user_request[:120] or task_no,
            user_request=user_request,
            task_type=task_type,
            status="pending",
            progress=0,
            thread_id=task_no,
            created_by=created_by,
            created_by_id=created_by_id,
            started_at=utcnow(),
        )
        session.add(task)
        session.flush()
        return task.id


def new_task_no() -> str:
    return f"T{utcnow():%y%m%d%H%M%S}{uuid.uuid4().hex[:4].upper()}"


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------
def claim(task_no: str) -> None:
    with _running_lock:
        if task_no in _running:
            raise TaskBusyError(task_no)
        _running.add(task_no)


def release(task_no: str) -> None:
    with _running_lock:
        _running.discard(task_no)


def is_running(task_no: str) -> bool:
    with _running_lock:
        return task_no in _running


# ---------------------------------------------------------------------------
# 后台执行
# ---------------------------------------------------------------------------
def run_guarded(
    task_no: str,
    user_request: str,
    task_type: str,
    created_by: str,
    scopes: frozenset[str] | None = None,
) -> None:
    """后台线程里跑任务。

    **异常必须在这里兜住。** 线程里抛出去的异常没人接，
    表现是"任务永远停在 running"而日志里什么都没有 —— 最难查的一类问题。

    ``scopes`` 必须从请求线程传进来，不能在这里重新算 ——
    后台线程里没有请求上下文（拿不到 Principal）。
    这也意味着**审批通过后的续跑用的是提交时的权限快照**，
    而不是审批时的权限：如果提交人的权限被中途撤销，续跑仍用旧快照。
    这个窗口是任务的生命周期（分钟级），并且审计里记了当时的 granted_scopes，
    对当前场景可以接受；要彻底解决得把权限随任务持久化并在续跑时重新求交。
    """
    runner = None
    try:
        runner = build_runner(task_no, scopes=scopes)
        runner.submit(
            task_no=task_no,
            user_request=user_request,
            task_type=task_type,
            created_by=created_by,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("任务 %s 执行失败", task_no)
        mark_failed(task_no, runner, exc)
    finally:
        release(task_no)


def resume_guarded(task_no: str, decision: dict[str, Any]) -> None:
    runner = None
    try:
        runner = build_runner(task_no)
        runner.resume(task_no=task_no, decision=decision)
    except Exception as exc:  # noqa: BLE001
        logger.exception("任务 %s 恢复失败", task_no)
        mark_failed(task_no, runner, exc)
    finally:
        release(task_no)


def mark_failed(task_no: str, runner: Any, exc: Exception) -> None:
    """尽力把失败状态写进去。写不进去也至少要留下日志。"""
    try:
        from fashionops.agents.recorder import TaskRecorder

        recorder = runner.recorder if runner is not None else TaskRecorder()
        recorder.finish(
            task_no=task_no, status="failed", error_message=f"{type(exc).__name__}: {exc}"
        )
    except Exception:  # noqa: BLE001
        logger.exception("任务 %s 的失败状态也没能写进数据库", task_no)


# ---------------------------------------------------------------------------
# 一站式入口
# ---------------------------------------------------------------------------
def submit(
    *,
    user_request: str,
    task_type: str,
    created_by: str,
    created_by_id: int | None = None,
    scopes: frozenset[str] | None = None,
    task_no: str | None = None,
) -> str:
    """去重 → 落库 → 丢给线程池。返回任务号。

    ## 顺序是有讲究的

    **先 ``claim`` 去重、再落库、最后才交给线程池。** 反过来的话，
    两个并发请求会各自落一行、各跑一个图，而 LangGraph 的 checkpoint
    是按 ``thread_id`` 存的 —— 两个图会互相覆盖状态。

    ## 为什么落库必须在返回之前（一个真踩过的坑）

    最初这里是直接 submit 到线程池就 return，结果前端拿到 task_no 立刻去
    GET 详情 —— 而那一行还没插进去，于是 404。表现是刚提交完就跳到一个
    "请求失败：404"的死页面；而且因为首次加载失败、SSE 也没挂上，
    等任务真跑起来后页面**永远不会自己恢复**。

    用 curl 手工测的时候完全看不出来：人打完一条命令再敲第二条，中间隔着
    好几秒，刚好够后台线程把行写进去。**这个 bug 只能在点界面时暴露。**
    """
    number = task_no or new_task_no()
    claim(number)
    try:
        create_task_row(
            number,
            user_request=user_request,
            task_type=task_type,
            created_by=created_by,
            created_by_id=created_by_id,
        )
        EXECUTOR.submit(
            run_guarded,
            number,
            user_request,
            task_type,
            created_by,
            scopes,
        )
    except Exception:
        release(number)
        raise
    return number


def enqueue_resume(task_no: str, decision: dict[str, Any]) -> None:
    """把「审批后从断点继续」丢给线程池。

    **调用方负责先 ``claim()``**：审批结果落库必须发生在 claim 和 enqueue
    之间，而那段是路由自己的业务（要写 ``approved_by`` / ``approved_at``
    这些字段）。把那一段也搬进来的话，这一层就得知道"审批人是谁"、
    "要不要禁止自批"这种身份域的概念 —— 那不是它的职责。
    """
    EXECUTOR.submit(resume_guarded, task_no, decision)


__all__ = [
    "EXECUTOR",
    "TaskBusyError",
    "build_runner",
    "claim",
    "create_task_row",
    "enqueue_resume",
    "is_running",
    "mark_failed",
    "new_task_no",
    "release",
    "resume_guarded",
    "run_guarded",
    "submit",
]
