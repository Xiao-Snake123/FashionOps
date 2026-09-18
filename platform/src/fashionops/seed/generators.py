"""假数据生成器。

## 为什么强调"必须有波澜"

如果所有假数据都是正态分布的漂亮数字，那么下面这些东西**永远演示不出来**：

* 选品的库存硬约束 —— 每个款库存都充足，约束就形同虚设
* 广告的自动停投熔断 —— ROI 一直平稳，规则引擎没有触发机会
* 客服转人工 —— 客户情绪都是 neutral，转人工分支是死代码
* 错误归因与评测 —— 没有失败样本，"成功率"这个指标就没有分母

所以这里**刻意埋了四类异常**：库存告急 / ROI 暴跌 / 纠纷工单 / 失败运行。

## 关于可复现

全部走固定种子的 ``random.Random``，同一份种子永远生成同一份数据。
评测集要用它做基线，不能每次跑出来的数据都不一样。
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from decimal import Decimal

# ===========================================================================
# 异常注入比例 —— 改这里就能调"业务有多惨"
# ===========================================================================
STOCKOUT_RATIO = 0.15  # 15% 的 SKU 库存告急（选品 Agent 必须过滤掉它们）
BAD_ROI_RATIO = 0.25  # 25% 的广告计划最近 ROI 暴跌（触发自动停投）
ANGRY_TICKET_RATIO = 0.20  # 20% 工单是情绪激动的纠纷（触发转人工）
FAILED_RUN_RATIO = 0.12  # 12% 的 Agent 运行是失败/超时（评测要有分母）
HIGH_RETURN_CATEGORY = "连衣裙"  # 这个类目退货率人为拉高（服装行业真实情况）

# ===========================================================================
# 业务词表
# ===========================================================================
CATEGORIES = {
    "女装": ["连衣裙", "针织衫", "风衣", "半身裙", "西装外套", "卫衣", "大衣", "衬衫", "阔腿裤"],
    "男装": ["夹克", "卫衣", "衬衫", "休闲裤", "针织衫"],
    "配饰": ["围巾", "腰带", "帽子"],
}
SEASONS = ["春季", "夏季", "秋季", "冬季"]
STYLES = ["法式", "通勤", "甜酷", "复古", "简约", "韩系", "御姐", "学院", "温柔"]
FABRICS = ["醋酸", "羊毛混纺", "雪纺", "纯棉", "羊毛", "聚酯纤维", "天丝", "灯芯绒", "真丝"]
COLORS = ["米白", "黑色", "焦糖", "雾霾蓝", "燕麦", "复古红", "深灰", "奶油白", "卡其", "墨绿"]
SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
DETAILS = ["收腰显瘦", "小个子友好", "大码宽松", "气质百搭", "显瘦遮肉", "长款过膝", "短款减龄"]
CHANNELS = ["自然流量", "搜索", "直播", "广告", "私域"]
PROVINCES = ["广东", "浙江", "江苏", "上海", "北京", "四川", "山东", "福建", "湖北", "湖南"]

KB_CATEGORIES = {
    "尺码": [
        (
            "S/M/L 分别对应什么身高体重",
            "S 适合 155-162cm / 45-52kg；M 适合 160-167cm / 50-58kg；"
            "L 适合 165-172cm / 56-65kg。建议按胸围优先选择，胸围比身高更能决定合身度。",
        ),
        (
            "微胖身材怎么选码",
            "建议比常规身型大一码，并优先选择有弹性的面料（醋酸、针织）。避免选择硬挺无弹的涤纶面料。",
        ),
        ("身高 158 能穿长款吗", "可以。155-160cm 建议选择衣长 105cm 以内的长款，搭配 5cm 以上鞋跟可拉长比例。"),
    ],
    "退换货": [
        ("支持七天无理由吗", "支持。签收后 7 天内、吊牌完整、未洗涤未穿着的情况下可申请无理由退货，运费由买家承担。"),
        (
            "质量问题谁承担运费",
            "质量问题（破洞、污渍、明显色差、尺码标错）由我们承担来回运费，"
            "请在申请时上传至少 2 张商品细节图，客服 24 小时内审核。",
        ),
        ("换货要多久", "仓库签收后 1 个工作日内发出，江浙沪 1-2 天送达，其他地区 3-5 天。"),
        (
            "洗过还能退吗",
            "已水洗的商品无法二次销售，通常不支持退换。若为面料本身质量问题（如甲醛味、严重掉色），"
            "请拍照联系客服协商处理。",
        ),
    ],
    "物流": [
        (
            "多久发货",
            "付款后 48 小时内发货，大促期间（双11/618）可能延长至 72 小时，"
            "下单页会显示预计发货时间。",
        ),
        ("能指定快递吗", "默认发中通/圆通。需要指定顺丰请下单备注，顺丰需补差价 8 元。"),
        (
            "物流不更新怎么办",
            "揽收后 24 小时无更新通常是中转仓积压，建议再等 1 天；超过 72 小时未更新可联系客服发起查件。",
        ),
    ],
    "材质": [
        (
            "醋酸面料怎么洗",
            "建议干洗或手洗（30 度以下冷水），不可漂白、不可拧干，阴凉处悬挂晾干。醋酸怕高温，禁止熨烫超过 110 度。"),
        (
            "羊毛会起球吗",
            "羊毛混纺在摩擦部位（袖口、腋下）会有轻微起球，属正常现象。"
            "用毛球修剪器处理即可，不影响穿着。",
        ),
        ("掉色正常吗", "深色纯棉首次下水有轻微浮色属正常。建议单独手洗、冷水、加少量食盐固色。"),
    ],
    "发票": [
        (
            "能开增值税发票吗",
            "支持。下单后联系客服提供公司名称、税号、开户行及账号，电子发票 3 个工作日内发到指定邮箱。",
        ),
        ("发票金额含优惠券吗", "按实付金额开具。使用优惠券的部分不开票。"),
    ],
    "支付": [
        ("支持花呗分期吗", "支持。单笔满 300 元可分 3/6/12 期，具体以支付页面显示为准。"),
        ("优惠券能叠加吗", "店铺券和平台券可叠加使用，同一类型的券不可叠加。"),
    ],
}

# 工单模板：(主题, 内容, 意图, 情绪, 优先级)
TICKET_TEMPLATES = [
    (
        "尺码选不准，想问下推荐",
        "我身高 163 体重 55kg，平时穿 M，这件连衣裙建议选什么码？",
        "尺码咨询",
        "neutral",
        "normal",
    ),
    ("申请退货", "收到货了，颜色和图片差太多，想退货。", "退换货", "negative", "normal"),
    (
        "衣服有破洞",
        "刚拆开就发现袖子接缝处有个洞，这质量也太差了，必须给个说法！",
        "质量投诉",
        "angry",
        "high",
    ),
    (
        "物流三天没动",
        "发货三天了物流一直停在杭州转运中心，什么时候能到？",
        "物流查询",
        "negative",
        "normal",
    ),
    ("发票怎么开", "公司采购需要开增值税普通发票，怎么弄？", "发票", "neutral", "low"),
    ("催发货", "下单两天了还没发货，说好 48 小时的。", "催发货", "negative", "normal"),
    (
        "洗了一次就掉色",
        "按说明冷水手洗的，结果整盆水都是红的，还染到别的衣服了，这个损失谁赔？",
        "质量投诉",
        "angry",
        "urgent",
    ),
    ("尺码换大一号", "M 码有点紧，想换成 L 码，怎么操作？", "退换货", "neutral", "normal"),
    (
        "面料是纯羊毛吗",
        "详情页写着羊毛混纺，具体含毛量是多少？",
        "尺码咨询",
        "neutral",
        "low",
    ),
    (
        "包裹破损",
        "快递盒被压扁了，里面的衣服有折痕，虽然能穿但很不爽。",
        "物流查询",
        "negative",
        "normal",
    ),
    (
        "优惠券怎么用不了",
        "领了店铺 50 元券，结算时显示不满足使用条件。",
        "支付",
        "negative",
        "normal",
    ),
    (
        "要投诉客服",
        "上一个客服说 24 小时回复，结果两天了没人理我，我要投诉！",
        "质量投诉",
        "angry",
        "urgent",
    ),
]


# ===========================================================================
# 商品与 SKU
# ===========================================================================
def make_products(rng: random.Random, count: int = 60) -> list[dict]:
    products: list[dict] = []
    for i in range(count):
        parent = rng.choice(list(CATEGORIES))
        sub = rng.choice(CATEGORIES[parent])
        season = rng.choices(SEASONS, weights=[1, 2, 5, 2])[0]  # 秋季为主
        style = rng.choice(STYLES)
        fabric = rng.choice(FABRICS)
        detail = rng.choice(DETAILS)

        cost = Decimal(rng.randrange(4000, 22000)) / 100  # 40.00 ~ 220.00
        list_price = (cost * Decimal(str(round(rng.uniform(2.6, 4.2), 2)))).quantize(Decimal("0.01"))

        status = rng.choices(
            ["on_sale", "ready", "draft", "off_shelf"], weights=[5, 2, 2, 1]
        )[0]
        products.append(
            {
                "spu_code": f"SPU{2026}{i + 1:04d}",
                "title": f"{season}{style}{fabric}{sub}{detail}",
                "category": parent,
                "sub_category": sub,
                "season": season,
                "brand": f"{style}衣橱",
                "style_tags": rng.sample(STYLES, k=2),
                "target_audience": rng.choice(
                    ["18-25 学生党", "25-30 职场新人", "30-38 轻熟白领", "全年龄段"]
                ),
                "fabric": fabric,
                "cost_price": cost,
                "list_price": list_price,
                "rating": Decimal(str(round(rng.uniform(4.1, 4.95), 2))),
                "status": status,
                "launch_date": date(2026, 8, 1) + timedelta(days=rng.randrange(0, 60)),
                "description": f"{fabric}面料，{style}风格{sub}，{detail}。适合{season}日常通勤与约会场景。",
                "image_urls": [f"/static/mock/{i + 1:04d}_{n}.jpg" for n in range(1, 4)],
            }
        )
    return products


def make_skus(rng: random.Random, products: list[dict]) -> list[dict]:
    """每个 SPU 拆成 颜色 × 尺码 的 SKU。

    定价规律：热销色（黑/米白）不打折，冷门色便宜 10~20 元 ——
    这样数据分析 Agent 才能做出"颜色维度的价格弹性"结论。
    """
    skus: list[dict] = []
    counter = 1
    for p_idx, product in enumerate(products, start=1):
        colors = rng.sample(COLORS, k=rng.randrange(2, 4))
        sizes = rng.sample(SIZES, k=rng.randrange(3, 5))
        hot_colors = {"黑色", "米白", "奶油白"}
        for color in colors:
            for size in sizes:
                base = product["list_price"] * Decimal(str(round(rng.uniform(0.62, 0.85), 2)))
                if color not in hot_colors:
                    base -= Decimal(rng.randrange(0, 2000)) / 100
                skus.append(
                    {
                        "product_index": p_idx,  # 1-based，orchestrator 用它还原外键
                        "sku_code": f"SKU{counter:05d}",
                        "color": color,
                        "size": size,
                        "cost_price": product["cost_price"],
                        "sale_price": base.quantize(Decimal("0.01")),
                        "barcode": f"69{rng.randrange(10**10, 10**11 - 1)}",
                        "weight_gram": rng.randrange(180, 900),
                        "is_active": random.Random(counter).random() > 0.08,
                    }
                )
                counter += 1
    return skus


# ===========================================================================
# 竞品
# ===========================================================================
def make_competitors(rng: random.Random, count: int = 80) -> list[dict]:
    items: list[dict] = []
    for i in range(count):
        parent = rng.choice(list(CATEGORIES))
        sub = rng.choice(CATEGORIES[parent])
        base_price = Decimal(rng.randrange(6900, 39900)) / 100

        # 价格历史：有人一路降价（价格战），有人先涨后降（大促套路）
        history = []
        price = base_price * Decimal(str(round(rng.uniform(1.0, 1.35), 2)))
        trend = rng.choice(["down", "down", "flat", "updown"])
        for day in range(30, 0, -1):
            if trend == "down":
                price *= Decimal("0.995") if rng.random() < 0.6 else Decimal("1.0")
            elif trend == "updown":
                price *= Decimal("1.01") if day > 12 else Decimal("0.97")
            else:
                price *= Decimal(str(round(rng.uniform(0.995, 1.005), 4)))
            history.append(
                {
                    "date": (date(2026, 9, 11) - timedelta(days=day)).isoformat(),
                    "price": float(price.quantize(Decimal("0.01"))),
                }
            )

        items.append(
            {
                "platform": rng.choice(["mock", "douyin", "taobao"]),
                "shop_name": f"{rng.choice(STYLES)}旗舰店{rng.randrange(1, 99)}",
                "product_title": f"{rng.choice(SEASONS)}{rng.choice(STYLES)}{rng.choice(FABRICS)}{sub}",
                "category": parent,
                "sub_category": sub,
                "season": rng.choice(SEASONS),
                "style_tags": rng.sample(STYLES, k=2),
                "price": history[-1]["price"],
                "original_price": float(base_price),
                "monthly_sales": rng.randrange(0, 15000),
                "review_count": rng.randrange(0, 8000),
                "rating": Decimal(str(round(rng.uniform(4.0, 4.95), 2))),
                "url": f"https://mock-ecom.example.com/item/{1000000 + i}",
                "price_history": history,
            }
        )
    return items


# ===========================================================================
# 库存
# ===========================================================================
def make_inventory(
    rng: random.Random, sku_count: int, days: int = 60
) -> tuple[list[dict], dict[int, int]]:
    """生成库存流水，并返回**期末可用库存**快照。

    ``final_stock[sku_index]`` 是选品 Agent 做硬约束过滤时用的库存量。
    ``STOCKOUT_RATIO`` 比例的 SKU 期末会被压到 0~5 件，制造"看着不错但没货"的场景。

    实现上刻意**先定期末目标、再倒推期初库存**：

    1. 随机出期间所有变动，求和得到净变动
    2. 期初 = 期末目标 - 净变动

    如果反过来"先随机期初、再看期末落到哪"，那么中间那些 ``in 入库``
    会把余额越加越高，想要的"库存告急"根本不会出现 —— 这个 bug
    第一次跑就露馅了（告急 SKU 数量是 0）。
    """
    start = datetime(2026, 9, 11) - timedelta(days=days)
    end = datetime(2026, 9, 11, 21, 0, 0)
    stockout_skus = set(
        rng.sample(range(1, sku_count + 1), k=max(1, int(sku_count * STOCKOUT_RATIO)))
    )

    rows: list[dict] = []
    final_stock: dict[int, int] = {}

    for sku_index in range(1, sku_count + 1):
        is_stockout = sku_index in stockout_skus
        target = rng.randrange(0, 6) if is_stockout else rng.randrange(40, 400)

        # ---- 1. 先生成期间变动 ----
        movements: list[tuple[datetime, str, int]] = []
        for _ in range(rng.randrange(6, 22)):
            occurred = start + timedelta(
                days=rng.randrange(0, days),
                hours=rng.randrange(9, 22),
                minutes=rng.randrange(0, 60),
            )
            kind = rng.choices(
                ["sale", "return", "in", "out", "adjust"], weights=[10, 2, 3, 2, 1]
            )[0]
            if kind == "sale":
                qty = -rng.randrange(1, 6)
            elif kind == "return":
                qty = rng.randrange(1, 3)
            elif kind == "in":
                qty = rng.randrange(20, 120)
            elif kind == "out":
                qty = -rng.randrange(1, 5)
            else:
                qty = rng.randrange(-3, 4)
            movements.append((occurred, kind, qty))

        movements.sort(key=lambda m: m[0])  # 必须按时间顺序回放，否则 quantity_after 会乱
        net_change = sum(qty for _, _, qty in movements)

        # ---- 2. 倒推期初 ----
        opening = max(0, target - net_change)
        balance = opening
        rows.append(
            {
                "sku_index": sku_index,
                "warehouse": "主仓",
                "change_type": "in",
                "quantity": opening,
                "quantity_after": balance,
                "ref_no": f"PO{start.strftime('%Y%m%d')}{sku_index:05d}",
                "note": "期初入库",
                "occurred_at": start,
            }
        )

        # ---- 3. 按时间回放 ----
        for occurred, kind, qty in movements:
            balance = max(0, balance + qty)
            rows.append(
                {
                    "sku_index": sku_index,
                    "warehouse": "主仓",
                    "change_type": kind,
                    "quantity": qty,
                    "quantity_after": balance,
                    "ref_no": f"{kind.upper()[:2]}{occurred.strftime('%y%m%d%H%M')}{sku_index:04d}",
                    "note": None,
                    "occurred_at": occurred,
                }
            )

        # ---- 4. 收口 ----
        # balance 被 max(0, ...) 截断过，可能偏离 target，用一条盘点调整拉回来。
        # 差额通常很小；正常 SKU 走这条分支说明确实发生过盘盈亏。
        if balance != target:
            rows.append(
                {
                    "sku_index": sku_index,
                    "warehouse": "主仓",
                    "change_type": "adjust",
                    "quantity": target - balance,
                    "quantity_after": target,
                    "ref_no": f"ADJ{end.strftime('%y%m%d')}{sku_index:05d}",
                    "note": (
                        "盘点调整：可售不足 5 件，需立即补货"
                        if is_stockout
                        else "月度盘点调整"
                    ),
                    "occurred_at": end,
                }
            )
            balance = target

        final_stock[sku_index] = balance

    rows.sort(key=lambda r: r["occurred_at"])
    return rows, final_stock


# ===========================================================================
# 订单与 BI
# ===========================================================================
def make_orders(
    rng: random.Random, sku_meta: list[dict], days: int = 60, count: int = 800
) -> list[dict]:
    """``sku_meta`` 每项需含 ``sku_index / sale_price / product_title / category``。"""
    orders: list[dict] = []
    now = datetime(2026, 9, 11, 21, 0, 0)
    for i in range(count):
        sku = rng.choice(sku_meta)
        paid_at = now - timedelta(
            days=rng.randrange(0, days), hours=rng.randrange(0, 14), minutes=rng.randrange(0, 60)
        )
        qty = rng.choices([1, 2, 3], weights=[82, 14, 4])[0]
        unit = sku["sale_price"]
        total = (unit * qty).quantize(Decimal("0.01"))
        discount = (
            Decimal("0.00")
            if rng.random() < 0.55
            else (total * Decimal(str(round(rng.uniform(0.05, 0.25), 2)))).quantize(Decimal("0.01"))
        )
        actual = total - discount

        status = rng.choices(
            ["completed", "shipped", "paid", "refunded", "refunding", "cancelled"],
            weights=[58, 16, 8, 11, 5, 2],
        )[0]
        refunded_at = (
            paid_at + timedelta(days=rng.randrange(2, 12)) if status == "refunded" else None
        )
        orders.append(
            {
                "order_no": f"SO{paid_at.strftime('%y%m%d')}{i + 1:05d}",
                "platform": "mock",
                "buyer_nick": f"买家{rng.randrange(1000, 9999)}",
                "is_new_customer": rng.random() < 0.62,
                "sku_index": sku["sku_index"],
                "product_title": sku["product_title"],
                # 下划线开头 = 中间字段，用来聚合 BI，不落库
                "_category": sku["category"],
                "quantity": qty,
                "unit_price": unit,
                "total_amount": total,
                "discount_amount": discount,
                "actual_amount": actual,
                "status": status,
                "channel": rng.choices(CHANNELS, weights=[44, 22, 12, 18, 4])[0],
                "province": rng.choice(PROVINCES),
                "city": None,
                "paid_at": paid_at,
                "shipped_at": paid_at + timedelta(hours=rng.randrange(6, 60)),
                "refunded_at": refunded_at,
            }
        )
    return orders


def make_bi_metrics(rng: random.Random, orders: list[dict], days: int = 60) -> list[dict]:
    """按天 × 类目聚合订单，再补上 UV/PV/广告数据。

    这里刻意让 **连衣裙类目退货率最高**（服装行业真实规律：版型主观性强）。
    """
    now = date(2026, 9, 11)
    cats = ["全部", "女装", "男装", "配饰"]
    sub_cat_stats: dict[tuple[str, str], dict] = {}

    for order in orders:
        if order["status"] in ("cancelled",):
            continue
        key = (order["paid_at"].date().isoformat(), order["_category"])
        stat = sub_cat_stats.setdefault(key, {"gmv": Decimal("0"), "orders": 0, "refund": Decimal("0")})
        stat["gmv"] += order["actual_amount"]
        stat["orders"] += 1
        if order["status"] in ("refunded", "refunding"):
            stat["refund"] += order["actual_amount"]

    rows: list[dict] = []
    for offset in range(days, 0, -1):
        day = now - timedelta(days=offset)
        # 周末流量高、工作日转化高 —— 制造一点可被 Agent 发现的规律
        weekend = day.weekday() >= 5
        total_orders = sum(
            s["orders"] for (d, _), s in sub_cat_stats.items() if d == day.isoformat()
        )
        base_uv = rng.randrange(6000, 12000) * (1.25 if weekend else 1.0)

        for cat in cats:
            stat = sub_cat_stats.get((day.isoformat(), cat), {"gmv": Decimal("0"), "orders": 0, "refund": Decimal("0")})
            order_count = stat["orders"] if cat != "全部" else total_orders
            # sum() 在空序列上返回 int 0 而不是 Decimal，必须显式给起始值，
            # 否则下面的 .quantize() 会抛 AttributeError。
            # 这个坑只在"某一天完全没订单"时才显形 —— 典型的低概率数据边界。
            gmv = (
                stat["gmv"]
                if cat != "全部"
                else sum(
                    (s["gmv"] for (d, _), s in sub_cat_stats.items() if d == day.isoformat()),
                    Decimal("0"),
                )
            )
            refund = (
                stat["refund"]
                if cat != "全部"
                else sum(
                    (s["refund"] for (d, _), s in sub_cat_stats.items() if d == day.isoformat()),
                    Decimal("0"),
                )
            )

            uv = max(1, int(base_uv * rng.uniform(0.25, 1.0))) if cat != "全部" else int(base_uv)
            pv = int(uv * rng.uniform(2.2, 4.5))
            customers = max(1, int(order_count * rng.uniform(0.86, 0.97)))

            return_rate_base = 0.34 if cat == HIGH_RETURN_CATEGORY else rng.uniform(0.14, 0.26)
            return_rate = min(0.72, max(0.03, return_rate_base + rng.uniform(-0.05, 0.05)))

            ad_spend = (gmv * Decimal(str(round(rng.uniform(0.08, 0.24), 3)))).quantize(Decimal("0.01"))
            # 广告带来约 30%~55% 的 GMV
            ad_revenue = (gmv * Decimal(str(round(rng.uniform(0.30, 0.55), 3)))).quantize(Decimal("0.01"))
            roi = (ad_revenue / ad_spend).quantize(Decimal("0.01")) if ad_spend else None

            rows.append(
                {
                    "metric_date": day,
                    "category": cat,
                    "gmv": gmv.quantize(Decimal("0.01")),
                    "order_count": order_count,
                    "customer_count": customers,
                    "avg_order_value": (
                        (gmv / order_count).quantize(Decimal("0.01")) if order_count else None
                    ),
                    "refund_amount": refund.quantize(Decimal("0.01")),
                    "return_rate": Decimal(str(round(return_rate, 6))),
                    "uv": uv,
                    "pv": pv,
                    "conversion_rate": Decimal(str(round(order_count / uv, 6))),
                    "ad_spend": ad_spend,
                    "ad_revenue": ad_revenue,
                    "ad_roi": roi,
                    "stockout_sku_count": rng.randrange(0, 26),
                }
            )
    return rows


# ===========================================================================
# 知识库与工单
# ===========================================================================
def make_kb_articles() -> list[dict]:
    rows: list[dict] = []
    counter = 1
    for category, articles in KB_CATEGORIES.items():
        for title, content in articles:
            rows.append(
                {
                    "article_no": f"KB{counter:04d}",
                    "title": title,
                    "category": category,
                    "content": content,
                    # 拼接检索文本：类目 + 标题 + 正文。比正文裸文本检索效果好得多
                    "retrieval_text": f"[{category}] {title}。{content}",
                    "tags": [category, "售前" if category in ("尺码", "材质") else "售后"],
                    "version": "v1",
                    "is_active": True,
                }
            )
            counter += 1
    return rows


def make_tickets(
    rng: random.Random, order_nos: list[str], count: int = 60
) -> list[dict]:
    """生成工单。

    按 ``ANGRY_TICKET_RATIO`` 强制制造情绪激动的纠纷工单，
    并且给它们挂上订单号 —— 客服 Agent 必须能查到关联订单才谈得上"处理"。
    """
    rows: list[dict] = []
    angry_quota = int(count * ANGRY_TICKET_RATIO)
    now = datetime(2026, 9, 11, 21, 0, 0)

    for i in range(count):
        if i < angry_quota:
            pool = [t for t in TICKET_TEMPLATES if t[3] == "angry"]
        else:
            pool = [t for t in TICKET_TEMPLATES if t[3] != "angry"]
        subject, content, intent, sentiment, priority = rng.choice(pool)

        created = now - timedelta(days=rng.randrange(0, 20), minutes=rng.randrange(0, 1440))
        is_angry = sentiment == "angry"
        # 纠纷类工单必然转人工：涉及金额、情绪激动，Agent 不该自作主张
        escalated = is_angry or (intent in ("质量投诉", "退换货") and rng.random() < 0.35)

        if escalated:
            status = rng.choice(["transferred", "waiting"])
            handler = "human"
            resolution = None if status == "waiting" else "已安抚并补偿优惠券 20 元，客户接受。"
        else:
            status = rng.choices(["closed", "processing", "open"], weights=[6, 3, 1])[0]
            handler = "agent"
            resolution = "已按知识库话术答复客户。" if status == "closed" else None

        rows.append(
            {
                "ticket_no": f"TK{created.strftime('%y%m%d')}{i + 1:04d}",
                "channel": rng.choice(["douyin", "taobao", "wechat", "mock"]),
                "customer_nick": f"买家{rng.randrange(1000, 9999)}",
                "order_no": rng.choice(order_nos) if rng.random() < 0.7 else None,
                "subject": subject,
                "content": content,
                "intent": intent,
                "sentiment": sentiment,
                "priority": priority,
                "status": status,
                "handler": handler,
                "escalated": escalated,
                "escalate_reason": (
                    rng.choice(
                        [
                            "客户情绪激动，多次表达不满，建议人工介入",
                            "涉及金额纠纷超过 200 元，超出 Agent 自主处理额度",
                            "客户明确提出投诉，触发转人工规则",
                        ]
                    )
                    if escalated
                    else None
                ),
                "resolution": resolution,
                "cited_article_nos": (
                    [f"KB{rng.randrange(1, 14):04d}" for _ in range(rng.randrange(1, 3))]
                    if not escalated
                    else None
                ),
                "confidence": (
                    None if escalated else Decimal(str(round(rng.uniform(0.62, 0.97), 6)))
                ),
                "first_response_seconds": rng.randrange(2, 240) if handler == "agent" else rng.randrange(600, 7200),
                "resolved_at": created + timedelta(hours=rng.randrange(1, 48)) if status == "closed" else None,
                "created_at": created,
                "updated_at": created + timedelta(hours=rng.randrange(0, 24)),
            }
        )
    return rows


# ===========================================================================
# 编排：任务 / 事件 / Agent 运行
# ===========================================================================
NEW_PRODUCT_DAG = {
    "nodes": [
        {"id": "understand", "name": "需求理解", "agent": "orchestrator"},
        {"id": "select", "name": "选品打分", "agent": "selection"},
        {"id": "analyze", "name": "数据分析", "agent": "analytics"},
        {"id": "approve", "name": "人工确认", "agent": "human_gate", "interrupt": True},
        {"id": "artwork", "name": "图文生成", "agent": "creative"},
        {"id": "shelf", "name": "商品上架", "agent": "listing"},
        {"id": "advertise", "name": "广告投放", "agent": "ads"},
        {"id": "notify", "name": "邮件通知", "agent": "notification"},
        {"id": "review", "name": "复盘", "agent": "orchestrator"},
    ],
    "edges": [
        ["understand", "select"],
        ["select", "analyze"],
        ["analyze", "approve"],
        ["approve", "artwork"],
        ["artwork", "shelf"],
        ["shelf", "advertise"],
        ["advertise", "notify"],
        ["notify", "review"],
    ],
}

DAILY_REPORT_DAG = {
    "nodes": [
        {"id": "understand", "name": "需求理解", "agent": "orchestrator"},
        {"id": "analyze", "name": "数据分析", "agent": "analytics"},
        {"id": "notify", "name": "邮件通知", "agent": "notification"},
    ],
    "edges": [["understand", "analyze"], ["analyze", "notify"]],
}

#: 需求池。**刻意铺到 18 条**：20 个任务从 8 条里抽的话，
#: 列表页会出现同一个标题连着四五次 —— 一眼就是假数据。
#: 这里覆盖三类意图的组合（选品 / 分析 / 通知），
#: 让"规划器按关键词拆步骤"这件事在列表上就看得出来。
TASK_REQUESTS = [
    ("帮我选 3 款秋季女装并出分析报告", "new_product_launch"),
    ("分析昨天各品类销售情况，出日报", "daily_report"),
    ("给秋季连衣裙做一组主图和小红书文案", "new_product_launch"),
    ("抖音新款连衣裙投放 ROI 掉了，帮我看看要不要停", "ad_optimize"),
    ("处理今天积压的售后工单", "after_sale"),
    ("生成上周店铺经营周报并邮件发我", "daily_report"),
    ("帮我把这款风衣上架到抖音小店", "new_product_launch"),
    ("最近退货率是不是变高了？做个归因", "daily_report"),
    ("挑 5 款适合上新的秋季连衣裙", "new_product_launch"),
    ("选 2 款针织衫做双十一预热，顺便出份选品表", "new_product_launch"),
    ("把近 7 天的渠道 GMV 拉出来对比一下", "daily_report"),
    ("出一份本月退货率专项分析，发给商品部", "daily_report"),
    ("看看哪些 SKU 库存告急，列个补货清单", "after_sale"),
    ("把上周的广告投放复盘一下，ROI 低于 1 的标出来", "ad_optimize"),
    ("帮我挑 3 款大衣做直播专场，要能撑得住库存", "new_product_launch"),
    ("这个月客单价掉了，做个归因分析给我", "daily_report"),
    ("把这份选品结论整理成邮件发给运营组", "daily_report"),
    ("查一下 SKU-1024 现在的可用库存", "after_sale"),
]


#: 执行 Agent 的名字。新架构里任务侧**只有一个** Agent（见 `agents/loop.py`），
#: 所有步骤都记在它名下 —— 指标看板上看到的就是这一个名字，不再有 9 个节点。
TASK_AGENT = "task"

#: 每类工作"做完了"的一句话结论，用来拼任务级 summary 和每个 todo 的结论。
#: 数字是**编的但看起来合理**的量级 —— 假数据不能有"测试"味儿。
_KIND_SUMMARY: dict[str, str] = {
    "selection": "已选出 3 款候选（综合得分 87.4 / 82.1 / 79.6）",
    "analytics": "已生成近 30 天经营分析报表（GMV ¥130,226.43，共 322 单）",
    "notification": "已将结论与报表邮件发送给运营组",
    "generic": "已完成，结论见上方摘要",
}


def make_tasks(rng: random.Random, count: int = 20) -> list[dict]:
    """生成任务行。

    ## 计划是怎么来的：调真正的规划器，而不是在假数据里另写一套

    这里直接调 ``agents/workplan.plan_todos()``，它和线上跑任务时用的是
    **同一个函数**。这么做有两个好处：

    1. 假数据里的步骤拆分永远是"真实规则"的样子 —— 不会出现"演示数据里
       有 9 个节点、实际任务只有 3 步"这种割裂
    2. 规划规则一改，假数据自动跟着变；要是抄一份在生成器里，迟早分叉
    """
    from fashionops.agents.workplan import plan_payload, plan_todos

    now = datetime(2026, 9, 11, 21, 0, 0)
    rows: list[dict] = []
    for i in range(count):
        request, task_type = rng.choice(TASK_REQUESTS)
        created = now - timedelta(days=rng.randrange(0, 14), hours=rng.randrange(0, 12))
        status = rng.choices(
            ["succeeded", "running", "waiting_approval", "failed", "pending", "cancelled"],
            weights=[48, 14, 12, 12, 8, 6],
        )[0]
        plan = [s for s in plan_todos(request)]

        # 只有"包含需要审批的那一步"的任务才可能卡在闸口。
        # 不判的话会出现"一步查库存的任务在等人确认发邮件"这种假数据 ——
        # 演示时一眼就被看穿，而且会让人误以为审批规则是随机的。
        if status == "waiting_approval" and not any(s.approval_required for s in plan):
            status = "succeeded"

        progress = {
            "succeeded": 100,
            "running": rng.randrange(15, 85),
            "waiting_approval": rng.randrange(40, 70),
            "failed": rng.randrange(10, 80),
            "pending": 0,
            "cancelled": rng.randrange(0, 50),
        }[status]
        finished = status in ("succeeded", "failed", "cancelled")

        # 每一步停在哪：由任务终态反推"跑到第几步了"，
        # 这样事件流 / todo 状态 / 进度条三者一定对得上（对不上最容易被一眼看穿）
        stop_at = {
            "succeeded": len(plan),
            "running": rng.randrange(1, len(plan) + 1),
            "waiting_approval": len(plan),
            "failed": rng.randrange(1, len(plan) + 1),
            "pending": 0,
            "cancelled": rng.randrange(1, len(plan) + 1),
        }[status]

        rows.append(
            {
                "task_no": f"T{created.strftime('%y%m%d')}{i + 1:03d}",
                "title": request[:40],
                "user_request": request,
                "task_type": task_type,
                "status": status,
                # 当前节点不再是"图里的节点名"，而是"第几步"
                "current_node": (f"todo-{stop_at}" if stop_at else None),
                "progress": progress,
                "priority": rng.randrange(1, 9),
                "dag": plan_payload(plan),
                # 以下划线开头的是给**其它生成器**用的中间量，不进模型
                # （seed 里统一过滤掉，见 `seed()` 里的那段说明）
                "_plan": plan,
                "_stop_at": stop_at,
                "result": (
                    {
                        "summary": (
                            f"已完成 {len(plan)} 步："
                            + " ｜ ".join(_KIND_SUMMARY.get(s.kind, "已完成") for s in plan)
                        ),
                        "todo_count": len(plan),
                    }
                    if status == "succeeded"
                    else None
                ),
                "error_message": (
                    rng.choice(
                        [
                            "工具调用超时：mock_ad_api.timeout after 30s",
                            "结构化输出校验失败重试 3 次后放弃：SelectionResult.score 缺失",
                            "外部 API 熔断中：ecom.create_listing 连续失败 5 次",
                        ]
                    )
                    if status == "failed"
                    else None
                ),
                # 不再写 thread_id：它是 LangGraph checkpoint 的遗留物，
                # 新架构没有 checkpoint，写上去只会让人以为还有状态机
                "approval_payload": (
                    {
                        "action": "执行「把结论与报表邮件发送给运营组」中的 mail.send 调用",
                        "reason": "mail.send 会产生对外不可撤销的副作用，需要人工确认",
                        "risk": "medium",
                        "todo_seq": len(plan),
                        "tool": "mail.send",
                        "preview": (
                            "收件人：ops@fashionops.com\n"
                            "主题：【经营日报】近 30 天数据与退货率告警\n"
                            "正文（86 字）：近 30 天 GMV ¥130,226.43，订单 322 单；"
                            "直播渠道退货率 40.0% 超过 30% 红线，详见附件报表。"
                        ),
                    }
                    if status == "waiting_approval"
                    else None
                ),
                "llm_calls": rng.randrange(3, 42),
                "total_tokens": rng.randrange(4000, 180000),
                "total_cost": Decimal(str(round(rng.uniform(0.02, 3.60), 6))),
                "created_by": rng.choice(["user", "user", "user", "scheduler"]),
                "started_at": created + timedelta(seconds=rng.randrange(1, 60)),
                "finished_at": created + timedelta(minutes=rng.randrange(2, 90)) if finished else None,
                "created_at": created,
                "updated_at": created + timedelta(minutes=rng.randrange(1, 90)),
            }
        )

    _ensure_one_waiting_approval(rng, rows)
    return rows


def _ensure_one_waiting_approval(rng: random.Random, rows: list[dict]) -> None:
    """保证**至少有一个**任务卡在人工闸口。

    ## 为什么要强制

    随机抽状态的话，某些种子下一次都抽不到 `waiting_approval`
    （默认种子就是这样）。于是演示时看不到审批卡片 ——
    而这个项目最想展示的东西恰好是"不可撤销的对外动作会停下来等人"。

    所以这里做一件**显式**的事：如果没有闸口任务，就把第一个"计划里含审批步骤"
    的任务改成闸口状态，并把与之相关的字段（进度 / 当前步 / 审批载荷 / 是否结束）
    一起改对 —— 只改 status 会留下"状态说在等人、但 result 已经写好了"这种自相矛盾的数据。
    """
    if any(r["status"] == "waiting_approval" for r in rows):
        return
    for row in rows:
        if not any(s.approval_required for s in row["_plan"]):
            continue
        row["status"] = "waiting_approval"
        row["progress"] = rng.randrange(40, 70)
        row["_stop_at"] = [s.seq for s in row["_plan"] if s.approval_required][0]
        row["current_node"] = f"todo-{row['_stop_at']}"
        row["result"] = None
        row["finished_at"] = None
        row["approval_payload"] = {
            "action": "执行「把结论与报表邮件发送给运营组」中的 mail.send 调用",
            "reason": "mail.send 会产生对外不可撤销的副作用，需要人工确认",
            "risk": "medium",
            "todo_seq": row["_stop_at"],
            "tool": "mail.send",
            "preview": (
                "收件人：ops@fashionops.com\n"
                "主题：【经营日报】近 30 天数据与退货率告警\n"
                "正文（86 字）：近 30 天 GMV ¥130,226.43，订单 322 单；"
                "直播渠道退货率 40.0% 超过 30% 红线，详见附件报表。"
            ),
        }
        return


#: 每一步里会出现的过程事件。名字和线上 `agents/loop.py` 写出的一致 ——
#: 前端按 event_type 渲染，假数据用另一套名字的话，演示时会一片空白。
TODO_EVENT_TEMPLATES = [
    ("tool_call", "info", "调用工具 erp.query_candidates"),
    ("tool_call", "info", "调用工具 selection.rank_candidates"),
    ("tool_call", "info", "调用工具 erp.query_sales_summary"),
    ("tool_call", "info", "调用工具 analytics.detect_alerts"),
    ("tool_call", "info", "调用工具 analytics.render_business_report"),
    ("llm_call", "info", "模型返回结构化结果"),
    ("log", "debug", "命中缓存，跳过重复查询"),
]


def _events_for_task(rng: random.Random, task_index: int, task: dict) -> list[dict]:
    """一个任务的事件流。拆成独立函数是为了让 `seq` 的推进一眼可见。"""
    plan = task["_plan"]
    stop_at = task["_stop_at"]
    status = task["status"]
    base = task["created_at"]
    rows: list[dict] = []

    def emit(event_type: str, *, node: str | None = None, level: str = "info",
             message: str = "", payload=None, duration_ms: int | None = None) -> None:
        rows.append(
            {
                "task_index": task_index,
                "seq": len(rows) + 1,  # 从 1 连续递增，不跳号
                "event_type": event_type,
                "node_name": node,
                "agent_name": TASK_AGENT if node else None,
                "level": level,
                "message": message,
                "payload": payload,
                "duration_ms": duration_ms,
                "created_at": base + timedelta(seconds=(len(rows) + 1) * 9),
            }
        )

    emit("task_started", node="start", message=f"任务开始：{task['user_request']}")
    emit(
        "task_planned",
        node="plan",
        message="已拆分为 " + " → ".join(s.kind for s in plan),
        payload={"mode": "todo_list", "todos": len(plan)},
    )

    if status == "pending":
        return rows

    for spec in plan:
        node = f"todo-{spec.seq}"
        if spec.seq > stop_at:
            # 取消/驳回之后，余下的步骤是被跳过的，不是失败
            if status == "cancelled":
                emit("node_finished", node=node, level="warn",
                     message=f"第 {spec.seq} 步被跳过：上游未通过人工确认")
            continue

        emit("todo_started", node=node, message=f"第 {spec.seq} 步（{spec.kind}）：{spec.goal}")

        for _ in range(rng.randrange(1, 4)):
            event_type, level, message = rng.choice(TODO_EVENT_TEMPLATES)
            emit(event_type, node=node, level=level, message=message,
                 payload={"elapsed_step": len(rows) + 1},
                 duration_ms=rng.randrange(80, 9000))

        if status == "waiting_approval" and spec.seq == stop_at:
            emit("approval_required", node=node, level="warn",
                 message="这一步会产生对外不可撤销的副作用，已挂起等待人工确认",
                 payload=task.get("approval_payload"))
            break
        if status == "running" and spec.seq == stop_at:
            break
        if status == "failed" and spec.seq == stop_at:
            emit("node_finished", node=node, level="error",
                 message=f"第 {spec.seq} 步失败：{task['error_message']}",
                 duration_ms=rng.randrange(200, 24000))
            break

        emit("node_finished", node=node,
             message=_KIND_SUMMARY.get(spec.kind, f"第 {spec.seq} 步完成"),
             duration_ms=rng.randrange(200, 24000))

    if status == "succeeded":
        emit("task_finished", node="finalize", message="任务完成")
    elif status == "failed":
        emit("task_failed", node="finalize", level="error",
             message=task["error_message"] or "任务失败")
    elif status == "cancelled":
        emit("task_cancelled", node="finalize", level="warn", message="任务已被人工取消")
    return rows


def make_task_events(rng: random.Random, tasks: list[dict]) -> list[dict]:
    """按**新架构的事件词汇**生成事件流。

    顺序刻意和线上一致：
    ``task_started → task_planned → (todo_started → tool_call… → node_finished) × N
      → [approval_required] → task_finished / task_failed / task_cancelled``

    ``seq`` 必须从 1 连续递增 —— 它和 ``uq_task_event_seq`` 唯一约束、
    以及 SSE 的断线续传是同一件事（见 `agents/recorder.py` 里那条踩坑记录）。
    """
    rows: list[dict] = []
    for task_index, task in enumerate(tasks, start=1):
        rows.extend(_events_for_task(rng, task_index, task))
    return rows


AGENT_NAMES = [
    TASK_AGENT,
    TASK_AGENT,
    TASK_AGENT,
    "service",  # 客服 Agent 是独立链路（工单不是任务，见 service/agent.py）
]


def make_task_todos(rng: random.Random, tasks: list[dict]) -> list[dict]:
    """给每个任务生成它的步骤（新架构的 `task_todos`）。

    ## 这一步是"看起来像真项目"的关键

    只灌 tasks 不灌 todos 的话，任务详情页会显示"这个任务没有步骤记录"——
    一眼就是空壳。所以每个任务都按它的终态生成一整套步骤：

    * ``succeeded``  → 全部 done，每步都有结论 / 工具 / 耗时 / token
    * ``running``    → 前面 done、当前步 running
    * ``waiting_approval`` → 通知那步 waiting_approval（并挂上审批单号）
    * ``failed``     → 失败那步 failed，后面全部 skipped
    * ``cancelled``  → 被驳回那步及之后全部 skipped
    * ``pending``    → 全部 pending
    """
    rows: list[dict] = []
    for task_index, task in enumerate(tasks, start=1):
        plan = task["_plan"]
        stop_at = task["_stop_at"]
        status = task["status"]
        started = task["started_at"]

        for spec in plan:
            if status == "pending":
                todo_status = "pending"
            elif status == "succeeded":
                todo_status = "done"
            elif spec.seq < stop_at:
                todo_status = "done"  # 终态之前的步骤都已经跑完了
            elif spec.seq == stop_at:
                todo_status = {
                    "running": "running",
                    "waiting_approval": "waiting_approval",
                    "failed": "failed",
                    "cancelled": "skipped",
                }[status]
            else:
                # 上游没做成 → 下游 skipped，而不是失败。
                # failed 是"我错了"，skipped 是"轮不到我"，这两件事不能混
                todo_status = "skipped"

            duration = None
            if todo_status in ("done", "failed"):
                duration = rng.randrange(1500, 52000)
            tokens = rng.randrange(900, 9000) if todo_status in ("done", "failed") else 0

            rows.append(
                {
                    "task_index": task_index,
                    "seq": spec.seq,
                    "goal": spec.goal,
                    "kind": spec.kind,
                    "status": todo_status,
                    "tool_names": (
                        _KIND_TOOLS.get(spec.kind, []) if todo_status in ("done", "failed") else None
                    ),
                    "result_summary": (
                        _KIND_SUMMARY.get(spec.kind) if todo_status == "done" else None
                    ),
                    "artifact_nos": (
                        ["AS20260911000001"] if spec.kind == "analytics" and todo_status == "done"
                        else None
                    ),
                    "error": (
                        task["error_message"]
                        if todo_status == "failed"
                        else ("上游步骤未通过人工确认" if todo_status == "skipped" and status == "cancelled" else None)
                    ),
                    "llm_calls": rng.randrange(1, 8) if todo_status == "done" else 0,
                    "total_tokens": tokens,
                    "total_cost": Decimal(str(round(tokens * 3e-6, 6))),
                    "started_at": started + timedelta(minutes=(spec.seq - 1) * 3) if started else None,
                    "finished_at": (
                        started + timedelta(minutes=(spec.seq - 1) * 3, milliseconds=duration or 0)
                        if started and duration
                        else None
                    ),
                    "duration_ms": duration,
                    "created_at": task["created_at"],
                }
            )
    return rows


#: 每类工作"实际会调"的工具（和 `agents/loop.py` 的 KIND_TOOLS 保持一致）
_KIND_TOOLS: dict[str, list[str]] = {
    "selection": ["erp.query_candidates", "selection.rank_candidates"],
    "analytics": ["erp.query_sales_summary", "analytics.detect_alerts", "analytics.render_business_report"],
    "notification": ["mail.send"],
    "generic": ["erp.query_candidates"],
}


def make_agent_runs(rng: random.Random, tasks: list[dict]) -> list[dict]:
    rows: list[dict] = []
    now = datetime(2026, 9, 11, 21, 0, 0)
    for task_index, task in enumerate(tasks, start=1):
        for _ in range(rng.randrange(2, 7)):
            agent = rng.choice(AGENT_NAMES)
            failed = rng.random() < FAILED_RUN_RATIO
            status = rng.choice(["error", "timeout"]) if failed else "success"
            latency = rng.randrange(800, 42000)
            prompt_tokens = rng.randrange(600, 14000)
            completion_tokens = rng.randrange(80, 2600)
            rows.append(
                {
                    "task_index": task_index,
                    "trace_id": f"{task['task_no']}-{rng.randrange(10**7, 10**8 - 1):08x}",
                    "agent_name": agent,
                    # 一次运行对应**一个 todo**，所以 node_name 是 todo-N；
                    # 客服那条链路跑的是工单，没有 todo，记 service
                    "node_name": (
                        None if agent == "service"
                        else f"todo-{rng.randrange(1, len(task['_plan']) + 1)}"
                    ),
                    "run_index": rng.randrange(1, 3),
                    "model": rng.choices(
                        ["deepseek-chat", "deepseek-chat", "qwen-turbo"], weights=[6, 3, 1]
                    )[0],
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "cost": Decimal(str(round((prompt_tokens * 1e-6 + completion_tokens * 2e-6) * 7.2, 6))),
                    "latency_ms": latency,
                    "status": status,
                    "tool_call_count": rng.randrange(0, 9),
                    "retry_count": rng.randrange(0, 3) if failed else 0,
                    "input_digest": f"{rng.randrange(16**15, 16**16 - 1):x}",
                    "output_digest": f"{rng.randrange(16**15, 16**16 - 1):x}",
                    "error_code": (
                        rng.choice(["TOOL_TIMEOUT", "SCHEMA_VALIDATION_FAILED", "RATE_LIMITED", "CIRCUIT_OPEN"])
                        if failed
                        else None
                    ),
                    "created_at": now - timedelta(days=rng.randrange(0, 14), minutes=rng.randrange(0, 1440)),
                }
            )
    return rows


# ===========================================================================
# 素材 / 广告 / 邮件 / 审计
# ===========================================================================
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ASSET_SPECS = [
    ("image", "主图", "image/jpeg", 1200, 1600, None),
    ("image", "详情页长图", "image/jpeg", 800, 4800, None),
    ("copywriting", "小红书文案", "text/plain", None, None, None),
    ("video", "短视频种草", "video/mp4", 1080, 1920, Decimal("15.00")),
    ("spreadsheet", "选品分析报表.xlsx", XLSX_MIME, None, None, None),
    ("report", "日报.pdf", "application/pdf", None, None, None),
]


def make_assets(rng: random.Random, tasks: list[dict], count: int = 50) -> list[dict]:
    now = datetime(2026, 9, 11, 21, 0, 0)
    rows: list[dict] = []
    for i in range(count):
        asset_type, label, mime, width, height, duration = rng.choice(ASSET_SPECS)
        created = now - timedelta(days=rng.randrange(0, 14), minutes=rng.randrange(0, 1440))
        review = rng.choices(["approved", "pending", "rejected"], weights=[6, 3, 1])[0]
        ext = {"image": "jpg", "video": "mp4", "copywriting": "txt", "spreadsheet": "xlsx", "report": "pdf"}[asset_type]
        rows.append(
            {
                "asset_no": f"AS{created.strftime('%y%m%d')}{i + 1:04d}",
                "task_index": rng.randrange(1, len(tasks) + 1),
                "product_index": rng.randrange(1, 61),
                "asset_type": asset_type,
                "name": f"{label}_{i + 1:03d}.{ext}",
                "storage_backend": "local",
                "storage_path": f"generated/{created.strftime('%Y%m%d')}/{asset_type}/{i + 1:04d}.{ext}",
                "mime_type": mime,
                "size_bytes": rng.randrange(60_000, 18_000_000),
                "width": width,
                "height": height,
                "duration_seconds": duration,
                "source": rng.choices(["generated", "uploaded"], weights=[8, 2])[0],
                "generator": rng.choice(["creative", "video", "analytics"]),
                "prompt": (
                    f"秋季女装{ rng.choice(FABRICS) }{rng.choice(CATEGORIES['女装'])}，"
                    f"{rng.choice(STYLES)}风格，模特实拍，柔和自然光，纯色背景"
                ),
                "review_status": review,
                "review_note": (
                    "手部比例不自然，已重绘" if review == "rejected"
                    else None
                ),
                "regenerate_count": rng.randrange(0, 3) if review != "approved" else 0,
                "meta": {"model": "mock-image-v1", "seed": rng.randrange(1, 10**6)},
                "created_at": created,
            }
        )
    return rows


def make_ad_campaigns(rng: random.Random, count: int = 12) -> list[dict]:
    """广告计划。

    ``BAD_ROI_RATIO`` 比例的计划被强制灌成亏损状态（ROI < 1.0），
    广告投放 Agent 跑起来才能真的触发"自动停投"这条规则。
    """
    now = datetime(2026, 9, 11, 21, 0, 0)
    bad_quota = max(1, int(count * BAD_ROI_RATIO))
    rows: list[dict] = []

    for i in range(count):
        is_bad = i < bad_quota
        spend = Decimal(rng.randrange(200_00, 3000_00)) / 100
        if is_bad:
            # 花得多、回得少 —— ROI 0.35 ~ 0.95
            revenue = (spend * Decimal(str(round(rng.uniform(0.35, 0.95), 2)))).quantize(Decimal("0.01"))
            status = rng.choice(["running", "paused"])
        else:
            revenue = (spend * Decimal(str(round(rng.uniform(1.8, 5.2), 2)))).quantize(Decimal("0.01"))
            status = rng.choices(["running", "paused", "ended", "pending_approval"], weights=[5, 2, 2, 2])[0]

        clicks = int(spend * Decimal(rng.randrange(8, 30)) / 10)
        impressions = clicks * rng.randrange(18, 60)

        rows.append(
            {
                "campaign_no": f"AC{now.strftime('%y%m')}{i + 1:03d}",
                "task_index": rng.randrange(1, 21),
                "product_index": rng.randrange(1, 61),
                "platform": rng.choice(["mock", "juliang"]),
                "name": f"{rng.choice(SEASONS)}{rng.choice(CATEGORIES['女装'])}-拉新计划{i + 1:02d}",
                "objective": rng.choice(["gmv", "roi", "clicks", "uv"]),
                "budget_daily": Decimal(rng.randrange(200_00, 5000_00)) / 100,
                "bid": Decimal(rng.randrange(50, 480)) / 100,
                "start_at": now - timedelta(days=rng.randrange(3, 30)),
                "end_at": now + timedelta(days=rng.randrange(1, 20)),
                "status": status,
                "spend": spend,
                "impressions": impressions,
                "clicks": clicks,
                "conversions": int(clicks * rng.uniform(0.02, 0.11)),
                "revenue": revenue,
                "roi": (revenue / spend).quantize(Decimal("0.01")),
                "target_audience": {
                    "age": rng.choice(["18-24", "25-30", "30-35"]),
                    "gender": "female",
                    "cities": rng.sample(["一线", "新一线", "二线"], k=2),
                },
                "creative_asset_nos": [f"AS{rng.randrange(10**7, 10**8 - 1):08d}" for _ in range(2)],
                "approved_by": rng.choice([None, "user"]) if status != "pending_approval" else None,
                "approved_at": (now - timedelta(days=rng.randrange(1, 10))) if status != "pending_approval" else None,
                "reject_reason": None,
                "created_at": now - timedelta(days=rng.randrange(15, 40)),
                "updated_at": now - timedelta(hours=rng.randrange(1, 72)),
            }
        )
    return rows


def make_email_messages(rng: random.Random, count: int = 30) -> list[dict]:
    """邮件记录，包含被闸门拦下来的那部分。

    ``gate_decision='blocked_*'`` 的记录是面试时的好素材 ——
    可以直接讲"这封邮件为什么被拦下来"，而不是空口说"我做了安全策略"。
    """
    now = datetime(2026, 9, 11, 21, 0, 0)
    subjects = [
        "【日报】昨日经营数据与异常提醒",
        "【周报】本周选品与投放复盘",
        "【告警】广告计划 AC2609002 ROI 跌破 1.0，已自动暂停",
        "【通知】3 款秋季女装已选中，等待您的确认",
        "【报表】选品分析报告.xlsx",
    ]
    rows: list[dict] = []
    for i in range(count):
        created = now - timedelta(days=rng.randrange(0, 20), minutes=rng.randrange(0, 1440))
        decision = rng.choices(
            ["allowed", "dry_run", "blocked_whitelist", "blocked_ratelimit", "blocked_size"],
            weights=[58, 18, 12, 8, 4],
        )[0]
        blocked = decision.startswith("blocked")
        subject = rng.choice(subjects)
        rows.append(
            {
                "message_no": f"EM{created.strftime('%y%m%d')}{i + 1:04d}",
                "task_index": rng.randrange(1, 21),
                "direction": rng.choices(["outbound", "inbound"], weights=[8, 2])[0],
                "to_addrs": [f"ops{rng.randrange(1, 9)}@example.com"],
                "cc_addrs": ["boss@example.com"] if rng.random() < 0.3 else None,
                "bcc_addrs": None,
                "from_addr": "agent@fashionops.local",
                "subject": subject,
                "body_text": f"{subject}\n\n（本邮件由邮件通知 Agent 自动生成，数据截至 {created:%Y-%m-%d %H:%M}）",
                "body_html": None,
                "attachments": (
                    [
                        {
                            "filename": "选品分析报告.xlsx",
                            "asset_no": f"AS{rng.randrange(10**7, 10**8 - 1):08d}",
                            "size": rng.randrange(20_000, 2_000_000),
                        }
                    ]
                    if rng.random() < 0.4
                    else None
                ),
                "status": "blocked" if blocked else rng.choice(["sent", "draft", "failed"]),
                "gate_decision": decision,
                "gate_reason": {
                    "blocked_whitelist": "收件人 not-in-whitelist：external@gmail.com",
                    "blocked_ratelimit": "同一收件人 1 小时内已发 3 封，超过上限",
                    "blocked_size": "附件合计 24.8MB 超过 20MB 上限",
                    "dry_run": "dry_run 模式：仅记录不投递",
                }.get(decision),
                "retry_count": rng.randrange(0, 3) if rng.random() < 0.15 else 0,
                "sent_at": created + timedelta(seconds=rng.randrange(1, 30)) if not blocked else None,
                "error_message": "SMTP 421 Service not available" if rng.random() < 0.08 else None,
                "created_at": created,
            }
        )
    return rows


TOOLS = [
    ("erp.query_inventory", "read", "inventory"),
    ("erp.query_orders", "read", "orders"),
    ("ecom.query_competitors", "read", "competitors"),
    ("ecom.create_listing", "write", "listing"),
    ("ad.create_campaign", "write", "campaigns"),
    ("ad.update_budget", "write", "campaigns"),
    ("kb.search", "read", "kb_articles"),
    ("report.export_excel", "execute", "assets"),
    ("mail.send", "execute", "email_messages"),
    ("vector.upsert", "write", "qdrant"),
]


def make_audit_logs(rng: random.Random, count: int = 300) -> list[dict]:
    now = datetime(2026, 9, 11, 21, 0, 0)
    rows: list[dict] = []
    for _ in range(count):
        tool_name, action, resource = rng.choice(TOOLS)
        created = now - timedelta(days=rng.randrange(0, 14), minutes=rng.randrange(0, 1440))
        status = rng.choices(["success", "error", "blocked"], weights=[84, 11, 5])[0]
        rows.append(
            {
                "trace_id": f"{rng.randrange(16**15, 16**16 - 1):x}",
                "task_index": rng.choice([None, rng.randrange(1, 21)]),
                "actor": rng.choice(AGENT_NAMES + ["user", "scheduler"]),
                "tool_name": tool_name,
                "action": action,
                "resource": resource,
                "args_digest": {"tool": tool_name, "sample_args": {"limit": rng.randrange(1, 200)}},
                "result_summary": (
                    f"返回 {rng.randrange(1, 500)} 条记录" if status == "success" else None
                ),
                "status": status,
                "error_code": (
                    rng.choice(["RATE_LIMITED", "CIRCUIT_OPEN", "PERMISSION_DENIED", "TOOL_TIMEOUT"])
                    if status != "success"
                    else None
                ),
                "duration_ms": rng.randrange(3, 12000),
                "ip": "127.0.0.1",
                "created_at": created,
            }
        )
    return rows


# ===========================================================================
# 对话演示数据
# ===========================================================================
#: 三段"真实会发生"的对话。刻意覆盖三种结局：
#: ① 一问一答（查数据直接答）  ② 调工具后建任务  ③ 停在人工闸口等确认
CHAT_SCRIPTS = [
    {
        "title": "最近退货是不是变高了？",
        "created_days_ago": 0,
        "messages": [
            ("user", "最近退货是不是变高了？帮我看看哪个类目最严重。", None),
            (
                "tool",
                "erp.query_sales_summary 返回：近 30 天共 322 单，退款 ¥19,710.42；"
                "连衣裙类目退货率 21.4%，直播渠道 40.0%（超 30% 红线）。",
                "erp.query_sales_summary",
            ),
            (
                "assistant",
                "是变高了。近 30 天整体退货率 15.1%，比上月的 12.6% 上升 2.5 个百分点。\n\n"
                "最严重的是**连衣裙**（21.4%），其中**直播渠道**达到 40%，已经越过 30% 红线。"
                "这两个数字都来自 ERP 汇总，不是估算。\n\n"
                "要我做一次归因分析的话，我可以出一份带报表的数据分析任务，"
                "你到任务页看进度就行。",
                None,
            ),
        ],
    },
    {
        "title": "帮我选 3 款秋季女装并出报表",
        "created_days_ago": 2,
        "messages": [
            ("user", "帮我选 3 款秋季女装，再出一份选品报表。", None),
            (
                "tool",
                "task.submit 返回 task_no=T260909001，状态 pending，已入队后台执行。",
                "task.submit",
            ),
            (
                "assistant",
                "已经建了一个任务（T260909001），它会先选品、再出报表。\n\n"
                "这类「先做 A 再拿 A 的结果做 B」的事我不在对话里一步步跑 ——"
                "任务有进度、有断点、有产物，去**任务页**看更合适。",
                None,
            ),
        ],
    },
    {
        "title": "把这份结论发给运营组",
        "created_days_ago": 1,
        "messages": [
            ("user", "把这份结论整理一下发给运营组，抄送商品部。", None),
            (
                "assistant",
                "我准备发一封邮件给运营组（抄送商品部），主题是"
                "「【经营日报】近 30 天数据与退货率告警」。\n\n"
                "邮件发出去就收不回来，所以系统会**先拦下来等你确认**，"
                "你点同意之后才真正发送。",
                None,
            ),
            (
                "assistant",
                "（等待你确认：收件人 ops@fashionops.com、cc merchandising@fashionops.com）",
                "mail.send",
            ),
        ],
        # 第三条会话停在人工闸口 —— 对话页要能看到一张待确认的卡片
        "pending_approval": {
            "tool_name": "mail.send",
            "subject": "【经营日报】近 30 天数据与退货率告警",
            "to_addrs": ["ops@fashionops.com"],
            "cc_addrs": ["merchandising@fashionops.com"],
            "body_text": (
                "各位好：\n\n"
                "近 30 天 GMV ¥130,226.43，订单 322 单，退款 ¥19,710.42。\n"
                "需要重点关注：连衣裙类目退货率 21.4%，直播渠道 40.0%，已超过 30% 红线。\n\n"
                "完整报表见附件。\n\n商品运营中台"
            ),
        },
    },
]


def make_chat_demo(rng: random.Random, *, now: datetime | None = None) -> dict:
    """生成对话演示数据：会话 + 消息 +（可选）一张待确认的审批卡。

    ## 为什么连对话也要灌假数据

    演示时光秃秃一个空对话框，看的人只会觉得"又一个聊天壳子"。
    灌进去的这三条**刻意覆盖了三种结局**（直接答 / 建任务 / 停在闸口），
    正好对应 `chat/agent.py` 模块头部写的那棵决策树 ——
    看演示的人不用读代码就能明白"什么情况走哪条路"。

    返回的是**纯 dict**，不碰数据库（和别的生成器一样，方便单测）。
    """
    now = now or datetime(2026, 9, 11, 21, 0, 0)
    conversations: list[dict] = []

    for index, script in enumerate(CHAT_SCRIPTS, start=1):
        created = now - timedelta(days=script["created_days_ago"], hours=rng.randrange(0, 10))
        messages = []
        tokens = 0
        tool_calls = 0

        for seq, (role, content, tool_name) in enumerate(script["messages"], start=1):
            is_tool = role == "tool"
            prompt_tokens = rng.randrange(400, 3200) if role == "assistant" else rng.randrange(20, 160)
            completion_tokens = rng.randrange(60, 900) if role == "assistant" else 0
            tokens += prompt_tokens + completion_tokens
            if is_tool:
                tool_calls += 1
            messages.append(
                {
                    "seq": seq,
                    "role": role,
                    "content": content,
                    "tool_call_id": f"call_{index}{seq:02d}" if (is_tool or tool_name) else None,
                    "tool_name": tool_name,
                    "tool_arguments": None,
                    "tool_result": {"ok": True} if is_tool else None,
                    "tool_ok": True if is_tool else None,
                    "tool_duration_ms": rng.randrange(120, 6500) if is_tool else None,
                    "model": "glm-4-flash" if role == "assistant" else None,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost": Decimal(str(round((prompt_tokens * 1e-6 + completion_tokens * 2e-6) * 7.2, 6))),
                    "latency_ms": rng.randrange(600, 12000) if role == "assistant" else None,
                    "finish_reason": (
                        "tool_calls" if tool_name else ("stop" if role == "assistant" else None)
                    ),
                    "interrupted": False,
                    "created_at": created + timedelta(seconds=seq * 7),
                }
            )

        conversations.append(
            {
                # 对外编号形如 CH2609110003 —— 不用自增 id 对外（会泄漏"总共有多少会话"）
                "conversation_no": f"CH{created.strftime('%y%m%d')}{index:04d}",
                "title": script["title"],
                "status": "active",
                "username": "demo",
                "model": "glm-4-flash",
                "message_count": len(messages),
                "llm_calls": sum(1 for m in messages if m["role"] == "assistant"),
                "tool_calls": tool_calls,
                "total_tokens": tokens,
                "total_cost": Decimal(str(round(tokens * 1.4e-6, 6))),
                "last_message_at": messages[-1]["created_at"],
                "created_at": created,
                "updated_at": messages[-1]["created_at"],
                "messages": messages,
                "pending_approval": script.get("pending_approval"),
            }
        )

    return {"conversations": conversations}
