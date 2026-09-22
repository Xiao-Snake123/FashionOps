"""账号管理接口 —— 分级授权。

## 接口只回答"有没有资格"，"范围多大"由规则层判

本文件里所有端点用的都是同一个依赖：
`require_permission(PERM_ADMIN_USER, PERM_DEPT_USER)`（OR 语义）。
也就是说**部门主管和跨部门管理员进的是同一个接口**，
两者的差别完全由 `security.admin.resolve_scope()` 决定。

这么做而不是"给部门主管单独开一套 /department 前缀的接口"，理由是
两套接口会立刻分叉：加一个字段要在两个地方加，其中一个被忘了也不会报错 ——
它只会让部门主管那条路悄悄少了那个字段。而规则收在一处时，
"部门主管不能跨部门"这条判断只有一个实现，改也只改一次。

## 每个写操作都写审计

`record_audit` 记的是"哪个人点了哪个按钮"。账号管理是这个系统里
**唯一能改变其他人权限**的入口，如果它没有审计，
"这个 admin 账号是谁建的"这个问题就永远答不上来。

审计的 `detail` 里**绝不包含密码** —— 哪怕是一次性初始密码。
审计表经常被同步到别的地方（ELK、数仓），
密码一旦进去就等于泄漏，而且没人会想到去审计表里找密码。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fashionops.config import get_settings
from fashionops.db import get_db
from fashionops.db.models_identity import Department
from fashionops.security import admin as rules
from fashionops.security.deps import record_audit, require_permission
from fashionops.security.permissions import PERM_ADMIN_USER, PERM_DEPT_USER
from fashionops.security.service import AuthError, Principal

logger = logging.getLogger("fashionops.api.admin")

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

#: 进入账号管理的门槛。具体能管到哪，见 `rules.resolve_scope`。
_ANY_ADMIN = require_permission(PERM_ADMIN_USER, PERM_DEPT_USER)
#: 只有跨部门管理员能做的动作（建/改部门）。
_GLOBAL_ADMIN = require_permission(PERM_ADMIN_USER)


@contextmanager
def _admin_errors() -> Iterator[None]:
    """把规则层的异常翻译成 HTTP 状态码。

    状态码由规则层给（`AdminError.status`），路由层不猜 ——
    403 和 400 对这个页面是两种完全不同的处置方式：
    前者是"你去找有权限的人"，后者是"你把输入改一下"。
    路由层猜错的后果是用户按错误的提示去操作，然后还是不行。
    """
    try:
        yield
    except rules.AdminError as exc:
        logger.warning("账号管理被拒绝（%s）：%s", exc.reason or "-", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except AuthError as exc:
        # service 层抛的这些（用户名重复、邮箱占用、密码强度不够）都是**用户能修**的
        logger.info("账号管理参数被拒（%s）：%s", exc.reason or "-", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


def _serialize_department(dept: Department) -> dict:
    return {
        "department_id": dept.id,
        "code": dept.code,
        "name": dept.name,
        "description": dept.description,
        "is_active": dept.is_active,
    }


def _serialize_role(role) -> dict:  # noqa: ANN001
    return {
        "code": role.code,
        "name": role.name,
        "description": role.description,
        # 权限也一并给出：界面上要能解释"为什么这个角色我不能授予"，
        # 否则那个被灰掉的下拉项只会让人以为是 bug
        "permissions": sorted(role.permission_codes()),
    }


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class CreateAccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str | None = Field(
        default=None,
        description="留空则自动生成一个强密码，**只在响应里出现一次**",
    )
    full_name: str = Field(default="", max_length=128)
    email: str | None = Field(default=None, max_length=190)
    phone: str | None = Field(default=None, max_length=32)
    department_id: int | None = None
    role_codes: list[str] = Field(default_factory=list)
    is_superuser: bool = False


class UpdateAccountRequest(BaseModel):
    """部分更新。

    字段默认 `None` 在这里表示"没传"，靠 `model_dump(exclude_unset=True)`
    区分"没传"和"显式传了 null"。这个区别必须保住：
    清空邮箱（传 null）和 不动邮箱（不传）是两件不同的事，
    混起来就永远清不空一个字段。
    """

    full_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=190)
    phone: str | None = Field(default=None, max_length=32)
    department_id: int | None = None
    status: str | None = None
    role_codes: list[str] | None = None


class ResetPasswordRequest(BaseModel):
    password: str | None = Field(
        default=None,
        description="留空则生成随机强密码。指定密码时同样要过强度校验",
    )


class CreateDepartmentRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)


class UpdateDepartmentRequest(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
@router.get("/accounts", summary="账号列表（按管理范围自动过滤）")
def list_accounts(
    q: str | None = Query(default=None, description="按用户名 / 姓名 / 邮箱模糊搜索"),
    status_filter: str | None = Query(
        default=None, alias="status", description="active / disabled / locked / pending"
    ),
    department_id: int | None = Query(default=None, description="仅跨部门管理员可用"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(_ANY_ADMIN),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """列表里顺带返回部门与"我能授予的角色"。

    合并成一个响应是为了让它们**永远同步**：新建一个账号之后，
    下拉框用的角色列表和列表数据是同一次请求拿回来的，
    不会出现"刚建的角色在别处已经能用、在这个页面还不认识"。
    这些数据只有几十条，合并的代价可以忽略。
    """
    with _admin_errors():
        scope = rules.resolve_scope(principal)
        items, total = rules.list_accounts(
            session,
            scope,
            query=q,
            status=status_filter,
            department_id=department_id,
            limit=limit,
            offset=offset,
        )
        return {
            "scope": {
                "global": scope.global_scope,
                "department_id": scope.department_id,
                "label": scope.label,
                "is_superuser": principal.is_superuser,
                "can_create": scope.global_scope or scope.department_id is not None,
            },
            "total": total,
            # 统计**不受 status 筛选影响**（永远按整个作用域算）：
            # 点了"只看已停用"之后，"启用 12 个"如果跟着变成 0，
            # 那它就不再是"我有多少账号"的答案，而只是一个重复的计数
            "stats": rules.account_stats(session, scope),
            "items": [rules.serialize_account(scope, u) for u in items],
            "departments": [
                _serialize_department(d) for d in rules.list_departments(session, scope)
            ],
            "assignable_roles": [
                _serialize_role(r) for r in rules.assignable_roles(session, scope)
            ],
        }


# ---------------------------------------------------------------------------
# 写：账号
# ---------------------------------------------------------------------------
@router.post("/accounts", summary="新建账号")
def create_account(
    payload: CreateAccountRequest,
    request: Request,
    principal: Principal = Depends(_ANY_ADMIN),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    with _admin_errors():
        scope = rules.resolve_scope(principal)
        result = rules.create_account(
            session,
            scope,
            username=payload.username,
            password=payload.password,
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            department_id=payload.department_id,
            role_codes=payload.role_codes,
            is_superuser=payload.is_superuser,
            settings=get_settings(),
        )
        record_audit(
            session,
            principal=principal,
            action="admin.create_account",
            resource=f"user:{result.user.id}",
            detail={
                "username": result.user.username,
                "department_id": result.user.department_id,
                "roles": payload.role_codes,
                "is_superuser": payload.is_superuser,
                # 刻意不带密码 —— 见模块头部注释
                "password_generated": result.generated_password is not None,
            },
            request=request,
        )
        return {
            "account": rules.serialize_account(scope, result.user),
            "generated_password": result.generated_password,
            "warnings": result.warnings,
        }


@router.patch("/accounts/{user_id}", summary="修改账号（资料 / 部门 / 状态 / 角色）")
def update_account(
    user_id: int,
    payload: UpdateAccountRequest,
    request: Request,
    principal: Principal = Depends(_ANY_ADMIN),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    with _admin_errors():
        scope = rules.resolve_scope(principal)
        target = rules.get_account(session, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"账号 {user_id} 不存在")

        # exclude_unset 是关键：它让"没传"和"传了 null"分开（见请求模型的注释）
        provided = payload.model_dump(exclude_unset=True)
        notes = rules.update_account(
            session,
            scope,
            target,
            **{key: value for key, value in provided.items()},
        )
        record_audit(
            session,
            principal=principal,
            action="admin.update_account",
            resource=f"user:{target.id}",
            detail={"username": target.username, "changed": sorted(provided)},
            request=request,
        )
        return {
            "account": rules.serialize_account(scope, target),
            "notes": notes,
        }


@router.post("/accounts/{user_id}/reset-password", summary="重置账号密码")
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    request: Request,
    principal: Principal = Depends(_ANY_ADMIN),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """重置密码。

    ## 为什么没有"查看密码"这个接口

    密码用 argon2id 哈希存储，**数学上不可逆** —— 不是"不给你看"，
    是系统自己也不知道。所以"掌管所有账号密码"在这里的唯一实现方式
    就是"能重置任何人的密码"。

    自研/外包项目里常见的"密码可查看"功能，真实做法是把明文存进另一张表，
    而那张表一旦泄漏，全公司的密码就都出去了 —— 包括员工在别处复用的密码。
    这个代价换不来任何东西：管理员真正需要的能力是"帮人恢复访问"，重置就够。

    ## 重置会踢掉该账号的所有登录

    `service.set_password` 会撤销他全部 refresh token。
    这是必须的：重置的动机通常是"怀疑这个账号被盗"，
    旧会话还在的话攻击者手里的令牌照样能换新的 access token。
    """
    with _admin_errors():
        scope = rules.resolve_scope(principal)
        target = rules.get_account(session, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"账号 {user_id} 不存在")

        generated = rules.reset_password(
            session,
            scope,
            target,
            password=payload.password,
            settings=get_settings(),
        )
        record_audit(
            session,
            principal=principal,
            action="admin.reset_password",
            resource=f"user:{target.id}",
            detail={
                "username": target.username,
                "generated": bool(generated),
                "sessions_revoked": True,
            },
            request=request,
        )
        return {
            "username": target.username,
            "generated_password": generated or None,
            "message": (
                "已生成新的随机密码，请立刻复制并发给本人 —— 关闭后无法再查看"
                if generated
                else "已设置为指定的密码，并已撤销该账号的全部登录"
            ),
        }


# ---------------------------------------------------------------------------
# 写：部门
# ---------------------------------------------------------------------------
@router.post("/departments", summary="新建部门（需要跨部门管理权限）")
def create_department(
    payload: CreateDepartmentRequest,
    request: Request,
    principal: Principal = Depends(_GLOBAL_ADMIN),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    with _admin_errors():
        code = payload.code.strip()
        existing = session.scalar(select(Department).where(Department.code == code))
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"部门标识 {code} 已存在",
            )
        dept = Department(
            code=code, name=payload.name.strip(), description=payload.description
        )
        session.add(dept)
        session.flush()
        record_audit(
            session,
            principal=principal,
            action="admin.create_department",
            resource=f"department:{dept.id}",
            detail={"code": dept.code, "name": dept.name},
            request=request,
        )
        return {"department": _serialize_department(dept)}


@router.patch("/departments/{department_id}", summary="修改部门（改名 / 停用）")
def update_department(
    department_id: int,
    payload: UpdateDepartmentRequest,
    request: Request,
    principal: Principal = Depends(_GLOBAL_ADMIN),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    with _admin_errors():
        dept = session.get(Department, department_id)
        if dept is None:
            raise HTTPException(status_code=404, detail=f"部门 {department_id} 不存在")

        provided = payload.model_dump(exclude_unset=True)
        if "name" in provided and provided["name"]:
            dept.name = provided["name"].strip()
        if "description" in provided and provided["description"] is not None:
            dept.description = provided["description"]
        if "is_active" in provided and provided["is_active"] is not None:
            # 停用部门**不动已归属的人**：把一批账号瞬间变成"无部门"
            # 会让那些部门的主管立刻管不到自己的人，而他们不会收到任何通知
            dept.is_active = bool(provided["is_active"])
        session.flush()

        record_audit(
            session,
            principal=principal,
            action="admin.update_department",
            resource=f"department:{dept.id}",
            detail={"code": dept.code, "changed": sorted(provided)},
            request=request,
        )
        return {"department": _serialize_department(dept)}


__all__ = ["router"]
