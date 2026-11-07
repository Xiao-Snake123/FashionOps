"""长期记忆接口 —— 查看 / 删除**自己**被记住的偏好与约定。

## 为什么只暴露"我自己的"

记忆是**个人数据**：它属于某个用户，且只应被本人和她正在对话的 Agent 看到。
所以这里只能操作 ``principal.user_id`` 名下的记忆，既不接受、也不返回别人的。

## 写入口不在这里

记忆是 Agent 在对话里通过 ``remember`` 工具**主动**记的（见 ``chat/longterm.py``）。
这里只做用户侧的两件事：**我能看到**（确认它记对了）、**我能反悔**（删掉）。
"改一条"也不在这里 —— 对助手说"渠道偏好那条改一下"，它会重新调 ``remember`` 覆盖，
重复的"更新"入口只会让两套规则对不齐。

## 为什么是独立路由，不是挂在 /conversations 下

记忆属于**用户**而不是某场会话，挂在 ``/conversations`` 前缀下语义会拧。
而且 ``/conversations/{no}`` 已经占了 ``/{param}`` 这个位置，
新增 ``/conversations/memories`` 会被 FastAPI 路由匹配抢掉（先注册的赢），
排错时非常隐蔽。独立前缀 ``/api/v1/memories`` 干净。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session

from fashionops.chat import longterm
from fashionops.db import get_db
from fashionops.db.models_memory import AgentMemory
from fashionops.security.deps import Principal, require_permission
from fashionops.security.permissions import PERM_CHAT_USE

logger = logging.getLogger("fashionops.api.memory")

router = APIRouter(prefix="/api/v1/memories", tags=["chat"])


@router.get("", summary="我记住了关于你的哪些事")
def list_my_memories(
    principal: Principal = Depends(require_permission(PERM_CHAT_USE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """返回当前用户被记住的长期记忆（按最近更新倒序）。

    无记忆时返回空列表，而不是 404 —— 空和"出错了"是两回事，
    前端要据此判断"显示空态"还是"报异常"。

    ``limit`` 是个上限而不是分页：一个人正常记不下几十条，
    但**不能因为没有上限就把全表拉出来** —— 记忆是 Agent 自己写的，
    攒多了之后每次开面板都全量查一遍没有意义。
    """
    rows = (
        session.query(AgentMemory)
        .filter(AgentMemory.user_id == principal.user_id)
        .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
        .limit(limit)
        .all()
    )
    return {"items": [longterm.serialize(row) for row in rows]}


@router.delete("/{memory_id}", summary="忘记一条记忆")
def forget_memory(
    memory_id: int,
    principal: Principal = Depends(require_permission(PERM_CHAT_USE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """删除当前用户名下的一条记忆。

    必须校验归属：**不能让 A 删掉 B 的记忆**。记忆 id 是自增整数、
    完全可以枚举，漏掉这一步就是越权删别人的数据。
    """
    row = session.get(AgentMemory, memory_id)
    if row is None or row.user_id != principal.user_id:
        raise HTTPException(status_code=404, detail=f"记忆 {memory_id} 不存在")
    session.execute(sa_delete(AgentMemory).where(AgentMemory.id == memory_id))
    session.commit()
    logger.info("用户 %s 删除了自己的记忆 %s（topic=%s）", principal.username, memory_id, row.topic)
    return {"ok": True, "id": memory_id}
