import { useEffect, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { api, type DailyMetric, type Health, type LowStockItem, type Summary } from '../api'
import { PageHeader, pct, StatCard, yuan } from '../ui'

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [metrics, setMetrics] = useState<DailyMetric[]>([])
  const [lowStock, setLowStock] = useState<LowStockItem[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.health(), api.summary(), api.dailyMetrics({ days: 30 }), api.lowStock(5)])
      .then(([h, s, m, l]) => {
        setHealth(h)
        setSummary(s)
        setMetrics(m)
        setLowStock(l)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

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

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '数据看板']}
        title="数据看板"
        badge={<span className="tag accent">近 30 天</span>}
        desc="经营概览 · 假数据已刻意埋入异常，用于演示熔断与转人工"
      />

      {error && <div className="error-box">{error}</div>}

      {health && (
        <div className="card" style={{ marginBottom: 14 }}>
          <h3>
            依赖连通性{' '}
            <span className={`badge ${health.status === 'ok' ? 'ok' : 'warn'}`}>{health.status}</span>
          </h3>
          <div className="grid grid-4">
            {health.dependencies.map((dep) => (
              <div key={dep.name}>
                <div className="stat-label">{dep.name}</div>
                <div style={{ fontSize: 13 }}>
                  <span className={`badge ${dep.ok ? 'ok' : 'danger'}`}>{dep.ok ? '正常' : '异常'}</span>{' '}
                  <span className="muted">{dep.detail}</span>
                </div>
                {dep.latency_ms !== null && <div className="stat-sub">延迟 {dep.latency_ms.toFixed(1)} ms</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <StatCard
          label="近 30 天 GMV"
          value={summary ? yuan(summary.gmv_30d) : '—'}
          sub={`订单 ${summary?.orders ?? '—'} 单`}
          icon="trend-up"
        />
        <StatCard
          label="退货率"
          value={summary ? pct(summary.return_rate_30d) : '—'}
          sub="服装行业普遍 25%~40%"
          icon="alert"
        />
        <StatCard
          label="广告 ROI"
          value={summary ? Number(summary.ad_roi_30d ?? 0).toFixed(2) : '—'}
          sub={`花费 ${summary ? yuan(summary.ad_spend_30d) : '—'}`}
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
                </tr>
              </thead>
              <tbody>
                {lowStock.slice(0, 10).map((item) => (
                  <tr key={item.sku_code}>
                    <td className="mono">{item.sku_code}</td>
                    <td>{item.product_title}</td>
                    <td className="muted">
                      {item.color} / {item.size}
                    </td>
                    <td>
                      <span className="badge danger">{item.available}</span>
                    </td>
                  </tr>
                ))}
                {lowStock.length === 0 && (
                  <tr>
                    <td colSpan={4} className="empty">
                      没有告急库存
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  )
}
