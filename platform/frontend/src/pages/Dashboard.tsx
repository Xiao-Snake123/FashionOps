import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactECharts from 'echarts-for-react'
import {
  api,
  type DailyMetric,
  type Health,
  type LowStockItem,
  type ReturnRateByCategory,
  type Summary,
  type Task,
} from '../api'
import { currentUserSnapshot } from '../auth'
import { PageHeader, pct, StatCard, yuan } from '../ui'

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [metrics, setMetrics] = useState<DailyMetric[]>([])
  const [lowStock, setLowStock] = useState<LowStockItem[]>([])
  const [failedTasks, setFailedTasks] = useState<Task[]>([])
  const [losingCount, setLosingCount] = useState(0)
  const [returnRate, setReturnRate] = useState<ReturnRateByCategory | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState<string | null>(null)

  const user = currentUserSnapshot()
  const isAdmin = user?.is_superuser ?? false

  const load = useCallback(() => {
    Promise.all([
      api.health(),
      api.summary(),
      api.dailyMetrics({ days: 30 }),
      api.lowStock(5),
      api.tasks({ status: 'failed', page_size: 5 }),
      api.campaigns({ only_losing: true, page_size: 1 }),
      // 类目退货率失败只影响那一张图，不该让整个看板打不开
      api.returnRateByCategory(30).catch(() => null),
    ])
      .then(([h, s, m, l, ft, lc, rr]) => {
        setHealth(h)
        setSummary(s)
        setMetrics(m)
        setLowStock(l)
        setFailedTasks(ft.items)
        setLosingCount(lc.total)
        setReturnRate(rr)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const retryTask = useCallback(
    async (t: Task) => {
      setRetrying(t.task_no)
      try {
        // 后端约定：传已存在的 task_no 即重跑该任务（见 tasks.py 的 submit 注释）
        await api.submitTask({ user_request: t.user_request, task_no: t.task_no })
        load()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setRetrying(null)
      }
    },
    [load],
  )

  // 同源同口径：GMV / 广告花费直接用日趋势求和，卡片和曲线永远是一个数，
  // 不会再出现"卡片 ¥0、曲线却有值"的两头打架。
  const gmv30d = metrics.length > 0
    ? metrics.reduce((s, m) => s + Number(m.gmv || 0), 0)
    : Number(summary?.gmv_30d || 0)
  const spend30d = metrics.length > 0
    ? metrics.reduce((s, m) => s + Number(m.ad_spend || 0), 0)
    : Number(summary?.ad_spend_30d || 0)

  const chartOption = {
    grid: { left: 52, right: 22, top: 30, bottom: 30 },
    tooltip: { trigger: 'axis' },
    legend: { data: ['GMV', '广告花费'], textStyle: { color: '#8b9ab3' }, top: 0 },
    xAxis: {
      type: 'category',
      data: metrics.map((m) => m.metric_date.slice(5)),
      axisLine: { lineStyle: { color: '#2a3548' } },
      axisLabel: { color: '#8b9ab3', fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#8b9ab3', fontSize: 11 },
      splitLine: { lineStyle: { color: 'rgba(42,53,72,0.5)' } },
    },
    series: [
      {
        name: 'GMV',
        type: 'line',
        smooth: true,
        showSymbol: false,
        areaStyle: { opacity: 0.13 },
        lineStyle: { width: 2, color: '#4d8cf5' },
        itemStyle: { color: '#4d8cf5' },
        data: metrics.map((m) => Number(m.gmv)),
      },
      {
        name: '广告花费',
        type: 'line',
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1.5, color: '#e8b23a' },
        itemStyle: { color: '#e8b23a' },
        data: metrics.map((m) => Number(m.ad_spend)),
      },
    ],
  }

  // 退货率按类目看才有意义：整体一个数会被大类目平均掉，
  // 而真正要处理的是"哪一个类目在超基准"。
  const baseline = returnRate?.baseline
  const returnChartOption = returnRate
    ? {
        grid: { left: 46, right: 18, top: 24, bottom: 52 },
        tooltip: {
          trigger: 'axis',
          valueFormatter: (v: number | null) => (v === null ? '—' : `${(v * 100).toFixed(2)}%`),
        },
        xAxis: {
          type: 'category',
          data: returnRate.items.map((i) => i.category),
          axisLine: { lineStyle: { color: '#2a3548' } },
          axisLabel: { color: '#8b9ab3', fontSize: 11, interval: 0, rotate: 24 },
        },
        yAxis: {
          type: 'value',
          axisLabel: {
            color: '#8b9ab3',
            fontSize: 11,
            formatter: (v: number) => `${(v * 100).toFixed(0)}%`,
          },
          splitLine: { lineStyle: { color: 'rgba(42,53,72,0.5)' } },
        },
        series: [
          {
            name: '退货率',
            type: 'bar',
            barWidth: '55%',
            data: returnRate.items.map((i) => ({
              value: i.return_rate ?? 0,
              // 超基准的标红。**判定由后端给**（over_baseline），
              // 前端不自己比 25%~40% —— 那份区间只有后端一个来源
              itemStyle: { color: i.over_baseline ? '#ef5f6b' : '#4d8cf5' },
            })),
            markArea: {
              silent: true,
              itemStyle: { color: 'rgba(232,178,58,0.10)' },
              data: [
                [
                  { yAxis: returnRate.baseline.low, name: '行业基准' },
                  { yAxis: returnRate.baseline.high },
                ],
              ],
            },
          },
        ],
      }
    : null

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '数据看板']}
        title="数据看板"
        badge={<span className="tag accent">近 30 天</span>}
        desc="经营概览 · 数据每 10 分钟刷新"
        actions={
          <button type="button" className="button" onClick={load}>
            立即刷新
          </button>
        }
      />

      {error && <div className="error-box">{error}</div>}

      {/* 风险聚合横幅：把散在各页的"需要处理"汇总成一行，每条直接跳转 */}
      <div className="risk-banner">
        <b>⚠ 需要处理</b>
        <span>
          库存告急 <b>{summary?.stockout_skus ?? '—'}</b> 个 SKU
          <Link to="/products">去补货 →</Link>
        </span>
        <span>
          失败任务 <b>{summary?.failed_tasks ?? '—'}</b> 个
          <Link to="/tasks">去重跑 →</Link>
        </span>
        <span>
          转人工工单 <b>{summary?.escalated_tickets ?? '—'}</b> 条
          <Link to="/tickets">去处理 →</Link>
        </span>
        <span>
          亏损广告计划 <b>{losingCount}</b> 个
          <Link to="/campaigns">去止损 →</Link>
        </span>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <StatCard
          label="近 30 天 GMV"
          value={yuan(gmv30d)}
          sub={metrics.length > 0 ? '与趋势图同源同口径' : `订单 ${summary?.orders ?? '—'} 单`}
          icon="trend-up"
        />
        <StatCard
          label="退货率"
          value={summary ? pct(summary.return_rate_30d) : '—'}
          sub={
            baseline
              ? `行业基准 ${(baseline.low * 100).toFixed(0)}%–${(baseline.high * 100).toFixed(0)}%`
              : '行业基准 25%–40%'
          }
          icon="alert"
        />
        <StatCard
          label="广告 ROI"
          value={summary ? Number(summary.ad_roi_30d ?? 0).toFixed(2) : '—'}
          sub={`花费 ${yuan(spend30d)}`}
          icon="campaign"
        />
        <StatCard
          label="库存告急 SKU"
          value={summary?.stockout_skus ?? '—'}
          sub="选品 Agent 会被硬约束过滤掉"
          tone="danger"
          icon="alert"
        />
        <StatCard
          label="商品 / SKU"
          value={`${summary?.products ?? '—'} / ${summary?.skus ?? '—'}`}
          sub="在售 / 全部规格"
          icon="product"
        />
        <StatCard
          label="待处理工单"
          value={summary?.open_tickets ?? '—'}
          sub="等待客服 Agent 接管"
          icon="ticket"
        />
        <StatCard
          label="已转人工"
          value={summary?.escalated_tickets ?? '—'}
          sub="情绪激动 / 涉及金额纠纷"
          tone="warn"
          icon="alert"
        />
        <StatCard
          label="任务"
          value={`${summary?.running_tasks ?? 0} 运行 / ${summary?.failed_tasks ?? 0} 失败`}
          sub="失败任务用于错误归因"
          icon="tasks"
        />
      </div>

      <div className="grid grid-2">
        <div className="card">
          <h3>近 30 天 GMV 与广告花费</h3>
          {metrics.length > 0 ? (
            <ReactECharts option={chartOption} style={{ height: 268 }} notMerge />
          ) : (
            <div className="empty">暂无数据</div>
          )}
        </div>

        <div className="card">
          <h3>库存告急 Top 10</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>SKU</th>
                  <th>商品</th>
                  <th>规格</th>
                  <th>可售</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {lowStock.slice(0, 10).map((item) => {
                  const out = item.available === 0
                  return (
                    <tr key={item.sku_code}>
                      <td className="mono">{item.sku_code}</td>
                      <td>{item.product_title}</td>
                      <td className="muted">
                        {item.color} / {item.size}
                      </td>
                      <td>
                        <span className={`badge ${out ? 'danger' : 'warn'}`}>{item.available}</span>
                      </td>
                      <td className="muted">{out ? '已断货' : '告急'}</td>
                      <td>
                        <Link className="button small" to="/products">
                          补货申请
                        </Link>
                      </td>
                    </tr>
                  )
                })}
                {lowStock.length === 0 && (
                  <tr>
                    <td colSpan={6} className="empty">
                      没有告急库存
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* 退货率按类目看才有意义：整体一个数会被大类目平均掉，
          真正要处理的是"哪一个类目在超基准"。独占一行是因为它横向要放得下标 */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>
          各类目退货率
          {returnRate && (
            <span className="badge warn" style={{ marginLeft: 8 }}>
              {returnRate.items.filter((i) => i.over_baseline).length} 个超基准
            </span>
          )}
        </h3>
        {returnChartOption ? (
          <>
            <ReactECharts option={returnChartOption} style={{ height: 240 }} notMerge />
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              近 {returnRate?.days} 天，退货率 = 退款额 ÷ GMV（先聚合再相除，不是日均值）。
              黄色带是行业基准区间，超出即标红 —— 区间由后端给出，前端不另存一份。
            </div>
          </>
        ) : (
          <div className="empty">暂无数据</div>
        )}
      </div>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>
            失败任务 <span className="badge danger">{summary?.failed_tasks ?? failedTasks.length}</span>
          </h3>
          {failedTasks.length === 0 ? (
            <div className="empty">暂无失败任务</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>原因</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {failedTasks.map((t) => (
                    <tr key={t.task_no}>
                      <td>
                        <Link to={`/tasks/${t.task_no}`}>{t.title}</Link>
                      </td>
                      <td className="muted" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {t.error_message || '未知错误'}
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <button
                          type="button"
                          className="button small"
                          disabled={retrying === t.task_no}
                          onClick={() => void retryTask(t)}
                        >
                          {retrying === t.task_no ? '提交中…' : '重跑'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* 系统状态：基础设施明细（qdrant 地址、向量数、延迟）只给管理员看 */}
        <div className="card">
          <h3>系统状态</h3>
          {health ? (
            <div className="sys-status">
              {health.dependencies.map((dep) => (
                <div key={dep.name} className="sys-status-row">
                  <span className={`badge ${dep.ok ? 'ok' : 'danger'}`}>{dep.ok ? '正常' : '异常'}</span>
                  <span style={{ flex: 1 }}>{dep.name}</span>
                  {isAdmin && (
                    <span className="muted" style={{ fontSize: 12 }}>
                      {dep.detail}
                      {dep.latency_ms !== null && ` · ${dep.latency_ms.toFixed(0)}ms`}
                    </span>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="empty">加载中…</div>
          )}
        </div>
      </div>
    </>
  )
}
