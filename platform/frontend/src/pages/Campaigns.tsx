import { useEffect, useState } from 'react'
import { api, type Campaign } from '../api'
import { num, PageHeader, StatusBadge, yuan } from '../ui'

export default function Campaigns() {
  const [items, setItems] = useState<Campaign[]>([])
  const [total, setTotal] = useState(0)
  const [losingCount, setLosingCount] = useState(0)
  const [onlyLosing, setOnlyLosing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.campaigns({ page_size: 50, only_losing: onlyLosing }), api.campaigns({ page_size: 1, only_losing: true })])
      .then(([page, losing]) => {
        setItems(page.items)
        setTotal(page.total)
        setLosingCount(losing.total)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [onlyLosing])

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '广告投放']}
        title="广告投放"
        badge={<span className="badge danger">{losingCount} 个亏损</span>}
        desc={
          <>
            共 {total} 个计划，其中 <span style={{ color: 'var(--danger)' }}>{losingCount} 个 ROI 低于 1.0（亏损）</span>。
            广告投放 Agent 的自动停投规则就是抓这个信号；改预算属于高危操作，必须走人工确认闸口。
          </>
        }
      />

      {error && <div className="error-box">{error}</div>}

      <div className="toolbar">
        <label style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <input type="checkbox" checked={onlyLosing} onChange={(e) => setOnlyLosing(e.target.checked)} />
          只看亏损计划
        </label>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>计划号</th>
                <th>名称</th>
                <th>平台</th>
                <th>目标</th>
                <th>日预算</th>
                <th>状态</th>
                <th>花费</th>
                <th>点击</th>
                <th>成交</th>
                <th>收入</th>
                <th>ROI</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const roi = row.roi === null ? null : Number(row.roi)
                const losing = roi !== null && roi < 1
                return (
                  <tr key={row.campaign_no} style={losing ? { background: 'rgba(239,95,107,0.06)' } : undefined}>
                    <td className="mono">{row.campaign_no}</td>
                    <td>{row.name}</td>
                    <td className="muted">{row.platform}</td>
                    <td className="muted">{row.objective}</td>
                    <td>{yuan(row.budget_daily)}</td>
                    <td>
                      <StatusBadge status={row.status} />
                    </td>
                    <td>{yuan(row.spend)}</td>
                    <td>{num(row.clicks)}</td>
                    <td>{num(row.conversions)}</td>
                    <td>{yuan(row.revenue)}</td>
                    <td>
                      {roi === null ? (
                        '—'
                      ) : (
                        <span className={`badge ${losing ? 'danger' : roi > 3 ? 'ok' : 'warn'}`}>
                          {roi.toFixed(2)}
                          {losing ? ' 亏损' : ''}
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })}
              {items.length === 0 && (
                <tr>
                  <td colSpan={11} className="empty">
                    没有投放计划
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
