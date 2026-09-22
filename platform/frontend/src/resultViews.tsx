/**
 * 工具结果的"富视图"。
 *
 * ## 为什么渲染工具返回，而不是让模型写卡片
 *
 * 对话里最容易被糊弄的地方是数字。"退货率 15.1%" 这句话，
 * 可能是工具查出来的，也可能是模型顺口编的 —— 而两者在界面上长得一模一样。
 *
 * 所以这里**只渲染工具返回的原始 JSON**：模型说错话不影响卡片上的数字，
 * 卡片上的每个数都能在 `tool_result` 里找到出处。模型负责解释，
 * 数字负责真实。这也是这个系统唯一说得通的取舍。
 *
 * ## 为什么按工具名硬编码
 *
 * 试过"通用 JSON 渲染"：把任意 JSON 递归画成树。结果是所有工具长得一样，
 * 而 `query_sales_summary` 的 `groups` 该画成图，`rank_candidates` 的
 * `selected` 该带分数和理由 —— 通用渲染两头都做不到。
 * 每个工具一张视图，多写几十行，换来的是"这个数据本来就该这么看"。
 *
 * 认不出的工具会返回 null，退化成什么都不加 —— 新工具上线不会白屏。
 */
import type { ReactNode } from 'react'
import ReactECharts from 'echarts-for-react'
import { Icon } from './ui'
import { num, pct, yuan } from './ui'

type Dict = Record<string, unknown>

// ---------------------------------------------------------------- 取值助手
/*
 * 后端的 tool_result 是 JSON，类型上只是 unknown。这里全部走防御式取值：
 * 字段缺失/类型不对时返回 null，视图降级成"—"，而不是把 NaN 或者
 * "undefined" 显示给运营看。
 */
function asDict(v: unknown): Dict {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Dict) : {}
}

function asList(v: unknown): Dict[] {
  return Array.isArray(v)
    ? v.filter((x): x is Dict => !!x && typeof x === 'object' && !Array.isArray(x))
    : []
}

function s(v: unknown, fallback = '—'): string {
  if (v === null || v === undefined || v === '') return fallback
  return String(v)
}

function f(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/**
 * 把可能是"JSON 字符串"的 payload 统一成对象。
 *
 * `chat_messages.tool_result` 是 JSON 列，正常情况下 API 会给出对象；
 * 但同一份数据经 MySQL 原始查询或某些序列化路径时是字符串。
 * 多这一层解析，视图不会因为"拿到的是字符串"就整块消失。
 */
function toDict(v: unknown): Dict {
  if (typeof v === 'string') {
    try {
      return asDict(JSON.parse(v))
    } catch {
      return {}
    }
  }
  return asDict(v)
}

const CHART_TEXT = { fontSize: 10, color: '#8a97ab' }
const CHART_GRID_LINE = { lineStyle: { color: '#eef2f9' } }

// ---------------------------------------------------------------- 小组件
function Tile({
  k,
  v,
  d,
  tone,
}: {
  k: string
  v: string
  d?: string
  tone?: 'ok' | 'warn' | 'danger'
}) {
  return (
    <div className={`kpi-tile${tone ? ` ${tone}` : ''}`}>
      <div className="k">{k}</div>
      <div className="v">{v}</div>
      {d && <div className="d">{d}</div>}
    </div>
  )
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  if (rows.length === 0) return <div className="empty">暂无数据</div>
  return (
    <div className="result-table-wrap">
      <table>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------- 销售汇总
function SalesSummaryView({ data }: { data: Dict }) {
  const totals = asDict(data.totals)
  const allGroups = asList(data.groups)
  const groupBy = s(data.group_by, 'category')
  const days = f(data.days)
  const groupLabel = groupBy === 'day' ? '日期' : groupBy === 'channel' ? '渠道' : '类目'

  const orders = f(totals.order_count)
  // 整体退货率要**重新按订单数加权**：接口给的 groups 里每条都有 return_rate，
  // 但对它们取平均是错的（各分组订单量差很多），必须回到底层计数重算
  const refundCount = allGroups.reduce((acc, g) => acc + (f(g.refund_count) ?? 0), 0)
  const overallRate = orders && orders > 0 ? refundCount / orders : null

  // 趋势图必须按时间升序 —— 接口是**按 GMV 降序**返回的，
  // 直接画会得到一条来回跳的折线
  const rows =
    groupBy === 'day'
      ? [...allGroups].sort((a, b) => s(a.group).localeCompare(s(b.group)))
      : allGroups.slice(0, 12)

  const option =
    groupBy === 'day'
      ? {
          grid: { left: 6, right: 6, top: 36, bottom: 0, containLabel: true },
          tooltip: { trigger: 'axis' },
          legend: {
            top: 0,
            icon: 'roundRect',
            itemWidth: 10,
            itemHeight: 10,
            textStyle: { fontSize: 11, color: '#475569' },
          },
          xAxis: {
            type: 'category',
            data: rows.map((r) => s(r.group).slice(5)),
            axisLine: { lineStyle: { color: '#e7edf6' } },
            axisTick: { show: false },
            axisLabel: CHART_TEXT,
          },
          yAxis: [
            {
              type: 'value',
              name: 'GMV',
              nameTextStyle: CHART_TEXT,
              splitLine: CHART_GRID_LINE,
              axisLabel: CHART_TEXT,
            },
            {
              type: 'value',
              name: '退货率',
              nameTextStyle: CHART_TEXT,
              splitLine: { show: false },
              axisLabel: { ...CHART_TEXT, formatter: '{value}%' },
            },
          ],
          series: [
            {
              name: 'GMV',
              type: 'bar',
              barMaxWidth: 18,
              itemStyle: { color: '#93b4f8', borderRadius: [4, 4, 0, 0] },
              data: rows.map((r) => f(r.gmv) ?? 0),
            },
            {
              name: '退货率',
              type: 'line',
              yAxisIndex: 1,
              smooth: true,
              symbolSize: 5,
              lineStyle: { width: 2 },
              itemStyle: { color: '#d97706' },
              data: rows.map((r) => ((f(r.return_rate) ?? 0) * 100).toFixed(1)),
            },
          ],
        }
      : {
          grid: { left: 6, right: 20, top: 10, bottom: 0, containLabel: true },
          tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
          xAxis: { type: 'value', splitLine: CHART_GRID_LINE, axisLabel: CHART_TEXT },
          yAxis: {
            type: 'category',
            // 横向柱状图的类目要**从下往上**排，最大值才落在顶部：
            // yAxis 的 data 与 series 的 data 都要 reverse，只转一个会对不上号
            data: rows.map((r) => s(r.group)).reverse(),
            axisLine: { lineStyle: { color: '#e7edf6' } },
            axisTick: { show: false },
            axisLabel: { fontSize: 11, color: '#475569' },
          },
          series: [
            {
              name: 'GMV',
              type: 'bar',
              barMaxWidth: 16,
              itemStyle: { color: '#2563eb', borderRadius: [0, 4, 4, 0] },
              data: rows.map((r) => f(r.gmv) ?? 0).reverse(),
            },
          ],
        }

  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="dashboard" size={15} />
          销售汇总
        </span>
        <div className="result-tags">
          {days && <span className="tag">近 {days} 天</span>}
          <span className="tag accent">按{groupLabel}</span>
          <span className="tag">{allGroups.length} 组</span>
        </div>
      </div>

      <div className="result-kpis">
        <Tile k="GMV" v={yuan(f(totals.gmv) ?? 0)} />
        <Tile k="订单数" v={num(orders ?? 0)} />
        <Tile k="客单价" v={yuan(f(totals.avg_order_value) ?? 0)} />
        <Tile k="退款金额" v={yuan(f(totals.refund_amount) ?? 0)} tone="danger" />
        <Tile k="整体退货率" v={overallRate === null ? '—' : pct(overallRate, 1)} tone="warn" />
      </div>

      {rows.length > 0 && (
        <div className="result-chart">
          <ReactECharts option={option} style={{ height: 240 }} notMerge />
        </div>
      )}

      <Table
        head={[groupLabel, '订单数', '客户数', 'GMV', '客单价', '退款金额', '退货率']}
        rows={rows.map((r) => [
          s(r.group),
          num(f(r.order_count) ?? 0),
          num(f(r.customer_count) ?? 0),
          yuan(f(r.gmv) ?? 0),
          yuan(f(r.avg_order_value) ?? 0),
          yuan(f(r.refund_amount) ?? 0),
          (f(r.return_rate) ?? 0) >= 0.3 ? (
            <span className="badge danger">{pct(f(r.return_rate), 1)}</span>
          ) : (
            pct(f(r.return_rate), 1)
          ),
        ])}
      />
    </div>
  )
}

// ---------------------------------------------------------------- 库存
function InventoryView({ data }: { data: Dict }) {
  const items = asList(data.items)
  const total = items.reduce((acc, it) => acc + (f(it.available) ?? 0), 0)
  const out = items.filter((it) => (f(it.available) ?? 0) <= 0).length
  const low = items.filter((it) => {
    const a = f(it.available) ?? 0
    return a > 0 && a < 10
  }).length

  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="product" size={15} />
          库存查询
        </span>
        <div className="result-tags">
          <span className="tag">{items.length} 个 SKU</span>
        </div>
      </div>

      <div className="result-kpis">
        <Tile k="可用合计" v={num(total)} />
        <Tile k="缺货" v={num(out)} tone={out > 0 ? 'danger' : undefined} />
        <Tile k="低于 10 件" v={num(low)} tone={low > 0 ? 'warn' : undefined} />
      </div>

      <Table
        head={['SKU', '商品', '颜色', '尺码', '可用']}
        rows={items.map((it) => {
          const available = f(it.available) ?? 0
          return [
            <span className="mono">{s(it.sku_code)}</span>,
            s(it.product_title),
            s(it.color),
            s(it.size),
            available <= 0 ? (
              <span className="badge danger">缺货</span>
            ) : available < 10 ? (
              <span className="badge warn">{available}</span>
            ) : (
              num(available)
            ),
          ]
        })}
      />
    </div>
  )
}

// ---------------------------------------------------------------- 候选商品
function CandidatesView({ data }: { data: Dict }) {
  const rows = asList(data.candidates)
  const margins = rows.map((r) => f(r.margin_rate)).filter((v): v is number => v !== null)
  const avgMargin = margins.length > 0 ? margins.reduce((a, b) => a + b, 0) / margins.length : null

  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="product" size={15} />
          候选商品池
        </span>
        <div className="result-tags">
          <span className="tag">{f(data.count) ?? rows.length} 个 SPU</span>
        </div>
      </div>

      <div className="result-kpis">
        <Tile k="候选数" v={num(f(data.count) ?? rows.length)} />
        <Tile k="平均毛利率" v={avgMargin === null ? '—' : pct(avgMargin, 1)} />
      </div>

      <Table
        head={['SPU', '标题', '类目', '吊牌价', '毛利率', '评分', '库存', '近 30 天销量']}
        rows={rows.slice(0, 20).map((r) => [
          <span className="mono">{s(r.spu_code)}</span>,
          s(r.title),
          s(r.category),
          yuan(f(r.list_price) ?? 0),
          pct(f(r.margin_rate), 1),
          f(r.rating) === null ? '—' : (f(r.rating) as number).toFixed(1),
          num(f(r.available_stock) ?? 0),
          num(f(r.sold_30d) ?? 0),
        ])}
      />
      {rows.length > 20 && <div className="result-more">仅显示前 20 行，共 {rows.length} 行</div>}
    </div>
  )
}

// ---------------------------------------------------------------- 选品打分
function RankView({ data }: { data: Dict }) {
  const selected = asList(data.selected)
  const rejected = asList(data.rejected_samples)

  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="sparkle" size={15} />
          选品打分结果
        </span>
        <div className="result-tags">
          <span className="tag">候选池 {num(f(data.total_input) ?? 0)}</span>
          <span className="tag accent">通过 {num(f(data.passed) ?? 0)}</span>
        </div>
      </div>

      <div className="result-kpis">
        <Tile k="候选池" v={num(f(data.total_input) ?? 0)} />
        <Tile k="通过硬约束" v={num(f(data.passed) ?? 0)} tone="ok" />
        <Tile k="被过滤" v={num(f(data.filtered_out) ?? 0)} tone="warn" />
        <Tile k="选出" v={num(selected.length)} />
      </div>

      {typeof data.summary === 'string' && <div className="result-note">{data.summary}</div>}

      <div className="rank-list">
        {selected.map((item, i) => {
          const score = f(item.total_score)
          return (
            <div className="rank-item" key={`${s(item.spu_code)}-${i}`}>
              <div className="rank-badge">{f(item.rank) ?? i + 1}</div>
              <div className="rank-body">
                <div className="rank-title">
                  {s(item.title)}
                  <span className="mono rank-spu">{s(item.spu_code)}</span>
                  {score !== null && <span className="rank-score">{score.toFixed(2)} 分</span>}
                </div>
                {/* reason 是**代码**生成的加分理由（每条对得回具体数字），
                    不是模型写的推荐语 */}
                <div className="rank-reason">{s(item.reason, '')}</div>
                <div className="rank-metrics">
                  <span className="tag">毛利率 {pct(f(item.margin_rate), 1)}</span>
                  <span className="tag">库存 {num(f(item.available_stock) ?? 0)}</span>
                  <span className="tag">近 30 天销量 {num(f(item.sold_30d) ?? 0)}</span>
                  <span className="tag">吊牌价 {yuan(f(item.list_price) ?? 0)}</span>
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {rejected.length > 0 && (
        <details className="result-details">
          <summary>被硬约束挡掉的 {num(f(data.filtered_out) ?? rejected.length)} 个（样例）</summary>
          <Table
            head={['SPU', '标题', '原因']}
            rows={rejected.map((r) => [
              <span className="mono">{s(r.spu_code)}</span>,
              s(r.title),
              <span className="muted">{s(r.detail ?? r.reason)}</span>,
            ])}
          />
        </details>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 经营告警
const ALERT_TONE: Record<string, string> = {
  critical: 'danger',
  warning: 'warn',
  info: 'dim',
}

function AlertsView({ data }: { data: Dict }) {
  const alerts = asList(data.alerts)
  const critical = f(data.critical_count) ?? 0
  const warning = f(data.warning_count) ?? 0

  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="alert" size={15} />
          经营告警
        </span>
        <div className="result-tags">
          <span className="tag accent">阈值由代码判定</span>
        </div>
      </div>

      <div className="result-kpis">
        <Tile k="严重" v={num(critical)} tone={critical > 0 ? 'danger' : undefined} />
        <Tile k="警告" v={num(warning)} tone={warning > 0 ? 'warn' : undefined} />
        <Tile k="合计" v={num(alerts.length)} />
      </div>

      <div className="alert-list">
        {alerts.map((a, i) => (
          <div className="alert-row" key={`${s(a.code)}-${i}`}>
            <span className={`badge ${ALERT_TONE[s(a.level)] ?? 'dim'}`}>
              {s(a.level) === 'critical' ? '严重' : s(a.level) === 'warning' ? '警告' : '提示'}
            </span>
            <span className="alert-dim">{s(a.dimension)}</span>
            <span className="alert-msg">{s(a.message)}</span>
            {f(a.value) !== null && (
              <span className="alert-val">
                {s(a.metric)} {(f(a.value) as number).toFixed(2)}
                {f(a.threshold) !== null && <span className="muted"> / 阈值 {f(a.threshold)}</span>}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 知识库检索
function KbSearchView({ data }: { data: Dict }) {
  const items = asList(data.items)

  // 检索没命中是**正常结果**，不是错误：这时候要明确说"库里没有"，
  // 而不是渲染一张空卡片让模型自己去圆
  if (data.found === false || items.length === 0) {
    return (
      <div className="result-card">
        <div className="result-head">
          <span className="result-title">
            <Icon name="book" size={15} />
            知识库检索
          </span>
          <div className="result-tags">
            <span className="badge warn">无命中</span>
          </div>
        </div>
        <div className="result-note">{s(data.message ?? data.hint, '知识库里没有相关内容')}</div>
      </div>
    )
  }

  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="book" size={15} />
          知识库命中
        </span>
        <div className="result-tags">
          <span className="tag">{f(data.count) ?? items.length} 条</span>
        </div>
      </div>

      <div className="cite-list">
        {items.map((it, i) => (
          <div className="cite-item" key={`${s(it.article_no)}-${i}`}>
            <div className="cite-head">
              <span className="badge accent">{s(it.article_no)}</span>
              <span className="cite-title">{s(it.title)}</span>
              <span className="tag">{s(it.category)}</span>
              {f(it.score) !== null && (
                <span className="cite-score">相似度 {(f(it.score) as number).toFixed(3)}</span>
              )}
            </div>
            <div className="cite-body">{s(it.content)}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 知识库类目
function KbCategoriesView({ data }: { data: Dict }) {
  const cats = asList(data.categories)
  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="tag" size={15} />
          知识库类目
        </span>
        <div className="result-tags">
          <span className="tag">共 {num(f(data.total_articles) ?? 0)} 篇</span>
        </div>
      </div>
      <div className="chip-row">
        {cats.map((c, i) => (
          <span className="tag" key={i}>
            {s(c.category)} · {num(f(c.articles) ?? 0)}
          </span>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 报表产物
function ReportView({ data }: { data: Dict }) {
  const size = f(data.size_bytes)
  const sheets = Array.isArray(data.sheet_names) ? (data.sheet_names as unknown[]).map(String) : []
  const url = typeof data.url === 'string' ? data.url : null

  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="book" size={15} />
          报表已生成
        </span>
        <div className="result-tags">
          {typeof data.asset_no === 'string' && data.asset_no !== '' && (
            <span className="badge ok">{data.asset_no}</span>
          )}
        </div>
      </div>

      <div className="artifact-list">
        <div className="artifact">
          <Icon name="book" size={16} />
          <span className="name">{s(data.filename)}</span>
          {size !== null && <span className="muted">{Math.round(size / 1024)} KB</span>}
          {url && (
            <a className="btn small" href={url} target="_blank" rel="noreferrer">
              下载
            </a>
          )}
        </div>
      </div>

      {sheets.length > 0 && (
        <div className="chip-row" style={{ marginTop: 10 }}>
          {sheets.map((name) => (
            <span className="tag" key={name}>
              {name}
            </span>
          ))}
        </div>
      )}

      {/* 静默截断比截断本身更糟：让人以为看的是全量数据 */}
      {Array.isArray(data.truncated) && data.truncated.length > 0 && (
        <div className="result-note" style={{ marginTop: 10 }}>
          ⚠ 部分工作表超过行数上限已截断：
          {asList(data.truncated)
            .map((t) => `${s(t.sheet)} 保留 ${s(t.kept)} 行，丢弃 ${s(t.dropped)} 行`)
            .join('；')}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 技能加载
/**
 * `skill.load` 的结果。
 *
 * 对话链是**按需加载**技能的（系统提示里只给索引），所以这张卡片出现
 * 本身就是一条信息：**模型判断这次要按规范办事，于是去读了规范**。
 * 把它渲染出来，比让用户看一坨 JSON 强 —— "它到底照着什么做的"应该看得见。
 */
function SkillView({ data }: { data: Dict }) {
  if (data.found !== true) {
    return (
      <div className="result-card">
        <div className="result-head">
          <span className="result-title">
            <Icon name="book" size={15} />
            技能加载
          </span>
          <div className="result-tags">
            <span className="badge warn">没有这个技能</span>
          </div>
        </div>
        {/* hint 是给模型看的下一步建议，顺便也让人能看出"可用技能有哪些" */}
        <div className="result-note">{s(data.message ?? data.hint)}</div>
      </div>
    )
  }

  const tools = Array.isArray(data.tools) ? (data.tools as unknown[]).map(String) : []

  return (
    <div className="result-card">
      <div className="result-head">
        <span className="result-title">
          <Icon name="book" size={15} />
          {s(data.title)}
        </span>
        <div className="result-tags">
          <span className="tag accent">{s(data.name)}</span>
          {tools.length > 0 && <span className="tag">用到 {tools.length} 个工具</span>}
        </div>
      </div>

      <div className="cite-body" style={{ maxHeight: 320 }}>
        {s(data.content)}
      </div>

      {tools.length > 0 && (
        <div className="chip-row" style={{ marginTop: 10 }}>
          {tools.map((tool) => (
            <span className="tag" key={tool}>
              {tool}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 注册表
/*
 * 工具名 → 视图。以**工具名**而不是返回结构做键：
 * 同一个工具换实现（比如换成真实 ERP）返回结构可能变，但卡片该长什么样
 * 是由"这个工具在业务上是什么"决定的，不是由 JSON 形状决定的。
 */
const VIEWS: Record<string, (data: Dict) => ReactNode> = {
  'erp.query_sales_summary': (d) => <SalesSummaryView data={d} />,
  'erp.query_inventory': (d) => <InventoryView data={d} />,
  'erp.query_candidates': (d) => <CandidatesView data={d} />,
  'selection.rank_candidates': (d) => <RankView data={d} />,
  'analytics.detect_alerts': (d) => <AlertsView data={d} />,
  'kb.search': (d) => <KbSearchView data={d} />,
  'kb.categories': (d) => <KbCategoriesView data={d} />,
  'report.export_excel': (d) => <ReportView data={d} />,
  'analytics.render_business_report': (d) => <ReportView data={d} />,
  'skill.load': (d) => <SkillView data={d} />,
}

/** 渲染某个工具返回的富卡片；没有对应视图时返回 null（退化成只有工具芯片）。 */
export function ToolResultView({ tool, data }: { tool: string | null; data: unknown }) {
  if (!tool) return null
  const view = VIEWS[tool]
  if (!view) return null

  /*
   * 网关把工具输出统一包成 `{ok, tool, data, cached?}`
   * （见后端 `gateway/errors.py` 的 `ToolResult.to_dict`），
   * 业务字段在 `data` 里面 —— 直接当成原始结果读会全部取空。
   */
  const payload = toDict(data)
  // 失败的结果没有 data，卡片画不出任何东西。失败信息由工具芯片表达，
  // 这里不要重复一张空卡片。
  if (payload.ok !== true) return null

  // 早期落库的工具消息只有 {"ok": true} 这种裸壳（没有 data）。
  // 取不到业务字段就直接不渲染，退化成只有工具芯片 —— 比画一张全是"—"的
  // 空卡片诚实。
  const inner = toDict(payload.data)
  if (Object.keys(inner).length === 0) return null
  return view(inner)
}
