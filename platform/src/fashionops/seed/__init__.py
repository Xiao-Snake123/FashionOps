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
"""

from __future__ import annotations

import argparse
import sys
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
    Ticket,
)
from fashionops.seed import generators as gen

# Windows 控制台默认 GBK，打印中文会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SEED = 20260911


def _print(msg: str = "") -> None:
    print(msg)


def _mask(url: str) -> str:
    return url if "@" not in url else url.split("://", 1)[0] + "://***@" + url.rsplit("@", 1)[-1]


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
            SKU(**{k: v for k, v in row.items() if k != "product_index"},
                product_id=product_ids[row["product_index"] - 1])
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
            Inventory(**{k: v for k, v in row.items() if k != "sku_index"},
                      sku_id=sku_ids[row["sku_index"] - 1])
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
        tasks = [Task(**row) for row in task_rows]
        session.add_all(tasks)
        session.flush()
        task_ids = [t.id for t in tasks]
        counts["tasks"] = len(tasks)

        events = [
            TaskEvent(**{k: v for k, v in row.items() if k != "task_index"},
                      task_id=task_ids[row["task_index"] - 1])
            for row in gen.make_task_events(rng, task_rows)
        ]
        session.add_all(events)
        counts["task_events"] = len(events)

        runs = [
            AgentRun(**{k: v for k, v in row.items() if k != "task_index"},
                     task_id=task_ids[row["task_index"] - 1])
            for row in gen.make_agent_runs(rng, task_rows)
        ]
        session.add_all(runs)
        counts["agent_runs"] = len(runs)
        counts["_runs_failed"] = sum(1 for r in runs if r.status != "success")

        # ------------------------------------------------------------------
        # 7. 素材 / 广告 / 邮件
        # ------------------------------------------------------------------
        assets = [
            Asset(**{k: v for k, v in row.items() if k not in ("task_index", "product_index")},
                  task_id=task_ids[row["task_index"] - 1],
                  product_id=product_ids[row["product_index"] - 1])
            for row in gen.make_assets(rng, task_rows, count=50)
        ]
        session.add_all(assets)
        counts["assets"] = len(assets)

        campaigns = [
            AdCampaign(**{k: v for k, v in row.items() if k not in ("task_index", "product_index")},
                       task_id=task_ids[row["task_index"] - 1],
                       product_id=product_ids[row["product_index"] - 1])
            for row in gen.make_ad_campaigns(rng, count=12)
        ]
        session.add_all(campaigns)
        counts["ad_campaigns"] = len(campaigns)
        counts["_ad_bad_roi"] = sum(1 for c in campaigns if (c.roi or Decimal(0)) < Decimal("1.0"))

        emails = [
            EmailMessage(**{k: v for k, v in row.items() if k != "task_index"},
                         task_id=task_ids[row["task_index"] - 1])
            for row in gen.make_email_messages(rng, count=30)
        ]
        session.add_all(emails)
        counts["email_messages"] = len(emails)
        counts["_email_blocked"] = sum(1 for e in emails if e.status == "blocked")

        # ------------------------------------------------------------------
        # 8. 审计日志
        # ------------------------------------------------------------------
        audits = [
            AuditLog(**{k: v for k, v in row.items() if k != "task_index"},
                     task_id=(task_ids[row["task_index"] - 1] if row["task_index"] else None))
            for row in gen.make_audit_logs(rng, count=300)
        ]
        session.add_all(audits)
        counts["audit_logs"] = len(audits)

        session.commit()

        # 再查一遍行数，确认真的落库了（而不是只在自己的 Session 里）
        for model in ALL_MODELS:
            counts[model.__tablename__] = session.scalar(
                select(func.count()).select_from(model)
            ) or 0

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
    _print()
    _print("  下一步：uv run fashionops-db info   # 核对行数")
    _print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fashionops-seed", description="生成并灌入服装电商假数据"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子，默认 %(default)s")
    parser.add_argument("--keep", action="store_true", help="保留已有数据（默认先清空重建）")
    args = parser.parse_args(argv)

    counts = seed(seed_value=args.seed, reset=not args.keep)
    report(counts, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
