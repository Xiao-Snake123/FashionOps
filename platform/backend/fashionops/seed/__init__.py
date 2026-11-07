"""假数据灌库入口。

用法::

    uv run fashionops-seed              # 清空并重新灌（默认）
    uv run fashionops-seed --keep       # 保留已有数据，追加灌入
    uv run fashionops-seed --seed 42    # 换一个随机种子，生成不同的数据集

设计要点：

1. **可复现**：固定默认种子，同一份种子永远生成同一份数据 ——
   评测集要拿它做基线，不能每次跑出来的数据都不一样。
2. **异常必埋**：详见 ``generators`` 模块顶部的说明。
3. **按依赖顺序插入**：父表先插并 ``flush()`` 拿到自增主键，
   再用它还原子表的外键。生成器里用的是"第几个"这种逻辑序号，
   不掺和数据库主键，这样生成器可以脱离数据库单测。

## 一个必须知道的坑：`--keep=False` 会连账号一起删

``reset=True`` 走的是 ``Base.metadata.drop_all()``，**身份表（users / roles /
permissions）也在 metadata 里**，所以灌完库是登不进去的。
早期这个坑表现为"灌完数据后登录一直 401"。

所以这里在重建之后会**立刻把身份体系补回来**（见 `_bootstrap_identity`）：
同步权限码与内置角色 → 建演示账号。这样"灌完就能登进去看"这条路径是通的。
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from decimal import Decimal

from faker import Faker
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fashionops.config import get_settings
from fashionops.db.base import Base, get_engine, reset_engine
from fashionops.db.models import (
    ALL_MODELS,
    SKU,
    AdCampaign,
    AgentRun,
    Asset,
    AuditLog,
    BIMetric,
    Competitor,
    EmailMessage,
    Inventory,
    KBArticle,
    Order,
    Product,
    Task,
    TaskEvent,
    TaskTodo,
    Ticket,
)
from fashionops.db.models_chat import Approval, ChatMessage, Conversation
from fashionops.db.models_identity import User
from fashionops.seed import generators as gen

# Windows 控制台默认 GBK，打印中文会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SEED = 20260911


def _print(msg: str = "") -> None:
    print(msg)


def _mask(url: str) -> str:
    return url if "@" not in url else url.split("://", 1)[0] + "://***@" + url.rsplit("@", 1)[-1]


#: 灌库时建立的部门。
#:
#: 和迁移 `d3e4f5a6b7c8` 里的种子**必须保持一致**：迁移负责"升级已有库"，
#: seed 负责"重建演示库"（它会 `drop_all`，迁移插进去的行会一起没掉）。
#: 只写一处的话，会出现"升级上来的库有部门、重新灌的库一个都没有"这种
#: 只在别人的机器上复现的环境差异。
DEMO_DEPARTMENTS: tuple[tuple[str, str, str], ...] = (
    ("ops", "运营部", "商品运营、活动与日常经营分析"),
    ("service", "客服部", "售前售后工单与知识库维护"),
    ("marketing", "市场部", "广告投放与增长"),
    ("product", "商品部", "选品、供应链与库存"),
    ("tech", "技术部", "系统维护与数据对接"),
)


#: 灌库后自动建好的演示账号。**密码写在文档里是刻意的** ——
#: 这是本地演示库；真上生产要用 `fashionops-user` 改掉，
#: 而"默认账号不存在"会让第一次打开项目的人卡在登录页。
#:
#: 最后一项是**部门 code**（对应 `departments.code`，None 表示不归属部门）。
#: 演示账号不落到部门上的话，打开"账号管理"看到的全是"未分配"，
#: 完全看不出分级授权在做什么 —— 而那是这个页面唯一要展示的东西。
DEMO_USERS: tuple[tuple[str, str, str, tuple[str, ...], bool, str | None], ...] = (
    ("demo", "FashionOps@2026", "演示管理员", ("admin",), True, None),
    ("ops_manager", "FashionOps@2026", "运营主管", ("ops_manager",), False, "ops"),
    ("operator", "FashionOps@2026", "运营专员", ("operator",), False, "ops"),
    # 两个部门主管：用它们可以直接对比出"部门主管只看得到本部门"这件事
    ("dept_lead", "FashionOps@2026", "运营部主管", ("dept_manager",), False, "ops"),
    ("svc_lead", "FashionOps@2026", "客服部主管", ("dept_manager",), False, "service"),
)


def _bootstrap_identity(session: Session) -> dict[str, int]:
    """重建身份体系（权限码 / 内置角色 / 演示账号）。

    为什么必须做：`seed(reset=True)` 会 `drop_all`，账号和角色一起没了。
    不做这一步，灌完库的第一件事就是"登录 401"，
    而这个现象看起来像鉴权坏了，排查方向会整个跑偏。
    """
    from fashionops.db.models_identity import Department, User
    from fashionops.security.permissions import sync_identity
    from fashionops.security.service import create_user

    sync_identity(session)
    session.flush()

    # 部门先建：演示账号要挂上去，而账号管理是按部门分级的
    dept_by_code: dict[str, Department] = {}
    for code, name, description in DEMO_DEPARTMENTS:
        row = session.scalar(select(Department).where(Department.code == code))
        if row is None:
            row = Department(code=code, name=name, description=description)
            session.add(row)
            session.flush()
        dept_by_code[code] = row

    created = 0
    for username, password, full_name, roles, is_superuser, dept_code in DEMO_USERS:
        exists = session.scalar(select(User).where(User.username == username))
        department_id = dept_by_code[dept_code].id if dept_code else None
        if exists is not None:
            # 账号已存在时**只补部门**，不动密码和角色 ——
            # 重新灌库不该把别人改过的密码覆盖回去
            if exists.department_id is None and department_id is not None:
                exists.department_id = department_id
            continue
        result = create_user(
            session,
            username=username,
            password=password,
            full_name=full_name,
            roles=roles,
            is_superuser=is_superuser,
            created_by="seed",
        )
        result.user.department_id = department_id
        created += 1
    session.flush()
    return {"users": created, "departments": len(dept_by_code)}


def seed(seed_value: int = DEFAULT_SEED, reset: bool = True) -> dict[str, int]:
    """执行灌库，返回各表行数。"""
    import random

    settings = get_settings()
    reset_engine()
    engine = get_engine(settings)

    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    rng = random.Random(seed_value)
    fake = Faker("zh_CN")
    Faker.seed(seed_value)  # Faker 自带的随机源也要固定，否则它生成的昵称每次不同

    counts: dict[str, int] = {}

    with Session(engine) as session:
        # ------------------------------------------------------------------
        # 0. 身份体系（drop_all 会把它一起删掉，必须先补回来）
        # ------------------------------------------------------------------
        counts.update(_bootstrap_identity(session))
        counts["_users_created"] = counts.pop("users", 0)

        # ------------------------------------------------------------------
        # 1. 商品 + SKU
        # ------------------------------------------------------------------
        product_rows = gen.make_products(rng, count=60)
        products = [Product(**row) for row in product_rows]
        session.add_all(products)
        session.flush()  # 拿自增主键
        product_ids = [p.id for p in products]
        counts["products"] = len(products)

        sku_rows = gen.make_skus(rng, product_rows)
        skus = [
            SKU(
                **{k: v for k, v in row.items() if k != "product_index"},
                product_id=product_ids[row["product_index"] - 1],
            )
            for row in sku_rows
        ]
        session.add_all(skus)
        session.flush()
        sku_ids = [s.id for s in skus]
        counts["skus"] = len(skus)

        # 给订单用的 SKU 元数据（含所属商品标题与类目）
        sku_meta = [
            {
                "sku_index": i + 1,
                "sale_price": s.sale_price,
                "product_title": products[row["product_index"] - 1].title,
                "category": products[row["product_index"] - 1].category,
            }
            for i, (s, row) in enumerate(zip(skus, sku_rows, strict=True))
        ]

        # ------------------------------------------------------------------
        # 2. 竞品
        # ------------------------------------------------------------------
        competitors = [Competitor(**row) for row in gen.make_competitors(rng, count=80)]
        session.add_all(competitors)
        counts["competitors"] = len(competitors)

        # ------------------------------------------------------------------
        # 3. 库存流水
        # ------------------------------------------------------------------
        inv_rows, final_stock = gen.make_inventory(rng, sku_count=len(skus))
        inventories = [
            Inventory(**{k: v for k, v in row.items() if k != "sku_index"}, sku_id=sku_ids[row["sku_index"] - 1])
            for row in inv_rows
        ]
        session.add_all(inventories)
        counts["inventory"] = len(inventories)
        zero_stock = sum(1 for v in final_stock.values() if v <= 5)
        counts["_sku_stockout"] = zero_stock

        # ------------------------------------------------------------------
        # 4. 订单 → BI
        # ------------------------------------------------------------------
        order_rows = gen.make_orders(rng, sku_meta, days=60, count=800)
        orders = [
            # sku_index 是逻辑外键，_category 是聚合用的中间字段，两者都不能进模型
            Order(
                **{k: v for k, v in row.items() if k != "sku_index" and not k.startswith("_")},
                sku_id=sku_ids[row["sku_index"] - 1],
            )
            for row in order_rows
        ]
        session.add_all(orders)
        counts["orders"] = len(orders)

        bi_rows = gen.make_bi_metrics(rng, order_rows, days=60)
        metrics = [BIMetric(**row) for row in bi_rows]
        session.add_all(metrics)
        counts["bi_metrics"] = len(metrics)

        # ------------------------------------------------------------------
        # 5. 知识库 + 工单
        # ------------------------------------------------------------------
        kb = [KBArticle(**row) for row in gen.make_kb_articles()]
        session.add_all(kb)
        counts["kb_articles"] = len(kb)

        order_nos = [o.order_no for o in orders]
        tickets = [Ticket(**row) for row in gen.make_tickets(rng, order_nos, count=60)]
        session.add_all(tickets)
        counts["tickets"] = len(tickets)
        counts["_tickets_escalated"] = sum(1 for t in tickets if t.escalated)

        # ------------------------------------------------------------------
        # 6. 任务 → 事件 / Agent 运行
        # ------------------------------------------------------------------
        task_rows = gen.make_tasks(rng, count=20)
        # 生成器里下划线开头的是给别的生成器用的中间量（_plan / _stop_at），
        # 不是模型字段 —— 统一在这里过滤掉
        tasks = [Task(**{k: v for k, v in row.items() if not k.startswith("_")}) for row in task_rows]
        session.add_all(tasks)
        session.flush()
        task_ids = [t.id for t in tasks]
        counts["tasks"] = len(tasks)

        # ---- 6.1 新架构：每一步一条 todo ----
        todos = [
            TaskTodo(**{k: v for k, v in row.items() if k != "task_index"}, task_id=task_ids[row["task_index"] - 1])
            for row in gen.make_task_todos(rng, task_rows)
        ]
        session.add_all(todos)
        counts["task_todos"] = len(todos)
        counts["_todos_waiting"] = sum(1 for t in todos if t.status == "waiting_approval")

        # ---- 6.2 停在闸口的任务：补一张对应的待确认审批单 ----
        # 只有 tasks.approval_payload 而没有审批单行的话，"哪一步在等、
        # 等人批的是哪次调用"这两个问题答不上来 —— 那正是审批表存在的理由。
        todo_by_key = {(t.task_id, t.seq): t for t in todos}
        approvals: list[Approval] = []
        for index, row in enumerate(task_rows, start=1):
            if row["status"] != "waiting_approval":
                continue
            payload = row["approval_payload"] or {}
            approval_no = f"AP{row['created_at'].strftime('%Y%m%d')}{rng.randrange(10**5, 10**6):06d}"
            approvals.append(
                Approval(
                    approval_no=approval_no,
                    task_no=row["task_no"],
                    todo_seq=row["_stop_at"],
                    tool_call_id=f"call_{row['task_no'][-6:]}",
                    tool_name="mail.send",
                    tool_arguments={
                        "to_addrs": ["ops@fashionops.com"],
                        "subject": "【经营日报】近 30 天数据与退货率告警",
                        "body_text": "近 30 天 GMV ¥130,226.43，订单 322 单，直播渠道退货率 40.0%。",
                    },
                    preview=payload.get("preview"),
                    reason=payload.get("reason"),
                    status="pending",
                    created_at=row["created_at"] + timedelta(minutes=rng.randrange(2, 30)),
                )
            )
            todo = todo_by_key.get((task_ids[index - 1], row["_stop_at"]))
            if todo is not None:
                todo.approval_no = approval_no
        if approvals:
            session.add_all(approvals)
            session.flush()
            counts["chat_approvals"] = len(approvals)

        events = [
            TaskEvent(**{k: v for k, v in row.items() if k != "task_index"}, task_id=task_ids[row["task_index"] - 1])
            for row in gen.make_task_events(rng, task_rows)
        ]
        session.add_all(events)
        counts["task_events"] = len(events)

        runs = [
            AgentRun(**{k: v for k, v in row.items() if k != "task_index"}, task_id=task_ids[row["task_index"] - 1])
            for row in gen.make_agent_runs(rng, task_rows)
        ]
        session.add_all(runs)
        counts["agent_runs"] = len(runs)
        counts["_runs_failed"] = sum(1 for r in runs if r.status != "success")

        # ------------------------------------------------------------------
        # 7. 素材 / 广告 / 邮件
        # ------------------------------------------------------------------
        assets = [
            Asset(
                **{k: v for k, v in row.items() if k not in ("task_index", "product_index")},
                task_id=task_ids[row["task_index"] - 1],
                product_id=product_ids[row["product_index"] - 1],
            )
            for row in gen.make_assets(rng, task_rows, count=50)
        ]
        session.add_all(assets)
        counts["assets"] = len(assets)

        campaigns = [
            AdCampaign(
                **{k: v for k, v in row.items() if k not in ("task_index", "product_index")},
                task_id=task_ids[row["task_index"] - 1],
                product_id=product_ids[row["product_index"] - 1],
            )
            for row in gen.make_ad_campaigns(rng, count=12)
        ]
        session.add_all(campaigns)
        counts["ad_campaigns"] = len(campaigns)
        counts["_ad_bad_roi"] = sum(1 for c in campaigns if (c.roi or Decimal(0)) < Decimal("1.0"))

        emails = [
            EmailMessage(**{k: v for k, v in row.items() if k != "task_index"}, task_id=task_ids[row["task_index"] - 1])
            for row in gen.make_email_messages(rng, count=30)
        ]
        session.add_all(emails)
        counts["email_messages"] = len(emails)
        counts["_email_blocked"] = sum(1 for e in emails if e.status == "blocked")

        # ------------------------------------------------------------------
        # 8. 审计日志
        # ------------------------------------------------------------------
        audits = [
            AuditLog(
                **{k: v for k, v in row.items() if k != "task_index"},
                task_id=(task_ids[row["task_index"] - 1] if row["task_index"] else None),
            )
            for row in gen.make_audit_logs(rng, count=300)
        ]
        session.add_all(audits)
        counts["audit_logs"] = len(audits)

        # ------------------------------------------------------------------
        # 9. 对话演示数据（三条，覆盖"直接答 / 建任务 / 停在闸口"三种结局）
        # ------------------------------------------------------------------
        demo_user = session.scalar(select(User).where(User.username == "demo"))
        chat = gen.make_chat_demo(rng)
        pending_approvals = 0
        for conv in chat["conversations"]:
            messages = conv.pop("messages")
            pending = conv.pop("pending_approval")
            conversation = Conversation(**conv, user_id=demo_user.id if demo_user else None)
            session.add(conversation)
            session.flush()

            approval_message_id = None
            if pending:
                approval_message_id = messages[-1]["seq"]
            for msg in messages:
                session.add(ChatMessage(**msg, conversation_id=conversation.id))
            session.flush()

            if pending:
                msg_row = session.scalar(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conversation.id,
                        ChatMessage.seq == approval_message_id,
                    )
                )
                session.add(
                    Approval(
                        approval_no=f"AP{conversation.created_at.strftime('%Y%m%d')}{rng.randrange(10**5, 10**6):06d}",
                        conversation_id=conversation.id,
                        message_id=msg_row.id if msg_row else None,
                        tool_call_id=f"call_{conversation.conversation_no[-4:]}",
                        tool_name=pending["tool_name"],
                        tool_arguments={
                            "to_addrs": pending["to_addrs"],
                            "cc_addrs": pending["cc_addrs"],
                            "subject": pending["subject"],
                            "body_text": pending["body_text"],
                        },
                        preview=(
                            f"收件人：{'、'.join(pending['to_addrs'])}\n"
                            f"抄送：{'、'.join(pending['cc_addrs'])}\n"
                            f"主题：{pending['subject']}\n"
                            f"正文（{len(pending['body_text'])} 字）：{pending['body_text'][:60]}…"
                        ),
                        reason="邮件发出去就收不回来，需要人工确认",
                        status="pending",
                        created_at=conversation.updated_at,
                    )
                )
                pending_approvals += 1
            session.flush()

        counts["_chat_pending_approval"] = pending_approvals

        session.commit()

        # 再查一遍行数，确认真的落库了（而不是只在自己的 Session 里）
        for model in ALL_MODELS:
            counts[model.__tablename__] = session.scalar(select(func.count()).select_from(model)) or 0

    # fake 只是为了让 linter 知道它被用了；Faker 用于生成更自然的昵称/地址
    _ = fake
    return counts


def report(counts: dict[str, int], seed_value: int) -> None:
    settings = get_settings()
    _print()
    _print("=" * 66)
    _print("  假数据灌库完成")
    _print("=" * 66)
    _print(f"  目标库   : {_mask(settings.sqlalchemy_url)}")
    _print(f"  随机种子 : {seed_value}（换种子会生成不同数据集）")
    _print("-" * 66)

    for model in ALL_MODELS:
        name = model.__tablename__
        _print(f"  {name:<18} {counts.get(name, 0):>7} 行")

    _print("-" * 66)
    _print("  刻意埋入的异常（演示熔断 / 转人工 / 硬约束要用）：")
    _print(f"    库存告急 SKU        : {counts.get('_sku_stockout', 0)} 个（≤5 件）")
    _print(f"    亏损广告计划        : {counts.get('_ad_bad_roi', 0)} 个（ROI < 1.0）")
    _print(f"    转人工工单          : {counts.get('_tickets_escalated', 0)} 个")
    _print(f"    Agent 失败/超时运行 : {counts.get('_runs_failed', 0)} 次")
    _print(f"    被闸门拦下的邮件    : {counts.get('_email_blocked', 0)} 封")
    _print("-" * 66)
    _print("  新架构的演示数据（任务详情页 / 审批卡片要用）：")
    _print(f"    待办步骤 task_todos : {counts.get('task_todos', 0)} 行")
    _print(f"    卡在闸口的步骤      : {counts.get('_todos_waiting', 0)} 个")
    _print(f"    对话里待确认的卡片  : {counts.get('_chat_pending_approval', 0)} 张")
    _print()
    _print(f"  演示账号：demo / FashionOps@2026（本次新建 {counts.get('_users_created', 0)} 个账号）")
    _print("  下一步：uv run fashionops-db info   # 核对行数")
    _print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fashionops-seed", description="生成并灌入服装电商假数据")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子，默认 %(default)s")
    parser.add_argument("--keep", action="store_true", help="保留已有数据（默认先清空重建）")
    args = parser.parse_args(argv)

    counts = seed(seed_value=args.seed, reset=not args.keep)
    report(counts, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
