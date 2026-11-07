"""UI 优化那批新增写接口 / 新接口的端到端冒烟测试。

这些接口是前端这几页"能点"的底座：

- 广告投放：就地批准 / 驳回 / 一键止损 / 改预算
- 邮件闸门：策略读取 / 改白名单 / 重发
- 客服工单：派单 / 人工进度 / 阶段枚举 / 看板统计
- 登录安全审计：异常登录事件读取
- 看板指标：类目退货率（含行业基准带）

只验证"后端接住了请求并给出符合契约的响应"这一层 —— 闸门/限流/审计的具体分支
在各自的单测里已经密集覆盖，这里不重复。

用真实 TestClient + 真实登录（而非伪造 token），原因见 conftest 顶部说明：
权限同步、令牌校验这些最容易错的地方，伪造 token 会直接绿，等于没测。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import TEST_ADMIN_USERNAME, admin_headers_for
from sqlalchemy.orm import sessionmaker

from fashionops.db.models import AdCampaign, Ticket
from fashionops.db.models_identity import LoginEvent


@pytest.fixture
def client(db):  # noqa: ANN001, ANN201
    """带临时库的 TestClient（和 test_security.py 同一个思路）。"""
    from starlette.testclient import TestClient

    from fashionops.api.main import app

    with TestClient(app) as c:
        yield c


def _insert(db, obj) -> None:  # noqa: ANN001
    factory = sessionmaker(bind=db)
    with factory() as session:
        session.add(obj)
        session.commit()


def test_campaign_approve_then_pause(client, db):  # noqa: ANN001
    _insert(
        db,
        AdCampaign(
            campaign_no="AC20261001001",
            name="双11主推",
            budget_daily=Decimal("1000"),
            status="pending_approval",
        ),
    )
    headers = admin_headers_for(client, db)

    r = client.post("/api/v1/campaigns/AC20261001001/approve", headers=headers, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "running"
    assert body["approved_by"] is not None

    # 止损是唯一直达 paused、且不需要再走一轮审批的写操作
    r = client.post("/api/v1/campaigns/AC20261001001/pause", headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paused"


def test_campaign_reject_requires_reason(client, db):  # noqa: ANN001
    _insert(
        db,
        AdCampaign(
            campaign_no="AC20261001002",
            name="清仓",
            budget_daily=Decimal("50"),
            status="pending_approval",
        ),
    )
    headers = admin_headers_for(client, db)

    # 驳回必须写原因：申请人要靠它知道怎么改
    r = client.post("/api/v1/campaigns/AC20261001002/reject", headers=headers, json={})
    assert r.status_code == 400, r.text

    r = client.post(
        "/api/v1/campaigns/AC20261001002/reject",
        headers=headers,
        json={"reason": "预算超标，等月底再提"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


def test_campaign_change_budget(client, db):  # noqa: ANN001
    _insert(
        db,
        AdCampaign(
            campaign_no="AC20261001003",
            name="拉新",
            budget_daily=Decimal("10"),
            status="running",
        ),
    )
    headers = admin_headers_for(client, db)

    r = client.post(
        "/api/v1/campaigns/AC20261001003/budget",
        headers=headers,
        json={"budget_daily": 500},
    )
    assert r.status_code == 200, r.text
    # 金额是 Decimal，序列化后是字符串
    assert r.json()["budget_daily"] == "500"


def test_campaign_approve_wrong_state_is_409(client, db):  # noqa: ANN001
    """状态冲突用 409 而不是 400：让前端能区分"我传错参数"和"别人已处理"。"""
    _insert(
        db,
        AdCampaign(
            campaign_no="AC20261001004",
            name="已停",
            budget_daily=Decimal("10"),
            status="paused",
        ),
    )
    headers = admin_headers_for(client, db)
    r = client.post("/api/v1/campaigns/AC20261001004/approve", headers=headers, json={})
    assert r.status_code == 409, r.text


def test_mail_policy_roundtrip(client, db):  # noqa: ANN001
    headers = admin_headers_for(client, db)

    r = client.get("/api/v1/mail/policy", headers=headers)
    assert r.status_code == 200, r.text
    assert "overridable_keys" in r.json()

    # 改白名单应当即时生效（返回的新策略里 allowlist 已变）
    r = client.patch(
        "/api/v1/mail/policy",
        headers=headers,
        json={"mail_send_allowlist": "@corp.com,@partner.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["allowlist"] == ["@corp.com", "@partner.com"]

    # 清空运行时覆盖，回到 .env
    r = client.delete("/api/v1/mail/policy/overrides", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_ticket_assign_and_progress(client, db):  # noqa: ANN001
    _insert(
        db,
        Ticket(
            ticket_no="TK20261001001",
            channel="phone",
            subject="尺码咨询",
            content="客户问 M 码对应多少斤",
        ),
    )
    headers = admin_headers_for(client, db)

    r = client.post(
        "/api/v1/service/tickets/TK20261001001/assign",
        headers=headers,
        json={"assignee": "alice"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "alice"

    r = client.post(
        "/api/v1/service/tickets/TK20261001001/progress",
        headers=headers,
        json={"human_status": "contacted", "note": "已电话答复"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["human_status"] == "contacted"
    assert r.json()["human_note"] == "已电话答复"

    # 阶段枚举由后端给，前端不写死
    r = client.get("/api/v1/service/tickets/statuses", headers=headers)
    assert r.status_code == 200, r.text
    values = {s["value"] for s in r.json()["human_statuses"]}
    assert "contacted" in values


def test_login_events_visible_to_admin(client, db):  # noqa: ANN001
    # 第一次从办公室 IP 成功，第二次从新 IP 成功 → 第二次应被标 new_ip
    _insert(
        db,
        LoginEvent(username="bob", success=True, ip="1.2.3.4", auth_method="password"),
    )
    _insert(
        db,
        LoginEvent(username="bob", success=True, ip="9.9.9.9", auth_method="password"),
    )
    # 撞库探测：尝试一个不存在的账号也要记
    _insert(
        db,
        LoginEvent(username="ghost", success=False, reason="unknown_user", auth_method="password", ip="9.9.9.9"),
    )
    headers = admin_headers_for(client, db)

    r = client.get("/api/v1/admin/security/logins", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 3
    # 异常判定只看 IP 是否变化，不做 IP 归属地
    assert any(e["anomalies"] for e in body["items"])


def test_return_rate_by_category_contract(client, db):  # noqa: ANN001
    headers = admin_headers_for(client, db)
    r = client.get("/api/v1/metrics/return-rate-by-category?days=30", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # 基准区间由后端给，前端不另存一份
    assert "baseline" in body and "low" in body["baseline"]
    assert isinstance(body["items"], list)


def test_campaigns_only_losing_filter(client, db):  # noqa: ANN001
    _insert(
        db,
        AdCampaign(campaign_no="AC-LOSE", name="亏损计划", budget_daily=Decimal("10"), roi=Decimal("0.5")),
    )
    _insert(
        db,
        AdCampaign(campaign_no="AC-WIN", name="赚钱计划", budget_daily=Decimal("10"), roi=Decimal("3.2")),
    )
    headers = admin_headers_for(client, db)
    r = client.get("/api/v1/campaigns?only_losing=true", headers=headers)
    assert r.status_code == 200, r.text
    nos = {c["campaign_no"] for c in r.json()["items"]}
    assert nos == {"AC-LOSE"}


def test_tickets_mine_and_unassigned(client, db):  # noqa: ANN001
    _insert(db, Ticket(ticket_no="TK1", channel="phone", subject="s1", content="c1"))
    _insert(
        db,
        Ticket(ticket_no="TK2", channel="phone", subject="s2", content="c2", assignee=TEST_ADMIN_USERNAME),
    )
    _insert(db, Ticket(ticket_no="TK3", channel="phone", subject="s3", content="c3", assignee="alice"))
    headers = admin_headers_for(client, db)

    # only_mine 用**当前登录名**过滤，不接受前端传用户名
    r = client.get("/api/v1/tickets?only_mine=true", headers=headers)
    assert r.status_code == 200, r.text
    assert {t["ticket_no"] for t in r.json()["items"]} == {"TK2"}

    r = client.get("/api/v1/tickets?only_unassigned=true", headers=headers)
    assert {t["ticket_no"] for t in r.json()["items"]} == {"TK1"}


def test_ticket_board_stats(client, db):  # noqa: ANN001
    _insert(db, Ticket(ticket_no="TK-A", channel="phone", subject="a", content="ca"))
    _insert(
        db,
        Ticket(
            ticket_no="TK-B",
            channel="phone",
            subject="b",
            content="cb",
            assignee="alice",
            human_status="contacted",
        ),
    )
    headers = admin_headers_for(client, db)
    r = client.get("/api/v1/tickets/stats", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert body["unassigned"] == 1
    # 看 agent / human 两维，光看 status 分不出"有没有人在跟"
    assert body["by_assignee"].get("alice") == 1
    assert body["by_human_status"].get("contacted") == 1
