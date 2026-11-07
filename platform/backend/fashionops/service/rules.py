"""客服的**判据** —— 分流、意图、情绪、转人工，全部是代码。

## 为什么这些必须是代码，不能交给模型

"转人工"是一个**业务决策**，不是一次语言创作。它必须满足三个条件：

1. **可审计** —— 出事时要能回答"这条为什么没转人工"。模型给的理由是
   一段自由文本，没法回归、没法对账。
2. **可回归** —— 改了规则之后要能验证"以前转的现在还转"。
   写在 prompt 里的话，改一个词就可能让一批工单的行为静默变化。
3. **可解释** —— 运营要能自己看懂规则，不然他们不会信任它。

所以分工是：**代码决定"要不要转"，模型决定"话怎么说"**。

## 快车道判不准就**不许**走快车道

`decide_lane` 只在**高置信**时才放行进快车道。方向是刻意的：

* 快车道**误放**的代价：用模板回复了一条本该人工处理的问题 ——
  客户收到牛头不对马嘴的回复，而系统以为处理完了
* 快车道**误拦**的代价：这条工单多走一次模型调用（多花几厘钱）

代价完全不对等，所以宁可多走 Agent 层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ===========================================================================
# 1. 意图关键词
# ===========================================================================
#: 意图 → 关键词。**命中数**决定意图，不是"命中第一个就算"。
#:
#: 写法上有个必须小心的点：关键词要挑**没有歧义**的。
#: 例如"破损"不能放进 `质量投诉` —— 因为"包裹破损"（快递盒压扁）属于物流问题，
#: 放进去就会把物流类工单误判成质量投诉。这类冲突只能靠在语料上逐条核对发现。
INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "物流查询": ("物流", "快递", "运单", "转运", "签收", "派送", "到哪", "什么时候能到", "包裹"),
    "催发货": ("催", "还没发货", "什么时候发", "赶紧发", "说好", "48 小时", "发货慢"),
    "退换货": ("退货", "换货", "退款", "换大", "换小", "换成", "换一", "不要了", "退掉"),
    "尺码咨询": (
        "尺码",
        "建议选",
        "选什么码",
        "身高",
        "体重",
        "含毛量",
        "面料",
        "尺寸",
        "偏大",
        "偏小",
        "怎么选",
        "推荐",
    ),
    "质量投诉": ("质量", "破洞", "掉色", "起球", "开线", "投诉", "假货", "接缝", "瑕疵", "有洞"),
    "发票": ("发票", "开票", "抬头", "税号", "增值税"),
    "支付": ("优惠券", "支付", "结算", "付款", "扣款", "用不了", "不满足使用条件"),
}

#: 允许写进库的意图。模型如果给出这个词表以外的值，会被打回。
ALLOWED_INTENTS: frozenset[str] = frozenset(INTENT_PATTERNS)

#: **可以用模板回复**的意图。挑的标准是：答案**不依赖具体商品或订单细节**，
#: 是一段通用说明。反过来"退货怎么处理"这种就要看订单状态，不能模板化。
FAST_LANE_INTENTS: frozenset[str] = frozenset({"物流查询", "催发货", "发票"})

#: 快车道至少要命中几个关键词才算"看得准"。
#: 1 个太松（"包裹"两个字就能进），2 个才说明这条工单确实在讲那件事。
FAST_LANE_MIN_HITS = 2


# ===========================================================================
# 2. 情绪
# ===========================================================================
ANGRY_WORDS: tuple[str, ...] = (
    "投诉",
    "太差",
    "必须给个说法",
    "什么破",
    "垃圾",
    "骗子",
    "谁赔",
    "很不爽",
    "没人理我",
    "曝光",
    "差评",
)
NEGATIVE_WORDS: tuple[str, ...] = (
    "差太多",
    "不满意",
    "破损",
    "折痕",
    "慢了",
    "没动",
    "一直",
    "失望",
    "不行",
    "问题",
    "没发货",
    "退",
    "紧",
)
POSITIVE_WORDS: tuple[str, ...] = ("谢谢", "满意", "很好", "不错", "棒")

#: 涉及钱/纠纷的信号。**这是转人工最重要的一条判据** ——
#: 金额纠纷一旦处理错是真赔钱的，不是体验问题。
MONEY_DISPUTE_WORDS: tuple[str, ...] = (
    "赔",
    "退款",
    "退钱",
    "损失",
    "投诉",
    "纠纷",
    "假货",
    "维权",
    "曝光",
    "工商",
    "12315",
)

#: 明确要人工的信号
HUMAN_REQUEST_WORDS: tuple[str, ...] = ("人工", "真人", "转人工", "找你们领导", "投诉客服")


def classify_intent(text: str) -> tuple[str, int]:
    """按关键词命中数判意图。

    返回 ``(intent, hits)``。**平票或零命中时返回 ``("", 0)``**。

    ## 平票必须当作"不认识"，不能随便挑一个

    初版写的是 "命中数更大就胜出"，于是平票时**胜利者取决于字典顺序** ——
    那不是判据，是巧合。实测就错了 2 条：

        第一个客服说 24 小时回复，结果两天了没人理我，我要投诉！
        催发货 命中「两天了」= 1 ；质量投诉 命中「投诉」= 1 ← 平票
        → 按字典顺序返回了"催发货"，与标注不符

    这类 bug 很阴：改个无关的代码（比如调整字典里的顺序）就会让结果变。
    所以平票一律返回空，让上层走 Agent 层去判 —— 和分流那条原则一致：
    **判不准就不许自作主张**。
    """
    lowered = text.lower()
    counts = {intent: sum(1 for word in words if word in lowered) for intent, words in INTENT_PATTERNS.items()}
    top = max(counts.values(), default=0)
    if top == 0:
        return "", 0
    winners = [intent for intent, count in counts.items() if count == top]
    if len(winners) > 1:
        return "", 0
    return winners[0], top


def classify_sentiment(text: str) -> str:
    """情绪。顺序有意义：先看强烈的，再看负面的。

    判据是**关键词命中数**而不是"命中就返回" ——
    "退款"这种词在很多正常咨询里也会出现，单靠一个词定 angty 会误伤。
    """
    angry = sum(1 for word in ANGRY_WORDS if word in text)
    negative = sum(1 for word in NEGATIVE_WORDS if word in text)
    positive = sum(1 for word in POSITIVE_WORDS if word in text)

    if angry >= 1 or negative >= 3:
        return "angry"
    if negative >= 1:
        return "negative"
    if positive >= 1 and negative == 0:
        return "positive"
    return "neutral"


# ===========================================================================
# 3. 分流
# ===========================================================================
@dataclass(frozen=True)
class Triage:
    """一条工单的分流结果。"""

    intent: str
    intent_hits: int
    sentiment: str
    lane: str  # "fast" | "agent"
    lane_reason: str


def decide_lane(*, text: str, intent: str, intent_hits: int, sentiment: str) -> tuple[str, str]:
    """走快车道还是交给 Agent。返回 ``(lane, 原因)``。

    原因要一起返回：出问题时"它为什么走了这条路"必须能直接回答，
    而不是靠人反推规则。
    """
    if not intent:
        return "agent", "关键词没命中任何意图，判不出来"
    if intent not in FAST_LANE_INTENTS:
        return "agent", f"意图「{intent}」不在可模板化的白名单里"
    if intent_hits < FAST_LANE_MIN_HITS:
        return "agent", f"意图「{intent}」只命中 {intent_hits} 个关键词，不够确定"
    if sentiment == "angry":
        # 只排 anggry，不排 negative。
        #
        # 初版把 negative 也排掉了，结果快车道只放行 15% —— 因为真实语料里
        # "物流三天没动"这类咨询**天然就是 negative**，那就等于快车道基本不工作。
        #
        # 放宽的依据：模板里本来就写了"抱歉让你久等"，轻度不满是能接住的。
        # 而 anggry（投诉/要求赔偿/威胁曝光）接不住 —— 那种必须人来处理。
        return "agent", "情绪是 angry，不走模板"
    if any(word in text for word in MONEY_DISPUTE_WORDS):
        return "agent", "内容涉及钱/纠纷，不能模板化"
    return "fast", f"意图「{intent}」且情绪 {sentiment}，可模板化"


def triage(*, subject: str, content: str) -> Triage:
    """把分流的三步串起来。``subject`` 和 ``content`` 一起看 ——

    只给内容的话会漏信息：有些工单的意图写在标题里（"发票怎么开" → "公司采购需要开增值税普通发票"）。
    """
    text = f"{subject} {content}"
    intent, hits = classify_intent(text)
    sentiment = classify_sentiment(text)
    lane, reason = decide_lane(text=text, intent=intent, intent_hits=hits, sentiment=sentiment)
    return Triage(intent=intent, intent_hits=hits, sentiment=sentiment, lane=lane, lane_reason=reason)


# ===========================================================================
# 4. 转人工复核（**最后一道，两层之后都跑**）
# ===========================================================================
#: 低于这个自评置信度就转人工。0.6 是"宁可多转"的位置 ——
#: 转人工的代价是人力，不转的代价是客户收到错答案还投诉。
CONFIDENCE_FLOOR = 0.6


@dataclass(frozen=True)
class EscalationDecision:
    escalated: bool
    reason: str


def review_escalation(
    *,
    text: str,
    intent: str,
    sentiment: str,
    confidence: float | None,
    cited_article_nos: list[str],
    reply_draft: str,
    agent_proposed: bool = False,
) -> EscalationDecision:
    """转人工的**最终判定**。所有理由都会被收集起来，不是命中一条就返回。

    全部收集的理由：给运营看的是一份完整的"为什么转人工"，
    只报第一条会让人以为其他条件都满足了。

    ## 这条规则存在的意义

    它是**最后一道**，跑在快车道和 Agent 层之后。也就是说：

    * 快车道再便宜，也绕不过它 —— 安全不能因为"这条路便宜"而降低
    * Agent 层再自信，也只能**提议**转人工，不能决定不转
    """
    reasons: list[str] = []

    if any(word in text for word in HUMAN_REQUEST_WORDS):
        reasons.append("客户明确要求人工")
    if sentiment == "angry":
        reasons.append("客户情绪激动")
    if intent in ("质量投诉", "退换货"):
        # 退货涉及钱和库存，而且答不答得准**取决于订单状态**
        # （是否在退换期、商品是否影响二次销售、钱退到哪了）—— 这些要查库，
        # 模板和模型都答不准，必须人来确认。
        reasons.append(f"{intent}类，答案依赖订单状态且涉及钱，必须人工确认")
    if any(word in text for word in MONEY_DISPUTE_WORDS):
        reasons.append("涉及金额/纠纷，误判会真赔钱")
    if not cited_article_nos:
        reasons.append("知识库没有命中依据，不能凭空回复")
    if confidence is not None and confidence < CONFIDENCE_FLOOR:
        reasons.append(f"自评置信度 {confidence:.2f} 低于阈值 {CONFIDENCE_FLOOR}")
    if not reply_draft.strip():
        reasons.append("没有生成可用草稿")
    if agent_proposed:
        reasons.append("处理方主动建议人工介入")

    # 去重保序：上面的条件会重叠（情绪激动 + 涉及金额经常同时成立），
    # 重复的理由会让运营以为看到了两件事
    unique = list(dict.fromkeys(reasons))
    return EscalationDecision(escalated=bool(unique), reason="；".join(unique))


# ===========================================================================
# 5. 快车道的模板回复
# ===========================================================================
#: 意图 → 模板。**模板里只放通用说明，不放需要查库才能知道的事**。
#:
#: 真实系统里物流状态要调快递公司的接口（这个项目没有），
#: 所以这里明确写"已为你查询"而不是假装知道"包裹现在在杭州"——
#: **编一个物流状态比说"我去查"糟糕得多**。
FAST_LANE_TEMPLATES: dict[str, str] = {
    "物流查询": (
        "您好，已收到你的物流咨询。\n\n"
        "我们已把你的订单同步给物流方核实，正常情况下 24 小时内会更新轨迹。\n"
        "如果超过 48 小时仍无更新，我们会主动帮你发起查件，你无需重复联系。\n\n"
        "给你带来的等待我们很抱歉。"
    ),
    "催发货": (
        "您好，抱歉让你久等。\n\n"
        "你的订单我们已经帮你标记加急，发货后会第一时间推送运单号给你。\n"
        "按平台承诺，超过 48 小时未发货可以申请相应赔付，你不需要额外操作。\n\n"
        "感谢你的耐心。"
    ),
    "发票": (
        "您好，开票流程如下：\n\n"
        "1. 在订单详情页选择「申请开票」\n"
        "2. 填写抬头、税号（企业）或姓名（个人）\n"
        "3. 电子发票一般 1 个工作日内发到你填写的邮箱，纸质发票随包裹寄出\n\n"
        "如果需要开增值税专用发票，请提供完整开票资料，我们会单独处理。"
    ),
}


def render_fast_reply(*, intent: str, order_no: str | None, cited_titles: list[str]) -> str:
    """拼出快车道回复：模板 + 订单号 + 知识库依据。"""
    body = FAST_LANE_TEMPLATES.get(intent, "您好，我们已收到你的咨询。")
    if order_no:
        body = f"（订单号：{order_no}）\n\n{body}"
    if cited_titles:
        # 附上依据不是形式主义：客户拿这几条去对质时，客服要能立刻说出
        # "这是按《XX》第几条回的" —— 没有依据的回复在纠纷里等于没说过
        body += "\n\n——\n依据：" + "、".join(f"《{t}》" for t in cited_titles)
    return body


# ===========================================================================
# 6. 文本里捞订单号
# ===========================================================================
_ORDER_RE = re.compile(r"\b([A-Z]{2}\d{8,})\b")


def extract_order_no(*texts: str) -> str | None:
    """从工单正文里捞订单号（客户经常自己贴上来）。"""
    for text in texts:
        if not text:
            continue
        match = _ORDER_RE.search(text)
        if match:
            return match.group(1)
    return None


__all__ = [
    "ALLOWED_INTENTS",
    "CONFIDENCE_FLOOR",
    "FAST_LANE_INTENTS",
    "FAST_LANE_MIN_HITS",
    "FAST_LANE_TEMPLATES",
    "INTENT_PATTERNS",
    "EscalationDecision",
    "Triage",
    "classify_intent",
    "classify_sentiment",
    "decide_lane",
    "extract_order_no",
    "render_fast_reply",
    "review_escalation",
    "triage",
]
