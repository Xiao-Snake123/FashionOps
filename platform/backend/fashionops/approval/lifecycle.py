"""审批单的**有效期** —— "等人"这件事不能无限期等下去。

## `pending` 不会自己变

`APPROVAL_STATUS_PENDING` 只表示"等待处理"，它是个**静止**的状态：
没有任何定时任务、也没有任何读取路径会去动它。所以一张没人理的审批单
会永远停在 `pending`。这带来三个问题，严重程度递减：

1. **安全的（最严重）**：三天前起草的那封邮件，今天点一下"确认"就发出去了。
   而当时的数据、当时人的判断依据早就变了 —— 审批的全部意义是
   "**这一次**、发给这个人、这个内容，人同意了吗"，隔了三天那次"同意"
   答的已经不是同一个问题。
2. **看板的**：`SELECT * FROM approvals WHERE status='pending'` 是运维判断
   "现在有几个闸口卡着人"的唯一依据（`db/models_chat.py` 里就写着这条查询）。
   幽灵单让这个数字永远不为零，于是没人再信它 —— 看板失去意义比没有看板更糟。
3. **界面的**：用户刷新对话，三个月前那张卡片又回来了，而且**还能点**。

`APPROVAL_STATUS_EXPIRED` 这个状态从表建起来那天就存在
（`db/models_chat.py` 的注释写着"用户等太久没点 / 会话被归档时批量作废"），
但**一直没有代码去写它** —— 于是那个状态从来没出现过。
本模块就是补上这块。

## 两处执行口都要拦，缺一不可

作废状态本身拦不住任何人 —— 它可以被绕过（快照读、前端拿着旧卡片直接 POST）。
真正拦住的是**执行前的那次判断**：

* 对话侧：`chat/agent.py::resolve_approval`
* 任务侧：`api/routers/tasks.py::approve_task`

读取侧的过滤（`pending_for_*`）只解决第 2、3 个问题，
**它不构成安全边界** —— 一个知道审批单号的人可以直接调决定接口。
两层都要有：过滤让正常用户看不到幽灵单，执行口让绕过者拿不到执行。

## 为什么读取侧还要**再按时间过滤一遍**

因为写库和读库往往不在同一个事务里，而 MySQL 默认是 REPEATABLE READ：
调用方的 session 在鉴权阶段就已经读过数据、快照已经固定，
它**看不见**上面那次把 `pending` 改成 `expired` 的 UPDATE。
只靠 `status` 过滤的话，过期单在同一个请求里照样会被返回。

所以读取路径是"先落库作废（为了看板和事后复盘）+ 查询时再卡一次时间戳"。
这个组合的坑在 `chat/store.create_conversation` 的注释里已经记过一次了。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update

from fashionops.db import session_scope
from fashionops.db.base import utcnow
from fashionops.db.models_chat import (
    APPROVAL_STATUS_EXPIRED,
    APPROVAL_STATUS_PENDING,
    Approval,
)


def ttl_minutes() -> int:
    """审批单的有效期（分钟）。``<= 0`` 表示**永不过期**。

    留这个开关是为了让"小团队、人就坐在电脑前"的场景能退回旧行为 ——
    有些团队确实希望单子一直挂着。但默认是**开**的：
    漏拦的代价是"三天前的邮件照发"，误拦的代价只是"再问一次"。代价不对等。
    """
    from fashionops.config import get_settings

    return int(getattr(get_settings(), "approval_ttl_minutes", 120) or 0)


def cutoff(now: datetime | None = None) -> datetime | None:
    """比这个时间早创建的 pending 单都算过期。

    返回 ``None`` 表示"不设期限"（TTL 关掉了）—— 调用方据此跳过时间过滤。
    """
    minutes = ttl_minutes()
    if minutes <= 0:
        return None
    return (now or utcnow()) - timedelta(minutes=minutes)


def _as_datetime(value: datetime | str | None) -> datetime | None:
    """把 ``datetime`` / ISO 字符串统一成 ``datetime``，坏值给 ``None``。

    为什么必须两种都吃：ORM 给的是 ``datetime``，而
    ``approval/records.serialize`` 给的是 ``isoformat()`` 字符串 ——
    而"把 serialize 出来的那条直接传给 is_expired"是最自然的写法。
    只收一种的话，另一种的报错是
    ``TypeError: '<' not supported between 'str' and 'datetime'``，
    从这个报错完全看不出是"漏了解析"。
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_expired(created_at: datetime | str | None, *, now: datetime | None = None) -> bool:
    """这张单子是不是已经等太久了。

    ``created_at`` 认不出来时判**不过期**：那说明数据本身有问题，
    而"数据有问题"应该在别处报错，不该伪装成"过期了" ——
    后者会让人以为只是等久了，实际是脏数据。
    """
    limit = cutoff(now)
    moment = _as_datetime(created_at)
    if limit is None or moment is None:
        return False
    return moment < limit


def expired_message(*, tool_name: str = "", minutes: int | None = None) -> str:
    """给用户看的过期说明。

    **刻意不写"接下来怎么办"。** 那个答案取决于上下文，而且两边完全不同：

    * 对话里 —— "重新说一遍需求"，一句话就能重来
    * 任务里 —— "点驳回把它停掉"，因为任务已经提交、不会自己消失

    写死在这里，两边就会共用一句不贴切的建议，用户照着做会发现行不通。
    所以这里只说清"发生了什么、为什么这么设计"，下一步由调用方补。
    """
    window = minutes if minutes is not None else ttl_minutes()
    hours = window / 60
    window_text = f"{window} 分钟" if window < 120 else f"{hours:g} 小时"
    subject = f"{tool_name} 的确认单" if tool_name else "这张确认单"
    return (
        f"{subject}已经超过 {window_text} 没处理，已作废。"
        "**这不是出错**，而是刻意的：隔了这么久，当时的数据和判断依据可能已经变了，"
        "那时点下的「同意」答的已经不是同一个问题。"
    )


def expire_stale(*, now: datetime | None = None) -> int:
    """把所有超过有效期的 pending 单批量置为 ``expired``，返回作废条数。

    用一条 UPDATE 而不是逐行读改写：这张表可能积了几百条幽灵单，
    逐行处理会在一个请求里做几百次往返。条件全在 SQL 里，一次搞定。
    """
    limit = cutoff(now)
    if limit is None:
        return 0

    with session_scope() as session:
        result = session.execute(
            update(Approval)
            .where(
                Approval.status == APPROVAL_STATUS_PENDING,
                Approval.created_at < limit,
            )
            .values(status=APPROVAL_STATUS_EXPIRED)
        )
        return int(result.rowcount or 0)


def mark_expired(approval_no: str) -> None:
    """把单张作废（执行口拦住之后调用，让状态与"它没被执行"这件事一致）。"""
    with session_scope() as session:
        session.execute(
            update(Approval)
            .where(Approval.approval_no == approval_no)
            .values(status=APPROVAL_STATUS_EXPIRED)
        )


__all__ = [
    "cutoff",
    "expire_stale",
    "expired_message",
    "is_expired",
    "mark_expired",
    "ttl_minutes",
]
