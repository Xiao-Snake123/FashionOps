"""分级账号管理的规则测试。

## 为什么这个文件专门测"拒绝"

账号管理是这个系统里**唯一能改变其他人权限**的入口。它的正向功能
（建号、改名、重置密码）坏掉的后果是"用不了" —— 用户会立刻发现并报障。
而它的**越权路径**坏掉的后果是"用得太多"：部门主管悄悄拿到全公司账号的
管理权，或者给自己发一个管理员角色。后者没有任何症状，直到出事。

所以这里的用例绝大多数在断言 403 / 400，只有少数几条走通正向流程。

## 每个用例都在打一条具体的越权路径

| 用例 | 挡住的攻击 |
| --- | --- |
| 跨部门 | 主管 A 改 B 部门的人 |
| 权限高于自己 | 主管改同部门里管理员的账号 |
| 管理员碰超管 | 非超管停用超管，把系统的主人踢出去 |
| 授予超范围角色 | 主管给自己/别人发 admin 角色 |
| 操作自己 | 管理员停用自己（自锁）或绕过原密码改自己密码 |
| 没有部门 | "都没有部门"被当成"同一个部门"，导致无部门账号互相可见 |
"""
from __future__ import annotations

import pytest

PASSWORD = "T3st-Pa55word"

#: 用户名写成常量，夹具和用例引同一份。
#:
#: 初版是在夹具里返回一个 `{"super": 用户名}` 的字典，用例写
#: `_headers(client, "super")` —— 而那个键叫 "super"、用户名却叫
#: "root_admin"，于是登录全部失败。这类错误的表现是"一片 401/403"，
#: 看起来像权限逻辑坏了，实际只是名字对不上。**键和值只留一个。**
U_SUPER = "root_admin"
U_ADMIN = "global_admin"
U_LEAD_A = "lead_a"
U_LEAD_NONE = "lead_no_dept"
U_MEMBER_A = "member_a"
U_MEMBER_B = "member_b"
U_ADMIN_IN_A = "admin_in_a"


@pytest.fixture
def client(db):  # noqa: ANN001, ANN201
    """带临时库的 TestClient。和 `test_security.py` 里那个同一个思路。"""
    from starlette.testclient import TestClient

    from fashionops.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def world(db):  # noqa: ANN001, ANN201
    """一套最小组织：两个部门 + 超管 + 跨部门管理员 + 主管 + 两个部门的成员。

    特意造了 `admin_in_a`（管理员，但挂在 A 部门）：这是"部门边界"
    和"权限高低"两条规则的**交叉点** —— 主管 A 和他同部门，
    但权限比他高。只测其中一条规则的话，这个用例会漏掉。
    """
    from conftest import ensure_identity
    from sqlalchemy.orm import Session

    from fashionops.db.models_identity import Department
    from fashionops.security import service
    from fashionops.security.permissions import (
        ROLE_ADMIN,
        ROLE_DEPT_MANAGER,
        ROLE_VIEWER,
    )

    with Session(db) as session:
        ensure_identity(session)

        dept_a = Department(code="ops", name="运营部")
        dept_b = Department(code="svc", name="客服部")
        session.add_all([dept_a, dept_b])
        session.flush()

        def mk(
            username: str,
            roles: tuple[str, ...] = (),
            department_id: int | None = None,
            is_superuser: bool = False,
        ) -> int:
            result = service.create_user(
                session,
                username=username,
                password=PASSWORD,
                roles=roles,
                is_superuser=is_superuser,
                created_by="pytest",
            )
            result.user.department_id = department_id
            session.flush()
            return result.user.id

        ids = {
            U_SUPER: mk(U_SUPER, roles=(ROLE_ADMIN,), is_superuser=True),
            U_ADMIN: mk(U_ADMIN, roles=(ROLE_ADMIN,)),
            U_LEAD_A: mk(U_LEAD_A, roles=(ROLE_DEPT_MANAGER,), department_id=dept_a.id),
            U_LEAD_NONE: mk(U_LEAD_NONE, roles=(ROLE_DEPT_MANAGER,)),
            U_MEMBER_A: mk(U_MEMBER_A, roles=(ROLE_VIEWER,), department_id=dept_a.id),
            U_MEMBER_B: mk(U_MEMBER_B, roles=(ROLE_VIEWER,), department_id=dept_b.id),
            U_ADMIN_IN_A: mk(U_ADMIN_IN_A, roles=(ROLE_ADMIN,), department_id=dept_a.id),
        }
        dept_ids = {"a": dept_a.id, "b": dept_b.id}
        session.commit()

    # 把 engine 一起带出来：用例里要直接改库状态（模拟"账号被锁定"这类
    # 只能由登录失败累积出来的状态，走接口造不出来）
    return {"ids": ids, "dept": dept_ids, "engine": db}


def _headers(client, username: str) -> dict[str, str]:  # noqa: ANN001
    from conftest import login

    return login(client, username, PASSWORD)


def _usernames(payload: dict) -> set[str]:
    return {item["username"] for item in payload["items"]}


# ===========================================================================
# 部门边界
# ===========================================================================
class TestDepartmentBoundary:
    def test_lead_sees_only_own_department(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_LEAD_A)
        body = client.get("/api/v1/admin/accounts", headers=headers).json()

        assert body["scope"]["global"] is False
        assert body["scope"]["department_id"] == world["dept"]["a"]

        names = _usernames(body)
        assert U_MEMBER_A in names
        # 这三条是本用例的全部价值：别人的部门、超管、跨部门管理员都看不见
        assert U_MEMBER_B not in names
        assert U_SUPER not in names
        assert U_ADMIN not in names

    def test_lead_cannot_touch_other_department(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_LEAD_A)
        resp = client.patch(
            f"/api/v1/admin/accounts/{world['ids'][U_MEMBER_B]}",
            json={"full_name": "改名试试"},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_lead_cannot_create_in_other_department(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_LEAD_A)
        resp = client.post(
            "/api/v1/admin/accounts",
            json={"username": "sneaky", "department_id": world["dept"]["b"]},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_lead_create_lands_in_own_department(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_LEAD_A)
        resp = client.post(
            "/api/v1/admin/accounts",
            json={"username": "newbie", "role_codes": ["viewer"]},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["account"]["department_id"] == world["dept"]["a"]

    def test_lead_without_department_manages_nobody(self, client, world):  # noqa: ANN001
        """**"都没有部门"不是"同一个部门"。**

        把它当成相等会让所有无部门账号（超管、对接账号）互相可见，
        而"我还没被分配部门"恰恰是最常见的新账号状态。
        """
        headers = _headers(client, U_LEAD_NONE)
        body = client.get("/api/v1/admin/accounts", headers=headers).json()

        assert body["items"] == []
        assert body["stats"]["total"] == 0
        assert body["departments"] == []

        resp = client.patch(
            f"/api/v1/admin/accounts/{world['ids'][U_MEMBER_A]}",
            json={"full_name": "x"},
            headers=headers,
        )
        assert resp.status_code == 403


# ===========================================================================
# 权限高低
# ===========================================================================
class TestRankBoundary:
    def test_lead_cannot_manage_higher_privilege_peer(self, client, world):  # noqa: ANN001
        """同部门、但对方有 `admin:user` —— 主管不能碰。

        这条挡住的是最隐蔽的越权：先把比自己权限高的人降权或停用，
        再用他腾出来的位置做事。部门边界拦不住它（两个人在同一个部门）。
        """
        headers = _headers(client, U_LEAD_A)
        resp = client.patch(
            f"/api/v1/admin/accounts/{world['ids'][U_ADMIN_IN_A]}",
            json={"full_name": "改名试试"},
            headers=headers,
        )
        assert resp.status_code == 403
        assert "权限" in resp.json()["detail"]

    def test_admin_cannot_manage_superuser(self, client, world):  # noqa: ANN001
        """非超管的管理员**不能**停用超管 —— 否则他可以接管系统。"""
        headers = _headers(client, U_ADMIN)
        resp = client.patch(
            f"/api/v1/admin/accounts/{world['ids'][U_SUPER]}",
            json={"status": "disabled"},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_admin_cannot_reset_superuser_password(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_ADMIN)
        resp = client.post(
            f"/api/v1/admin/accounts/{world['ids'][U_SUPER]}/reset-password",
            json={},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_blocked_rows_report_why(self, client, world):  # noqa: ANN001
        """列表里要说明"为什么这个人不能管" —— 灰按钮不解释原因会被当成 bug。"""
        headers = _headers(client, U_LEAD_A)
        body = client.get("/api/v1/admin/accounts", headers=headers).json()

        by_name = {i["username"]: i for i in body["items"]}
        assert by_name[U_ADMIN_IN_A]["can_manage"] is False
        assert by_name[U_ADMIN_IN_A]["manage_blocked_reason"]
        assert by_name[U_MEMBER_A]["can_manage"] is True
        assert by_name[U_MEMBER_A]["manage_blocked_reason"] is None


# ===========================================================================
# 提权与自锁
# ===========================================================================
class TestEscalation:
    def test_cannot_grant_role_beyond_own_permissions(self, client, world):  # noqa: ANN001
        """主管不能给自己部门的人发 `admin` 角色 —— 发了分级授权就作废了。"""
        headers = _headers(client, U_LEAD_A)
        resp = client.patch(
            f"/api/v1/admin/accounts/{world['ids'][U_MEMBER_A]}",
            json={"role_codes": ["admin"]},
            headers=headers,
        )
        assert resp.status_code == 403
        assert "超出" in resp.json()["detail"]

    def test_assignable_roles_are_filtered(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_LEAD_A)
        body = client.get("/api/v1/admin/accounts", headers=headers).json()
        codes = {r["code"] for r in body["assignable_roles"]}

        # 比主管权限高的角色都不该出现在"可授予"列表里。
        # 出现了的话，界面会给一个点了必然 403 的下拉项
        assert "admin" not in codes
        assert "ops_manager" not in codes
        assert "viewer" in codes

    def test_cannot_disable_self(self, client, world):  # noqa: ANN001
        """自锁：管理员停用自己之后就没人能把他启用了。"""
        headers = _headers(client, U_ADMIN)
        resp = client.patch(
            f"/api/v1/admin/accounts/{world['ids'][U_ADMIN]}",
            json={"status": "disabled"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "自己" in resp.json()["detail"]

    def test_cannot_reset_own_password_via_admin_api(self, client, world):  # noqa: ANN001
        """改自己的密码必须走「修改密码」（要原密码）。

        管理接口这条路不校验原密码，放行等于"浏览器没锁屏时
        任何人坐过来就能改掉密码并接管账号"。
        """
        headers = _headers(client, U_LEAD_A)
        resp = client.post(
            f"/api/v1/admin/accounts/{world['ids'][U_LEAD_A]}/reset-password",
            json={},
            headers=headers,
        )
        assert resp.status_code == 400

    def test_self_row_is_not_manageable(self, client, world):  # noqa: ANN001
        """自己的那一行在界面上就该是只读的，而不是给一排按下去必报错的按钮。"""
        headers = _headers(client, U_LEAD_A)
        body = client.get("/api/v1/admin/accounts", headers=headers).json()
        me = next(i for i in body["items"] if i["username"] == U_LEAD_A)

        assert me["is_self"] is True
        assert me["can_manage"] is False
        assert me["manage_blocked_reason"]

    def test_non_admin_gets_403(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_MEMBER_A)
        assert client.get("/api/v1/admin/accounts", headers=headers).status_code == 403


# ===========================================================================
# 正向流程
# ===========================================================================
class TestHappyPath:
    def test_superuser_sees_everyone(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_SUPER)
        body = client.get("/api/v1/admin/accounts", headers=headers).json()

        assert body["scope"]["global"] is True
        assert _usernames(body) >= {U_MEMBER_A, U_MEMBER_B, U_SUPER, U_ADMIN}

    def test_superuser_reset_password_yields_working_password(self, client, world):  # noqa: ANN001
        headers = _headers(client, U_SUPER)
        resp = client.post(
            f"/api/v1/admin/accounts/{world['ids'][U_ADMIN]}/reset-password",
            json={},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        new_password = resp.json()["generated_password"]
        assert new_password

        # 旧密码立刻失效、新密码可用 —— 只断言"接口返回 200"是不够的，
        # 那不能证明密码真的被改了
        bad = client.post(
            "/api/v1/auth/login",
            json={"username": U_ADMIN, "password": PASSWORD},
        )
        assert bad.status_code == 401
        good = client.post(
            "/api/v1/auth/login",
            json={"username": U_ADMIN, "password": new_password},
        )
        assert good.status_code == 200

    def test_reset_revokes_existing_sessions(self, client, world):  # noqa: ANN001
        """重置密码必须**撤销该账号的全部登录**。

        不撤销的话，攻击者手里的 refresh token 照样能换新的 access token，
        重置密码就成了心理安慰 —— 而"怀疑账号被盗"恰恰是重置的最常见动机。
        """
        # 先拿到**重置之前**的那个 refresh token。顺序反了的话这个用例会假绿：
        # 重置之后旧密码登不进来，重新登录拿到的是**新**令牌，它本来就该有效
        before = client.post(
            "/api/v1/auth/login", json={"username": U_MEMBER_A, "password": PASSWORD}
        )
        assert before.status_code == 200
        stolen = before.json()["refresh_token"]

        admin_headers = _headers(client, U_SUPER)
        reset = client.post(
            f"/api/v1/admin/accounts/{world['ids'][U_MEMBER_A]}/reset-password",
            json={},
            headers=admin_headers,
        )
        assert reset.status_code == 200, reset.text

        reused = client.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
        assert reused.status_code == 401

    def test_create_account_without_roles_warns(self, client, world):  # noqa: ANN001
        """给空角色是允许的（"先建号、后授权"是合理流程），但要给出提示。"""
        headers = _headers(client, U_SUPER)
        body = client.post(
            "/api/v1/admin/accounts",
            json={"username": "no_role_yet"},
            headers=headers,
        ).json()

        assert body["generated_password"]
        assert body["account"]["roles"] == []
        assert any("角色" in w for w in body["warnings"])

    def test_reactivating_clears_lock(self, client, world):  # noqa: ANN001
        """启用一个锁定账号时要顺手清掉锁定期。

        只改状态不清锁定期会留下"状态 active 但 locked_until 还在未来"的账号，
        它下次登录仍然被拒 —— 表现是"管理员说解开了，但还是登不进"。
        """
        from sqlalchemy.orm import Session

        from fashionops.db.base import utcnow
        from fashionops.db.models_identity import User

        mark = utcnow().replace(year=utcnow().year + 1)
        with Session(world["engine"]) as session:
            user = session.get(User, world["ids"][U_MEMBER_A])
            user.status = "locked"
            user.failed_login_count = 5
            user.locked_until = mark
            session.commit()

        headers = _headers(client, U_SUPER)
        resp = client.patch(
            f"/api/v1/admin/accounts/{world['ids'][U_MEMBER_A]}",
            json={"status": "active"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        with Session(world["engine"]) as session:
            user = session.get(User, world["ids"][U_MEMBER_A])
            assert user.status == "active"
            assert user.locked_until is None
            assert user.failed_login_count == 0
