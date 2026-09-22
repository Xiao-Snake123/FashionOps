---
name: business-review
title: 经营分析
summary: 拉销售汇总 → 代码判阈值告警 → 出 Excel 报表，并解释告警的业务含义（含小样本规则）
todo_kind: analytics
tools:
  - erp.query_sales_summary
  - analytics.detect_alerts
  - analytics.render_business_report
---

**先调 `erp.query_sales_summary` 查两次**：`group_by=category` 和 `group_by=channel` 各一次。
days 用用户说的时间范围，没说就用 30。

**把 totals 和两个 groups 传给 `analytics.detect_alerts` 拿告警结论**，
再调 `analytics.render_business_report` 生成 Excel（两次查询结果原样传进去，不要自己改数字）。

## 严重程度以工具返回的 level 为准，**不要自己判断**
阈值是代码算的，参数是：
* 退货率 ≥ {{ return_rate_critical }} → critical；≥ {{ return_rate_warning }} → warning
* 退款金额占 GMV ≥ {{ refund_ratio_warning }} → 告警
* 单一渠道 GMV 占比 ≥ {{ channel_share_warning }} → 渠道集中风险
* 分组客单价低于整体 {{ aov_gap_warning }} → 客单价缺口

**为什么要写成硬性规则**：实测把这条阈值写在提示词里，模型照样给 14.69% 的退货率
标了 critical。数值判断是纯粹的算术，交给人以外的执行者只会得到不可复现的结果。

## 一条最容易被忽略的规则：小样本不下结论
**分组订单数 < {{ min_orders_for_judgement }} 单时，只报事实、不下判定。**
3 单退 1 单 = 33%，按阈值算是 critical，但那大概率只是噪声。
`detect_alerts` 对这类分组只返回 `insufficient_sample`，
**不要把它当成趋势去解读，也不要自己补一句"需要关注"** ——
用统计学上没有意义的数字指挥业务，比没有结论更糟。

## 结论里必须有
整体 GMV、订单数、最值得注意的一到两条告警（引用工具给的数值和 level），
以及报表的 asset_no 和下载地址。

## 报表说明
三张工作表（类目 / 渠道 / 整体）的列和数字格式已经定好了，**不要自己拼表**。
返回里带 `truncated` 说明有工作表超行数被截断 —— 这件事要**主动告诉用户**，
静默截断会让人以为看的是全量数据。
