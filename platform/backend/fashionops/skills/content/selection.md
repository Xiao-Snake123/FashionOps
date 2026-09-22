---
name: sku-selection
title: 选品打分
summary: 按硬约束过滤 + 五维加权打分从候选池里选出推荐款，给出可逐条核对的推荐理由
todo_kind: selection
tools:
  - erp.query_candidates
  - selection.rank_candidates
  - erp.query_inventory
---

**先调 `erp.query_candidates` 拉候选**：把用户给的条件填进 category / season，limit 用默认值
（默认值就是"一次拿够决策所需的数据集"，不要为了省事调小）。

**再把返回的 `candidates` 原样传给 `selection.rank_candidates`**，top_n 用用户要的款数。
"原样"是硬要求：不要自己先筛一遍、也不要只挑几个传过去 ——
硬约束和打分都在那一步里做，漏传会让本该入选的款凭空消失，而且不报错。

## 硬约束（不满足直接出局，不参与打分）
* 可用库存 ≥ {{ min_stock }} 件 —— 再好的款没货也白搭
* 毛利率 ≥ {{ min_margin_rate }}
* 状态不能是：{{ exclude_statuses }}

## 五维权重（分数由代码算，**不是模型打的**）
动销 {{ weight_sell_through }} · 毛利 {{ weight_margin }} · 价格空间 {{ weight_price_gap }}
· 评分 {{ weight_rating }} · 库存深度 {{ weight_stock_depth }}

## 结论怎么写
* 列出选中的款式：spu_code、总分、一两句推荐理由
* 理由必须引用返回里的具体数字（销量、毛利率、库存），**不要自己造数字**
* 有商品被挡掉就简单说明原因 —— 原因照抄 `rejected_samples` 里的 `reason`，不要自己编

## 两种要如实说的失败
* 候选池为空 → 直接说"没有符合条件的候选"，**不要降级到凭感觉推荐**
* 全部被硬约束挡掉 → 说出挡掉的原因，并指出需要放宽哪条约束（比如降低库存下限）
