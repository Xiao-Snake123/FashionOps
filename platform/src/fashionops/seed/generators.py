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

TASK_REQUESTS = [
    ("帮我选 3 款秋季女装并出分析报告", "new_product_launch"),
    ("分析昨天各品类销售情况，出日报", "daily_report"),
    ("给秋季连衣裙做一组主图和小红书文案", "new_product_launch"),
    ("抖音新款连衣裙投放 ROI 掉了，帮我看看要不要停", "ad_optimize"),
    ("处理今天积压的售后工单", "after_sale"),
    ("生成上周店铺经营周报并邮件发我", "daily_report"),
    ("帮我把这款风衣上架到抖音小店", "new_product_launch"),
    ("最近退货率是不是变高了？做个归因", "daily_report"),
]


def make_tasks(rng: random.Random, count: int = 20) -> list[dict]:
    now = datetime(2026, 9, 11, 21, 0, 0)
    rows: list[dict] = []
    for i in range(count):
        request, task_type = rng.choice(TASK_REQUESTS)
        created = now - timedelta(days=rng.randrange(0, 14), hours=rng.randrange(0, 12))
        status = rng.choices(
            ["succeeded", "running", "waiting_approval", "failed", "pending", "cancelled"],
            weights=[48, 14, 12, 12, 8, 6],
        )[0]
        dag = NEW_PRODUCT_DAG if task_type == "new_product_launch" else DAILY_REPORT_DAG

        progress = {
            "succeeded": 100,
            "running": rng.randrange(15, 85),
            "waiting_approval": rng.randrange(40, 70),
            "failed": rng.randrange(10, 80),
            "pending": 0,
            "cancelled": rng.randrange(0, 50),
        }[status]
        finished = status in ("succeeded", "failed", "cancelled")

        rows.append(
            {
                "task_no": f"T{created.strftime('%y%m%d')}{i + 1:03d}",
                "title": request[:40],
                "user_request": request,
                "task_type": task_type,
                "status": status,
                "current_node": (
                    "review" if status == "succeeded"
                    else "approve" if status == "waiting_approval"
                    else rng.choice([n["id"] for n in dag["nodes"]]) if status in ("running", "failed")
                    else None
                ),
                "progress": progress,
                "priority": rng.randrange(1, 9),
                "dag": dag,
                "state": {"user_request": request, "step": progress // 10},
                "result": (
                    {"summary": "已选出 3 款秋季女装，综合得分 87.4/82.1/79.6", "assets": 3}
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
                "thread_id": f"T{created.strftime('%y%m%d')}{i + 1:03d}",
                "approval_payload": (
                    {"action": "调整广告日预算", "from": 800, "to": 1500, "reason": "ROI 3.8 高于阈值"}
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
    return rows


EVENT_TEMPLATES = [
    ("node_start", "info", "节点开始执行"),
    ("tool_call", "info", "调用工具 mock_erp.query_inventory"),
    ("llm_call", "info", "模型返回结构化结果"),
    ("node_end", "info", "节点执行完成"),
]


def make_task_events(rng: random.Random, tasks: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for task_index, task in enumerate(tasks, start=1):
        nodes = [n["id"] for n in task["dag"]["nodes"]]
        seq = 0
        base = task["created_at"]
        for node in nodes:
            seq += 1
            rows.append(
                {
                    "task_index": task_index,
                    "seq": seq,
                    "event_type": "node_start",
                    "node_name": node,
                    "agent_name": next(
                        (n["agent"] for n in task["dag"]["nodes"] if n["id"] == node), None
                    ),
                    "level": "info",
                    "message": f"开始执行节点 {node}",
                    "payload": None,
                    "duration_ms": None,
                    "created_at": base + timedelta(seconds=seq * 12),
                }
            )

            for _ in range(rng.randrange(1, 4)):
                seq += 1
                event_type, level, message = rng.choice(EVENT_TEMPLATES)
                rows.append(
                    {
                        "task_index": task_index,
                        "seq": seq,
                        "event_type": event_type,
                        "node_name": node,
                        "agent_name": None,
                        "level": level,
                        "message": message,
                        "payload": {"elapsed_step": seq},
                        "duration_ms": rng.randrange(80, 9000),
                        "created_at": base + timedelta(seconds=seq * 12),
                    }
                )

            seq += 1
            ok = not (task["status"] == "failed" and node == task["current_node"])
            rows.append(
                {
                    "task_index": task_index,
                    "seq": seq,
                    "event_type": "node_end" if ok else "error",
                    "node_name": node,
                    "agent_name": None,
                    "level": "info" if ok else "error",
                    "message": f"节点 {node} 完成" if ok else f"节点 {node} 失败：工具调用超时",
                    "payload": None,
                    "duration_ms": rng.randrange(200, 24000),
                    "created_at": base + timedelta(seconds=seq * 12),
                }
            )

            if node == "approve":
                seq += 1
                rows.append(
                    {
                        "task_index": task_index,
                        "seq": seq,
                        "event_type": "interrupt",
                        "node_name": node,
                        "agent_name": "human_gate",
                        "level": "warn",
                        "message": "高危操作已挂起，等待人工确认",
                        "payload": task.get("approval_payload"),
                        "duration_ms": None,
                        "created_at": base + timedelta(seconds=seq * 12),
                    }
                )
            if not ok:
                break
    return rows


AGENT_NAMES = [
    "orchestrator",
    "selection",
    "creative",
    "video",
    "listing",
    "ads",
    "service",
    "analytics",
    "notification",
]


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
                    "node_name": rng.choice([n["id"] for n in task["dag"]["nodes"]]),
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
