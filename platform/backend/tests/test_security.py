"""鉴权测试 —— 密码 / 令牌 / 权限 / 登录流程 / 路由覆盖。

## 这个文件里最有价值的五条

1. **`test_epoch_conversion_is_timezone_aware`** —— 时序回归。
   之前 `to_epoch()` 用朴素 datetime 调 `.timestamp()`，东八区下
   token 一签发就被判过期（登录返回 200，下一个请求 401 "登录已过期"）。
   在 UTC 容器里这个 bug **完全不可见**，所以断言必须与时区无关。

2. **`test_every_role_permission_exists`** —— `ROLE_PRESETS` 里写的权限码
   必须在 `PERMISSIONS` 里真的存在。拼错一个字母的后果是
   "这个角色少了一个权限"，而**不报任何错** —— 直到有人点了按钮被拒。

3. **`test_intersection_blocks_escalation`** —— 权限向下传导。
   这是整个 RBAC 里唯一真正防住越权的地方：
   没有交集的话，只有 `task:submit` 的人可以让模型替他发邮件。

4. **`test_operator_can_submit_but_not_approve`** —— RBAC 在这个系统里
   存在的实际理由。发起和批准是两个人，人工闸口才有意义。

5. **`test_protected_routes_declare_permission`** —— 遍历全部路由。
   它能证明的不只是"现在安全"，而是"以后新增接口忘了加鉴权会在 CI 上变红"。

## 为什么登录流程测试要连数据库

`authenticate()` 里含失败计数、锁定、惰性解锁、哈希参数升级 ——
这些状态全在库里。mock 掉等于只测了一条 happy path。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fashionops.config import Settings
from fashionops.db.base import Base, build_engine, reset_engine, utcnow
from fashionops.db.models_identity import (
    USER_STATUS_ACTIVE,
    USER_STATUS_DISABLED,
    USER_STATUS_LOCKED,
)
from fashionops.security import passwords, service
from fashionops.security.deps import TokenService
from fashionops.security.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_CODES,
    PERMISSIONS,
    ROLE_ADMIN,
    ROLE_CODES,
    ROLE_OPERATOR,
    ROLE_PRESETS,
    ROLE_VIEWER,
    sync_identity,
    sync_permissions,
    sync_roles,
    user_permission_codes,
)
from fashionops.security.service import NOBODY, Principal
from fashionops.security.tokens import (
    TOKEN_TYPE_ACCESS,
    TokenError,
    from_epoch,
    hash_token,
    to_epoch,
)

#: 测试密码。满足策略（长度 >= 10、>= 2 类字符）
PASSWORD = "T3st-Password!x"


# ===========================================================================
# 库 fixture
# ===========================================================================
@pytest.fixture
def db(tmp_path, monkeypatch):
    """临时 SQLite 库 + 已同步的权限/角色。"""
    from fashionops.db import base as db_base

    settings = Settings(data_dir=tmp_path, db_host=None)
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)

    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        sync_identity(session)
        session.commit()

    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()
    reset_engine()


@pytest.fixture
def session(db):  # noqa: ANN001, ANN201
    with Session(db) as s:
        yield s


def make_user(session: Session, username: str = "zhang", **kwargs):  # noqa: ANN003, ANN201
    defaults = {"password": PASSWORD, "roles": (ROLE_OPERATOR,)}
    defaults.update(kwargs)
    return service.create_user(session, username=username, **defaults).user


# ===========================================================================
# 1. 密码
# ===========================================================================
class TestPasswords:
    def test_hash_then_verify(self):
        stored = passwords.hash_password(PASSWORD)
        assert stored.startswith("$argon2id$"), "用的不是 argon2id"
        assert passwords.verify_password(PASSWORD, stored) is True

    def test_hash_is_salted_so_two_hashes_differ(self):
        """同一个密码两次哈希必须不同。

        相同说明没加盐（或者盐是固定的）—— 那彩虹表就能一次命中所有用户。
        """
        assert passwords.hash_password(PASSWORD) != passwords.hash_password(PASSWORD)

    def test_wrong_password_rejected(self):
        stored = passwords.hash_password(PASSWORD)
        assert passwords.verify_password(PASSWORD + "x", stored) is False

    def test_none_hash_never_verifies(self):
        """SSO 用户没有密码 —— **任何**密码都不该能登进去。

        这里最容易写错的是 `if not password_hash: return True`
        （把"没有密码"当成"不需要密码"），那是一条直通后台的后门。
        """
        assert passwords.verify_password(PASSWORD, None) is False
        assert passwords.verify_password("", None) is False
        assert passwords.verify_password(PASSWORD, "") is False

    def test_broken_hash_does_not_raise(self):
        """库里的哈希串坏了要返回 False，不能抛异常。

        抛异常的话响应会是 500，而攻击者能靠"500 还是 401"
        区分出"这个账号的哈希是坏的"。
        """
        assert passwords.verify_password(PASSWORD, "not-a-hash") is False

    def test_unicode_normalization_makes_fullwidth_match(self):
        """全角/半角必须视作同一个密码。

        不归一化的话，用户在手机上（全角输入法）设的密码
        在电脑上永远登不进去，而**他看到的两次输入是一样的**。
        """
        stored = passwords.hash_password("Abcd1234ef")
        assert passwords.verify_password("Ａｂｃｄ1234ef", stored) is True

    def test_needs_rehash_detects_weaker_params(self):
        assert passwords.needs_rehash("$argon2i$v=19$m=8,t=1,p=1$aaaa$bbbb") is True
        assert passwords.needs_rehash("nonsense") is True

    @pytest.mark.parametrize(
        ("password", "expect_ok"),
        [
            ("short1!", False),  # 太短
            ("password", False),  # 常见密码
            ("12345678", False),  # 常见密码
            ("aaaaaaaaaaaa", False),  # 只有 1 类字符
            ("Abcdefghij12", True),  # 2 类，够长
            ("中文密码中文密码中文密码", True),  # 全中文：熵够，放行
        ],
    )
    def test_strength_policy(self, password, expect_ok):
        if expect_ok:
            passwords.validate_strength(password)
        else:
            with pytest.raises(passwords.PasswordPolicyError):
                passwords.validate_strength(password)

    def test_generated_password_satisfies_own_policy(self):
        """生成器必须满足自己的策略。

        不满足的话 `create_user` 里会抛 PasswordPolicyError ——
        表现是"自动生成密码"这条最省事的路直接走不通。
        """
        for _ in range(5):
            generated = passwords.generate_password()
            passwords.validate_strength(generated, min_length=20, require_classes=3)


# ===========================================================================
# 2. 令牌（含时序回归）
# ===========================================================================
class TestTokens:
    @pytest.fixture
    def tokens(self) -> TokenService:
        return TokenService(secret="x" * 48, access_minutes=30, refresh_days=14, issuer="fashionops")

    def test_roundtrip(self, tokens):
        pair = tokens.create_pair(user_id=7, username="zhang")
        user_id, jti = tokens.decode_access(pair.access_token)
        assert user_id == 7
        assert jti
        assert tokens.decode_refresh(pair.refresh_token).user_id == 7

    def test_access_and_refresh_have_different_jti(self, tokens):
        """两个 token 的 jti 必须不同。

        相同的话，将来"按 jti 撤销某一次会话"会顺带把另一个也撤掉，
        而调用方完全看不出来。
        """
        pair = tokens.create_pair(user_id=1, username="a")
        access_jti = tokens.decode_access(pair.access_token)[1]
        refresh_jti = tokens.decode_refresh(pair.refresh_token).jti
        assert access_jti != refresh_jti

    def test_epoch_conversion_is_timezone_aware(self):
        """`to_epoch` 必须把朴素时间当 UTC，而不是当本地时间。

        这条是那个"一登录就过期" bug 的**直接**回归防线。
        用错误的实现（按本地时区解释朴素时间）时，只要本机不是 UTC，
        往返就会差整个时区偏移 —— 而在 UTC 的 CI 上它恰好也是对的（差 0）。

        所以这里拿 `calendar.timegm` 做基准：它是"把朴素时间当 UTC"
        的标准做法，不依赖本机时区。**只比较往返相等是不够的**
        （错误实现的自洽往返也会通过）。
        """
        import calendar

        moment = utcnow().replace(microsecond=0)
        assert to_epoch(moment) == calendar.timegm(moment.timetuple())
        assert from_epoch(to_epoch(moment)) == moment

    def test_exp_window_is_exactly_configured(self, tokens):
        import jwt as pyjwt

        pair = tokens.create_pair(user_id=1, username="a")
        claims = pyjwt.decode(pair.access_token, options={"verify_signature": False})
        assert claims["exp"] - claims["iat"] == 30 * 60
        assert claims["nbf"] <= claims["iat"] + 1

    def test_expired_token_rejected(self):
        import jwt as pyjwt

        quick = TokenService(secret="y" * 48, access_minutes=1)
        expired = pyjwt.encode(
            {
                "sub": "1",
                "username": "a",
                "type": TOKEN_TYPE_ACCESS,
                "jti": "j1",
                "iat": to_epoch(utcnow() - timedelta(minutes=20)),
                "nbf": to_epoch(utcnow() - timedelta(minutes=20)),
                "exp": to_epoch(utcnow() - timedelta(minutes=10)),
                "iss": "fashionops",
            },
            quick._secret,  # noqa: SLF001
            algorithm="HS256",
        )
        with pytest.raises(TokenError) as excinfo:
            quick.decode(expired, expected_type=TOKEN_TYPE_ACCESS)
        assert excinfo.value.reason == "expired"

    def test_refresh_token_cannot_be_used_as_access(self, tokens):
        """**refresh token 不能当 access token 用。**

        两者的签名、issuer、sub 完全一样。不做 type 检查的话，
        有效期 14 天的 refresh token 就能直接访问业务接口 ——
        "access 只有 30 分钟"这个设计意图被完全绕过。
        """
        pair = tokens.create_pair(user_id=1, username="a")
        with pytest.raises(TokenError) as excinfo:
            tokens.decode_access(pair.refresh_token)
        assert "wrong_type" in excinfo.value.reason

        with pytest.raises(TokenError):
            tokens.decode_refresh(pair.access_token)

    def test_tampered_token_rejected(self, tokens):
        pair = tokens.create_pair(user_id=1, username="a")
        head, payload, sig = pair.access_token.split(".")
        with pytest.raises(TokenError):
            tokens.decode_access(f"{head}.{payload}.{sig[:-4]}AAAA")

    def test_other_secret_rejected(self, tokens):
        other = TokenService(secret="z" * 48)
        pair = other.create_pair(user_id=1, username="a")
        with pytest.raises(TokenError):
            tokens.decode_access(pair.access_token)

    def test_wrong_issuer_rejected(self):
        """issuer 校验：防止别的系统（或另一个部署）签发的 token 被接受。"""
        other = TokenService(secret="w" * 48, issuer="another-system")
        pair = other.create_pair(user_id=1, username="a")
        with pytest.raises(TokenError) as excinfo:
            TokenService(secret="w" * 48, issuer="fashionops").decode_access(pair.access_token)
        assert excinfo.value.reason == "bad_issuer"

    def test_empty_secret_refused(self):
        """空密钥必须直接拒绝构造。

        否则 `jwt.encode(payload, "")` 会用空密钥签名，
        而任何人都能用空密钥签一个"合法"的 token。
        """
        with pytest.raises(ValueError, match="不能为空"):
            TokenService(secret="")

    def test_hash_token_is_deterministic_and_not_reversible(self):
        token = "a.b.c"
        assert hash_token(token) == hash_token(token)
        assert token not in hash_token(token)
        assert len(hash_token(token)) == 64


# ===========================================================================
# 3. 权限目录与同步
# ===========================================================================
class TestPermissionCatalogue:
    def test_codes_are_unique(self):
        codes = [p.code for p in PERMISSIONS]
        assert len(codes) == len(set(codes))

    def test_codes_use_resource_action_shape(self):
        for spec in PERMISSIONS:
            assert spec.code.count(":") == 1, f"{spec.code} 不是 resource:action 形状"
            resource, action = spec.code.split(":")
            assert resource and action

    def test_every_role_permission_exists(self):
        """**角色预设里写的权限码必须真的存在。**

        拼错一个字母的后果是"这个角色少了一个权限"，而且**不报任何错** ——
        直到有人点了那个按钮被 403，然后开始怀疑是代码问题。
        这条测试把它变成一次 CI 失败。
        """
        for preset in ROLE_PRESETS:
            unknown = preset.permissions - PERMISSION_CODES
            assert not unknown, f"角色 {preset.code} 引用了不存在的权限码：{sorted(unknown)}"

    def test_admin_role_has_everything(self):
        admin = next(p for p in ROLE_PRESETS if p.code == ROLE_ADMIN)
        assert admin.permissions == ALL_PERMISSIONS

    def test_operator_can_submit_but_not_approve(self):
        """**这条是 RBAC 在这个系统里存在的实际理由。**

        如果"能发起"和"能批准"是同一个人，人工闸口就退化成点一下就过的
        形式主义 —— 而它存在的全部意义就是拦住"模型自己决定发出去"。
        """
        operator = next(p for p in ROLE_PRESETS if p.code == ROLE_OPERATOR)
        assert "task:submit" in operator.permissions
        assert "task:approve" not in operator.permissions
        assert "mail:send" not in operator.permissions

    def test_viewer_cannot_submit(self):
        viewer = next(p for p in ROLE_PRESETS if p.code == ROLE_VIEWER)
        assert "task:submit" not in viewer.permissions
        assert "task:read" in viewer.permissions

    def test_all_role_codes_have_presets(self):
        assert {p.code for p in ROLE_PRESETS} == ROLE_CODES

    def test_sync_is_idempotent(self, db):
        """重复同步不能产生重复行。

        这个方法每次 `fashionops-db init` 和服务启动时都会跑，
        重复执行是**常态**而不是异常。
        """
        from sqlalchemy import func, select

        from fashionops.db.models_identity import Permission

        with Session(db) as session:
            before = session.scalar(select(func.count()).select_from(Permission))
            stats = sync_permissions(session)
            session.commit()
            after = session.scalar(select(func.count()).select_from(Permission))

        # fixture 已经同步过了，所以这次应该是零变更
        assert stats["created"] == 0
        assert stats["removed"] == 0
        assert before == after == len(PERMISSIONS)

    def test_sync_creates_when_empty(self, db):
        """从空表开始同步要真的建出全部权限码。

        （上面那条测的是"重复不重复建"，这条测的是"第一次能建"。
        两者缺一不可：只测后者时，一个"每次都重建"的实现也能通过。）
        """
        from sqlalchemy import delete, func, select

        from fashionops.db.models_identity import Permission, RolePermission

        with Session(db) as session:
            session.execute(delete(RolePermission))
            session.execute(delete(Permission))
            session.commit()

            stats = sync_permissions(session)
            session.commit()
            count = session.scalar(select(func.count()).select_from(Permission))

        assert stats["created"] == len(PERMISSIONS)
        assert count == len(PERMISSIONS)

    def test_sync_updates_display_fields(self, db):
        """权限的中文名改了要能同步过来（它是给人看的，代码是事实源）。"""
        from sqlalchemy import select

        from fashionops.db.models_identity import Permission

        with Session(db) as session:
            row = session.scalar(select(Permission).where(Permission.code == "task:read"))
            row.name = "旧名字"
            session.commit()

            stats = sync_permissions(session)
            session.commit()
            assert stats["updated"] >= 1
            session.refresh(row)
            assert row.name != "旧名字"

    def test_sync_removes_stale_codes(self, db):
        """代码里删掉的权限码要从库里删掉。

        不删的后果是"这个角色到底能干什么"变成不可回答 ——
        库里有一条权限，但没有任何接口在检查它。
        """
        from sqlalchemy import select

        from fashionops.db.models_identity import Permission

        with Session(db) as session:
            session.add(Permission(code="ghost:gone", name="已废弃", category="ghost"))
            session.commit()
            stats = sync_permissions(session)
            session.commit()
            assert stats["removed"] == 1
            found = session.scalar(select(Permission).where(Permission.code == "ghost:gone"))
            assert found is None

    def test_role_sync_does_not_overwrite_existing_grants(self, db):
        """**内置角色的默认授权只在"出生"时写一次。**

        每次同步都重写的话，运维在界面上给"运营主管"多加的一项权限
        会在下次发版时被悄悄抹掉 —— 然后没人再敢用界面改配置。
        """
        from sqlalchemy import select

        from fashionops.db.models_identity import Permission, Role, RolePermission

        with Session(db) as session:
            role = session.scalar(select(Role).where(Role.code == ROLE_VIEWER))
            perm = session.scalar(select(Permission).where(Permission.code == "task:submit"))
            session.add(RolePermission(role_id=role.id, permission_id=perm.id))
            session.commit()

            sync_roles(session)
            session.commit()

            session.refresh(role)
            assert "task:submit" in role.permission_codes(), "自定义授权被同步覆盖了"

    def test_user_permissions_are_union_of_roles(self, db, session):
        user = make_user(session, "zhang", roles=(ROLE_OPERATOR, ROLE_VIEWER))
        session.commit()
        perms = user_permission_codes(user)
        assert "task:submit" in perms  # 来自 operator
        assert "task:read" in perms  # 两者都有
        assert "task:approve" not in perms

    def test_superuser_gets_everything_without_roles(self, session):
        """超管绕过角色检查。

        为什么不用"给他挂 admin 角色"实现：初始化时**还没有任何角色**，
        两者耦合的话第一个管理员就创建不出来（先有鸡还是先有蛋）。
        """
        user = make_user(session, "root", roles=(), is_superuser=True)
        session.commit()
        assert user_permission_codes(user) == ALL_PERMISSIONS


# ===========================================================================
# 4. 登录流程（真实库）
# ===========================================================================
class TestLoginFlow:
    def test_login_success(self, session):
        make_user(session, "zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)
        user, pair = service.login(session, username="zhang", password=PASSWORD, tokens=tokens)
        assert user.username == "zhang"
        assert pair.expires_in > 0

    def test_username_is_case_insensitive(self, session):
        """MySQL 默认大小写不敏感，SQLite 敏感 —— 不显式处理会"本地能登、线上不能"。"""
        make_user(session, "Zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)
        user, _ = service.login(session, username="zhang", password=PASSWORD, tokens=tokens)
        assert user.username == "Zhang"

    def test_wrong_password_records_failure(self, session):
        user = make_user(session, "zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)
        with pytest.raises(service.AuthError):
            service.login(session, username="zhang", password="wrong", tokens=tokens)
        session.refresh(user)
        assert user.failed_login_count == 1

    def test_nonexistent_user_and_wrong_password_give_same_message(self, session):
        """**不能让人枚举出有效用户名。**

        "用户名不存在"和"密码错"给不同的话术，攻击者就能先筛出
        哪些用户名真实存在，再集中爆破密码 —— 成本降低一个数量级。
        """
        make_user(session, "zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)

        with pytest.raises(service.AuthError) as wrong_pw:
            service.login(session, username="zhang", password="wrong", tokens=tokens)
        with pytest.raises(service.AuthError) as no_user:
            service.login(session, username="nobody", password="wrong", tokens=tokens)

        assert str(wrong_pw.value) == str(no_user.value), "话术不一样就能被用来枚举用户名"
        assert wrong_pw.value.reason == no_user.value.reason == "invalid_credentials"

    def test_lockout_after_max_failures(self, session):
        user = make_user(session, "zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)

        for _ in range(5):
            with pytest.raises(service.AuthError):
                service.login(session, username="zhang", password="wrong", tokens=tokens, max_failures=5)

        session.refresh(user)
        assert user.status == USER_STATUS_LOCKED
        assert user.locked_until is not None

        # 即使密码对了也进不去 —— 这才是锁定的意义
        with pytest.raises(service.AuthError) as excinfo:
            service.login(session, username="zhang", password=PASSWORD, tokens=tokens)
        assert excinfo.value.reason == "locked"
        # 锁定的提示必须告诉用户"等多久"，否则他只会一直重试
        assert "分钟" in str(excinfo.value)

    def test_successful_login_resets_failure_count(self, session):
        user = make_user(session, "zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)
        for _ in range(2):
            with pytest.raises(service.AuthError):
                service.login(session, username="zhang", password="wrong", tokens=tokens)
        service.login(session, username="zhang", password=PASSWORD, tokens=tokens)
        session.refresh(user)
        assert user.failed_login_count == 0

    def test_locked_status_self_heals_after_lock_expires(self, session):
        """锁定期过了就自动解锁 —— 用惰性恢复而不是定时任务。

        定时任务是一个额外的失效点（它挂了用户就永远解锁不了），
        而"解锁"本来就不需要精确到秒。
        """
        user = make_user(session, "zhang")
        user.status = USER_STATUS_LOCKED
        user.locked_until = utcnow() - timedelta(minutes=1)
        session.commit()

        tokens = TokenService(secret="s" * 48)
        service.login(session, username="zhang", password=PASSWORD, tokens=tokens)
        session.refresh(user)
        assert user.status == USER_STATUS_ACTIVE
        assert user.locked_until is None

    def test_disabled_user_cannot_login(self, session):
        user = make_user(session, "zhang")
        user.status = USER_STATUS_DISABLED
        session.commit()
        tokens = TokenService(secret="s" * 48)
        with pytest.raises(service.AuthError) as excinfo:
            service.login(session, username="zhang", password=PASSWORD, tokens=tokens)
        # 停用要**如实告诉用户**去找管理员 —— 换成"用户名或密码不正确"
        # 的话，用户会一直试密码，而正确动作是联系管理员
        assert "停用" in str(excinfo.value)

    def test_refresh_token_stored_hashed(self, session):
        make_user(session, "zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)
        _, pair = service.login(session, username="zhang", password=PASSWORD, tokens=tokens)

        from sqlalchemy import select

        from fashionops.db.models_identity import RefreshToken

        rows = list(session.scalars(select(RefreshToken)))
        assert len(rows) == 1
        # **库里不能出现 token 原文**
        assert pair.refresh_token not in rows[0].token_hash
        assert rows[0].token_hash == hash_token(pair.refresh_token)

    def test_login_records_ip(self, session):
        make_user(session, "zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)
        user, _ = service.login(session, username="zhang", password=PASSWORD, tokens=tokens, ip="10.0.0.9")
        assert user.last_login_ip == "10.0.0.9"
        assert user.last_login_at is not None


class TestRefreshRotation:
    @pytest.fixture
    def logged_in(self, session):  # noqa: ANN001, ANN201
        make_user(session, "zhang")
        session.commit()
        tokens = TokenService(secret="s" * 48)
        user, pair = service.login(session, username="zhang", password=PASSWORD, tokens=tokens)
        return session, tokens, user, pair

    def test_refresh_returns_new_pair_and_revokes_old(self, logged_in):
        session, tokens, _, pair = logged_in
        _, new_pair = service.refresh(session, refresh_token=pair.refresh_token, tokens=tokens)
        assert new_pair.refresh_token != pair.refresh_token

        from sqlalchemy import select

        from fashionops.db.models_identity import RefreshToken

        old = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(pair.refresh_token)))
        assert old.revoked_at is not None
        assert old.replaced_by_id is not None, "没有指向新的那条，重放检测就做不了"

    def test_replay_detection_revokes_everything(self, logged_in):
        """**重放检测**：拿已经用过的 refresh token 再换一次。

        这不是"用户操作失误"（正常流程里旧 token 换完就作废了），
        而是 **token 被泄漏**的信号：攻击者拿着旧的来换，
        而合法用户那份已经换过新的了。

        正确动作是撤销整条链，因为攻击者手里那个仍然能继续试。
        只拒这一次的话，他还会一直试，而合法用户会莫名被踢。
        """
        session, tokens, user, pair = logged_in
        service.refresh(session, refresh_token=pair.refresh_token, tokens=tokens)

        with pytest.raises(service.AuthError) as excinfo:
            service.refresh(session, refresh_token=pair.refresh_token, tokens=tokens)
        assert excinfo.value.reason == "refresh_replay"

        assert service.list_sessions(session, user.id) == []

    def test_logout_revokes_only_that_session(self, logged_in):
        session, tokens, user, pair = logged_in
        _, second = service.login(session, username="zhang", password=PASSWORD, tokens=tokens)

        assert service.logout(session, refresh_token=pair.refresh_token) is True
        # 另一个设备的会话不受影响 —— 否则"退出当前设备"变成了"全部退出"
        assert len(service.list_sessions(session, user.id)) == 1
        service.refresh(session, refresh_token=second.refresh_token, tokens=tokens)

    def test_logout_is_idempotent(self, logged_in):
        """重复登出返回 False 而不是抛异常。

        前端的登出按钮可能被点两次，而第二次报错会让用户以为"没登出成功"。
        """
        session, _, _, pair = logged_in
        assert service.logout(session, refresh_token=pair.refresh_token) is True
        assert service.logout(session, refresh_token=pair.refresh_token) is False

    def test_changed_password_revokes_all_sessions(self, logged_in):
        """**改密码必须让所有旧登录失效。**

        改密码最常见的动机是"我怀疑密码泄漏了"。如果旧会话还有效，
        攻击者手上那个 refresh token 还能继续换 access token ——
        改密码就成了心理安慰。
        """
        session, tokens, user, _ = logged_in
        assert len(service.list_sessions(session, user.id)) >= 1
        service.set_password(session, user, "New-Password!234")
        assert service.list_sessions(session, user.id) == []

        with pytest.raises(service.AuthError):
            service.login(session, username="zhang", password=PASSWORD, tokens=tokens)
        service.login(session, username="zhang", password="New-Password!234", tokens=tokens)

    def test_token_from_other_secret_rejected_at_service_layer(self, logged_in):
        """签名不对的 refresh token 在 service 层就被挡下（不是靠查库）。"""
        session, _, _, _ = logged_in
        forged = TokenService(secret="q" * 48).create_pair(user_id=1, username="zhang")
        with pytest.raises(service.AuthError) as excinfo:
            service.refresh(session, refresh_token=forged.refresh_token, tokens=TokenService(secret="s" * 48))
        assert excinfo.value.reason.startswith("token:")


# ===========================================================================
# 5. 权限向下传导（防越权的核心）
# ===========================================================================
class TestEffectiveScopes:
    AGENT = frozenset({"erp:read", "ecom:read", "ad:read", "kb:read", "asset:write", "mail:send"})

    def test_intersection_blocks_escalation(self):
        """**整个 RBAC 里唯一真正防住越权的地方。**

        只有 `task:submit` 的实习生能让模型去发邮件吗？
        没有交集的话能 —— 因为 Agent 用的是服务身份。
        有交集之后，Agent 拿到的权限严格是提交人权限的子集。
        """
        intern = Principal(
            user_id=2,
            username="intern",
            permissions=frozenset({"task:read", "task:submit", "erp:read"}),
        )
        granted = service.effective_scopes(intern, self.AGENT)
        assert granted == frozenset({"erp:read"})
        assert "mail:send" not in granted
        assert "asset:write" not in granted

    def test_superuser_gets_full_agent_scopes(self):
        admin = Principal(user_id=1, username="admin", is_superuser=True)
        assert service.effective_scopes(admin, self.AGENT) == self.AGENT

    def test_user_without_any_permission_gets_nothing(self):
        newbie = Principal(user_id=3, username="newbie", permissions=frozenset())
        assert service.effective_scopes(newbie, self.AGENT) == frozenset()

    def test_nobody_gets_nothing(self):
        assert service.effective_scopes(NOBODY, self.AGENT) == frozenset()

    def test_principal_has_or_semantics(self):
        principal = Principal(user_id=1, username="a", permissions=frozenset({"x:y"}))
        assert principal.has("x:y") is True
        assert principal.has("x:y", "p:q") is True  # OR 语义
        assert principal.has_all("x:y") is True
        assert principal.has_all("x:y", "p:q") is False
        assert principal.has() is True  # 没要求就是通过


# ===========================================================================
# 6. HTTP 层
# ===========================================================================
@pytest.fixture
def client(db):  # noqa: ANN001, ANN201
    """带临时库的 TestClient。

    ## 为什么不覆盖 `get_db`

    初版用 `app.dependency_overrides[get_db]` 指向测试库 —— 这很常见，
    但它把 `get_db` **整个替掉**了，包括它的**事务边界**。
    于是测试里的行为和生产不一样：

    * 生产：`get_db` 成功后 commit → 登录落的 refresh token 能查到
    * 测试：覆盖版本只 close 不 commit → 刷新令牌**回滚** → 刷新接口 401

    这个差异本身体现了一个真 bug（参见 `db.base.get_db` 的 docstring），
    但同时也说明**测试的替身不应该自己重写被替对象的职责**。

    所以这里改成只替换**会话工厂**（`db` fixture 里 patch 了 `get_settings`，
    而 `get_db` / `session_scope` 都是通过它拿引擎的），
    真实的事务边界一行不改地跑。
    """
    from fashionops.api.main import app

    with TestClient(app) as c:
        yield c


class TestHttpAuth:
    def test_anonymous_blocked(self, client):
        resp = client.get("/api/v1/products")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate", "").startswith("Bearer")

    def test_health_is_public(self, client):
        """/health 不能要鉴权。

        它是给负载均衡/监控用的探针。要鉴权的话探针过不了，
        而运维会以为是服务挂了 —— 把一个认证问题伪装成可用性故障。
        """
        assert client.get("/health").status_code == 200

    def test_login_then_access(self, client, db):
        from conftest import create_admin

        username, password = create_admin(db)
        resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["is_superuser"] is True
        assert body["expires_in"] > 0

        headers = {"Authorization": f"Bearer {body['access_token']}"}
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
        assert client.get("/api/v1/products?page_size=1", headers=headers).status_code == 200

    def test_wrong_password_gives_401_not_500(self, client, db):
        from conftest import create_admin

        username, _ = create_admin(db)
        resp = client.post("/api/v1/auth/login", json={"username": username, "password": "definitely-wrong"})
        assert resp.status_code == 401

    def test_401_and_403_are_distinguished(self, client, db):
        """**401 和 403 必须分开。**

        前端靠它决定"跳登录页"还是"显示无权限提示"。混用的话，
        用户会看到"请重新登录"，登录完了还是不行 —— 完全不知道该怎么办。
        """
        from conftest import login

        with Session(db) as session:
            service.create_user(session, username="viewer", password=PASSWORD, roles=(ROLE_VIEWER,))
            session.commit()
        viewer_headers = login(client, "viewer", PASSWORD)

        # 能读
        assert client.get("/api/v1/products?page_size=1", headers=viewer_headers).status_code == 200
        # 不能提交任务 → 403（已登录但没权限）
        resp = client.post("/api/v1/tasks", json={"user_request": "出份报表"}, headers=viewer_headers)
        assert resp.status_code == 403, resp.text
        # 没带令牌 → 401
        assert client.post("/api/v1/tasks", json={"user_request": "出份报表"}).status_code == 401

    def test_bad_token_gives_401(self, client):
        resp = client.get("/api/v1/products", headers={"Authorization": "Bearer not-a-token"})
        assert resp.status_code == 401

    def test_non_bearer_scheme_treated_as_anonymous(self, client):
        """不是 Bearer 就当作没带凭据，不报"格式错"。

        报格式错会泄漏"这个接口要 Bearer 认证"，对未认证的探测者没必要给这个信息。
        """
        resp = client.get("/api/v1/products", headers={"Authorization": "Basic abc"})
        assert resp.status_code == 401

    def test_me_returns_permissions_for_ui(self, client, db):
        from conftest import create_admin, login

        username, password = create_admin(db)
        headers = login(client, username, password)
        body = client.get("/api/v1/auth/me", headers=headers).json()
        assert "task:approve" in body["permissions"]
        assert body["roles"] == [ROLE_ADMIN]

    def test_emails_need_mail_permission(self, client, db):
        """邮件记录含**收件人地址和正文**，门槛比商品列表高。

        用 viewer（有 erp:read 但没有 mail:send）验证：
        能看商品，但看不了邮件记录。这就是"权限粒度按数据敏感度定"的意思。
        """
        from conftest import login

        with Session(db) as session:
            service.create_user(session, username="viewer2", password=PASSWORD, roles=(ROLE_VIEWER,))
            session.commit()
        headers = login(client, "viewer2", PASSWORD)

        assert client.get("/api/v1/products?page_size=1", headers=headers).status_code == 200
        assert client.get("/api/v1/emails", headers=headers).status_code == 403

    def test_refresh_endpoint_rotates(self, client, db):
        from conftest import create_admin

        username, password = create_admin(db)
        first = client.post("/api/v1/auth/login", json={"username": username, "password": password}).json()
        resp = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["refresh_token"] != first["refresh_token"]

        # 旧的那个再用一次 → 重放检测，401
        again = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert again.status_code == 401

    def test_logout_makes_refresh_useless(self, client, db):
        from conftest import create_admin

        username, password = create_admin(db)
        tokens = client.post("/api/v1/auth/login", json={"username": username, "password": password}).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        resp = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers=headers,
        )
        assert resp.status_code == 200 and resp.json()["revoked"] is True

        after = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert after.status_code == 401

    def test_change_password_needs_correct_old_password(self, client, db):
        """改密码必须验旧密码。

        不验的话，拿到一个"已经登录"的 access token（比如公用电脑没退出）
        就能把密码改掉并撤销全部会话，把真正的主人永久锁在外面。
        """
        from conftest import create_admin, login

        username, password = create_admin(db)
        headers = login(client, username, password)

        wrong = client.post(
            "/api/v1/auth/change-password",
            json={"old_password": "not-the-old-one", "new_password": "Another-Pass!234"},
            headers=headers,
        )
        assert wrong.status_code == 400
        assert "原密码" in wrong.json()["detail"]

    def test_oauth_callback_is_501_not_fake_success(self, client):
        """扫码登录的回调**必须明确报 501**，不能假装成功。

        假装成功的话前端会以为流程走通了，拿着一份空的 token 去跳转 ——
        排查时完全看不出问题在哪。
        """
        resp = client.post("/api/v1/auth/oauth/dingtalk/callback?code=abc")
        assert resp.status_code == 501
        detail = resp.json()["detail"]
        assert "未实现" in detail or "未接入" in detail

    def test_oauth_providers_expose_implemented_false(self, client):
        body = client.get("/api/v1/auth/providers").json()
        names = {p["provider"] for p in body["providers"]}
        assert names == {"dingtalk", "wecom"}
        # 前端据此把按钮置灰并给提示，而不是让用户点了才发现
        assert all(p["implemented"] is False for p in body["providers"])

    def test_oauth_authorize_without_config_is_503(self, client):
        """没配置时要报 503 + 说清缺哪几项，而不是抛 500。"""
        resp = client.get("/api/v1/auth/oauth/wecom/authorize")
        assert resp.status_code == 503
        assert "FASHIONOPS_WECOM" in resp.json()["detail"]


class TestRouteCoverage:
    """遍历全部路由，要求受保护的路由都声明了权限。

    **这条测试的价值在于它管的是"以后"。**
    上面那些用例只能证明"现在这几个接口是安全的"，
    而新增接口时"忘了加鉴权"和"故意不加"在 diff 里长得一模一样 ——
    前者是漏洞。有了这条，漏掉的路由会在 CI 上直接变红。
    """

    #: 允许不需要权限的路由（前缀匹配）。**每一条都要有理由。**
    PUBLIC_PREFIXES = (
        "/health",  # 探针，见 test_health_is_public
        "/docs",  # FastAPI 自带
        "/redoc",
        "/openapi.json",
        "/api/v1/meta",  # 运行时元信息，不含业务数据也不用敏感配置
        "/api/v1/summary",  # 首页概览数字，本身无敏感内容
        "/api/v1/auth/login",  # 登录当然不能要求先登录
        "/api/v1/auth/refresh",  # 用 refresh token 换新令牌
        "/api/v1/auth/oauth",  # 扫码登录入口，同样不能要求先登录
    )

    #: 这些路由的鉴权在**函数体**里手工做（它们的主体来自另一个依赖）。
    #: 列出来而不是自动跳过：手工鉴权是例外，**例外必须显式登记** ——
    #: 否则"忘了加"会伪装成"这个是例外"。
    MANUALLY_GUARDED = (
        "/api/v1/tasks/{task_no}/stream",
        "/api/v1/auth/logout",
        "/api/v1/auth/logout-all",
        "/api/v1/auth/change-password",
        "/api/v1/auth/me",
        "/api/v1/auth/sessions",
        "/api/v1/auth/providers",
    )

    @staticmethod
    def _iter_routes() -> list[tuple[str, set[str], object]]:
        """递归取出**全部**路由，返回 `(path, methods, dependant)`。

        **为什么必须递归**：FastAPI 新版把 `include_router` 进来的路由
        包成 `_IncludedRouter` 对象放在 `app.routes` 里，
        **子路由不再被摊平**，藏在 `original_router.routes` 里。

        直接遍历 `app.routes` 取 `.path` 会**一条业务路由都拿不到** ——
        而扫描不到任何路由的"鉴权覆盖检查"会**静静地全绿**，
        给出一种"都检查过了"的错觉。所以下面专门有一条
        `test_scan_actually_finds_routes` 先证明扫描有效。
        """
        from fashionops.api.main import app

        out: list[tuple[str, set[str], object]] = []

        def walk(router) -> None:  # noqa: ANN001
            for route in getattr(router, "routes", []):
                path = getattr(route, "path", "")
                dependant = getattr(route, "dependant", None)
                if path:
                    # `dependant` 可能为空：`/docs` / `/redoc` 这些是普通的
                    # `starlette.routing.Route`，没有 FastAPI 的依赖树。
                    # 仍然要收进来 —— 否则「公开前缀清单对不上任何路由」这条
                    # 检查会因为扫不到它们而误报。
                    out.append((path, set(getattr(route, "methods", None) or ()), dependant))
                # 嵌套包装：三个名字都要试
                #   - `routes`          普通 APIRouter / Mount
                #   - `router`          旧版包装
                #   - `original_router` FastAPI 新版的 `_IncludedRouter`
                for attr in ("routes", "router", "original_router"):
                    nested = getattr(route, attr, None)
                    if nested is not None and hasattr(nested, "routes"):
                        walk(nested)
                        break

        walk(app)
        return out

    @classmethod
    def _iter_api_routes(cls) -> list[tuple[str, set[str], object]]:
        return [r for r in cls._iter_routes() if r[0].startswith("/api/")]

    @staticmethod
    def _declared_permissions(dependant) -> set[str]:  # noqa: ANN001
        """递归收集依赖树里 `require_permission` 打的标记。"""
        if dependant is None:
            return set()
        found: set[str] = set()

        def walk(node) -> None:  # noqa: ANN001
            for sub in node.dependencies:
                marks = getattr(sub.call, "__fashionops_permissions__", None)
                if marks:
                    found.update(marks)
                walk(sub)

        walk(dependant)
        return found

    def test_scan_actually_finds_routes(self):
        """先证明扫描逻辑本身有效。

        没有这条，一个"扫不到任何路由"的 bug 会让下面那条覆盖检查
        **假绿** —— 而假绿的覆盖检查比没有覆盖检查更危险，
        因为它会让人以为"都查过了"。
        """
        routes = self._iter_api_routes()
        assert len(routes) >= 20, f"只扫到 {len(routes)} 条 /api 路由，扫描逻辑有问题"
        paths = {p for p, _, _ in routes}
        assert "/api/v1/tasks/{task_no}/approve" in paths
        assert "/api/v1/auth/login" in paths

    def test_protected_routes_declare_permission(self):
        unprotected: list[str] = []
        for path, methods, dependant in self._iter_api_routes():
            if path.startswith(self.PUBLIC_PREFIXES) or path in self.MANUALLY_GUARDED:
                continue
            if not self._declared_permissions(dependant):
                unprotected.append(f"{','.join(sorted(methods))} {path}")

        assert not unprotected, (
            "以下路由既不在 PUBLIC_PREFIXES，也没有声明 require_permission：\n  "
            + "\n  ".join(sorted(unprotected))
            + "\n\n要么加权限，要么显式登记为公开路由（并写清为什么）。"
        )

    def test_every_declared_permission_code_exists(self):
        """路由上声明的权限码必须在 PERMISSIONS 里存在。

        `require_permission("task:aprove")`（拼错）不会报错，
        只会让**所有**人都过不了校验 —— 而报错信息是"需要权限 task:aprove"，
        看起来像用户没权限，而不是代码写错了。
        """
        declared: set[str] = set()
        for _path, _methods, dependant in self._iter_api_routes():
            declared |= self._declared_permissions(dependant)

        unknown = declared - PERMISSION_CODES
        assert not unknown, f"路由声明了不存在的权限码：{sorted(unknown)}"
        assert declared, "一个权限声明都没扫到，说明扫描逻辑坏了"

    def test_manually_guarded_list_has_no_stale_entries(self):
        """例外清单不能有失效项。

        路由改了名而清单没更新的话，那条路由会变成"被登记为手工鉴权、
        但其实没人鉴权" —— 一个纯粹的漏洞，而且看起来完全正常。
        """
        paths = {p for p, _, _ in self._iter_routes()}

        stale = [p for p in self.MANUALLY_GUARDED if p not in paths]
        assert not stale, f"MANUALLY_GUARDED 里有已不存在的路由：{stale}"

        # 公开前缀只需“能对上某个真实路径”
        stale_public = [p for p in self.PUBLIC_PREFIXES if not any(p in e for e in paths)]
        assert not stale_public, f"PUBLIC_PREFIXES 里有对不上任何路由的项：{stale_public}"
