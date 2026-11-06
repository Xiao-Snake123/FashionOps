"""pytest 共享夹具与助手。

## 为什么会有这个文件

加了鉴权之后，"打接口"这件事多了一步前置条件：**得先有一个能登录的用户**。
几乎每个碰接口的测试文件都要这一步，所以把它抽出来放这里。

## 为什么是"真的登录一次"而不是伪造一个 token

伪造（自己用 `TokenService` 签一个）更快，但会把**真正容易错的地方**绕过去：

- 权限同步有没有跑（`sync_identity`）
- 角色授权对不对（`ROLE_PRESETS` 里的权限码有没有拼错）
- 登录接口本身能不能用
- 令牌能不能通过 `get_principal` 的校验（issuer / type / 签名算法）

这四件事任何一件坏了，伪造 token 的测试都照样绿。而这些恰恰是新增鉴权代码里
最容易出错的部分。多花几十毫秒换这些覆盖，非常值。
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """临时 SQLite 库。

    ## 为什么必须有它

    没有它的话，任何走 `session_scope()` 的代码（记录器、审批单、todo）
    都会打到**开发库**上 —— 跑一次测试污染一次真实数据，
    而且测试之间会互相看见对方留下的任务。

    做法是把 `fashionops.db.base.get_settings` 换成指向 `tmp_path` 的配置，
    再建表。改动是 monkeypatch 的，夹具结束自动还原。
    """
    import fashionops.db.base as db_base
    from fashionops.config import Settings
    from fashionops.db.base import Base, build_engine, reset_engine

    settings = Settings(data_dir=tmp_path, db_host=None)
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)

    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()
    reset_engine()


@pytest.fixture(autouse=True)
def tools_registered():
    """把业务工具注册进进程内注册表。

    `needs_approval` / 工具目录裁剪都依赖它 —— 没注册的话，
    任务执行会在"工具不存在"上失败，而报错点离原因很远。
    注册表本身不允许重名注册，所以先判空。
    """
    from fashionops.gateway.registry import registry
    from fashionops.tools import register_all_tools

    if not len(registry):
        register_all_tools(registry)


@pytest.fixture(autouse=True)
def business_cache_off(monkeypatch):
    """测试期间关掉业务缓存（Redis db2）。**不关会跨用例串数据。**

    每个用例用的是自己那份**临时 SQLite 库**（见 ``db`` 夹具），
    但 Redis 是**全局共享**的一份。开了缓存的话：用例 A 缓存了
    ``/api/v1/summary`` 的结果，用例 B 换了一个库却读到 A 的缓存 ——
    断言会莫名其妙地失败，而且**只在 Redis 真的在跑时才复现**
    （本地没起 Redis 反而"一切正常"，这类 bug 极难定位）。

    所以这里强制关掉，让测试永远走"回源查库"这条路径。
    """
    import fashionops.cache as cache_mod
    from fashionops.config import Settings

    disabled = Settings(business_cache_enabled=False)
    monkeypatch.setattr(cache_mod, "get_settings", lambda: disabled)


@pytest.fixture(autouse=True)
def audit_writes_off(monkeypatch):
    """测试期间把**落库审计**的写入动作关成空操作。

    ## 为什么必须关

    `ToolGateway` 的审计 sink 默认是 `SqlAuditSink`（落库）。单测跑在**临时
    SQLite** 上，而 `SqlAuditSink` 是**另开一个连接**去写的：调用方会话正处在
    事务里时，SQLite 会抛 `database is locked` —— 而且是**等 busy timeout
    （默认 5 秒）之后**才抛。一次工具调用白等 5 秒，全量 600+ 用例会从
    十几秒涨到几十分钟。生产用 MySQL 不受影响（已在本机 MySQL 上实测落库成功）。

    ## 为什么是"把 record 改空"而不是"换一个 MemoryAuditSink"

    换 sink 的话，`isinstance(gw.audit, SqlAuditSink)` 这类断言就会变成假绿
    —— 它本来要守的是"**生产接线的默认值是不是落库的**"（见
    `test_mcp.py::test_audit_sink_persists_instead_of_memory`）。
    把方法改空则保留了对象身份，只去掉副作用：**被替换的是行为，不是被测对象**。

    需要断言审计内容的用例，显式传 `audit_sink=MemoryAuditSink()` 即可
    （`test_gateway.py` 一直是这么写的），不受这个夹具影响。
    """
    from fashionops.gateway.audit import SqlAuditSink

    monkeypatch.setattr(SqlAuditSink, "record", lambda self, entry: None)


#: 测试用户的固定用户名。用固定值而不是随机值，是为了让失败时的报错可读 ——
#: `pytest_a1b2c3d4` 这种名字出现在报错里会让人以为是数据问题。
TEST_ADMIN_USERNAME = "pytest_admin"


def ensure_identity(session) -> None:  # noqa: ANN001
    """把权限码和内置角色同步进库。**建用户之前必须先跑** ——

    `create_user` 里要校验角色存在（`role_not_found`），
    没同步过的话会报"角色 admin 不存在"，而这个报错很容易被误解成
    "代码里的角色常量写错了"。
    """
    from fashionops.security.permissions import sync_identity

    sync_identity(session)


def create_admin(
    bind,  # noqa: ANN001
    *,
    username: str = TEST_ADMIN_USERNAME,
    password: str | None = None,
) -> tuple[str, str]:
    """在 `bind`（Engine 或 sessionmaker）指向的库里建一个超管。

    返回 `(username, password)`。

    **不用 `password=None` 让它自动生成**：自动生成只打印到 stdout，
    测试里拿不到。这里显式给一个随机但可读的密码。
    """
    from sqlalchemy.orm import sessionmaker

    from fashionops.security import service
    from fashionops.security.permissions import ROLE_ADMIN

    password = password or f"T3st-{secrets.token_hex(6)}"

    factory = bind if isinstance(bind, sessionmaker) else sessionmaker(bind=bind)
    with factory() as session:
        ensure_identity(session)
        existing = service.find_user(session, username)
        if existing is not None:
            # 已存在就重置密码 —— 跨测试运行复用同一个库时（MySQL 那条路）
            # 不能假设"上次跑完清理干净了"
            service.set_password(session, existing, password)
            if not existing.is_superuser:
                existing.is_superuser = True
            session.commit()
            return username, password

        service.create_user(
            session,
            username=username,
            password=password,
            full_name="测试管理员",
            roles=(ROLE_ADMIN,),
            is_superuser=True,
            created_by="pytest",
        )
        session.commit()
    return username, password


def delete_user(bind, username: str) -> None:  # noqa: ANN001
    """清理测试用户。**必须清理** ——

    MySQL 那条路径打的是开发库，留一个 `pytest_admin` 在里面
    会让人以为系统里有个野生管理员。而且这个账号是超管，风险是真实的。
    """
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from fashionops.db.models_identity import User

    factory = bind if isinstance(bind, sessionmaker) else sessionmaker(bind=bind)
    with factory() as session:
        user = session.scalar(select(User).where(User.username == username))
        if user is not None:
            session.delete(user)
            session.commit()


def login(client, username: str, password: str) -> dict[str, str]:  # noqa: ANN001
    """登录并返回可直接展开到 `client.get(..., headers=...)` 的字典。

    登录失败时**把响应体打进断言消息**：只看到 `assert 401 == 200` 的话，
    完全不知道是密码错了、账号锁了、还是权限同步没跑。
    """
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, f"登录失败：{resp.status_code} {resp.text[:500]}"
    body: dict[str, Any] = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}


def admin_headers_for(client, bind) -> dict[str, str]:  # noqa: ANN001
    """一行搞定：建超管 → 登录 → 返回请求头。"""
    username, password = create_admin(bind)
    return login(client, username, password)


__all__ = [
    "TEST_ADMIN_USERNAME",
    "admin_headers_for",
    "create_admin",
    "delete_user",
    "ensure_identity",
    "login",
]
