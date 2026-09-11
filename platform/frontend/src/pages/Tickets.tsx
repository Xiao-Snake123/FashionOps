import { useEffect, useState } from 'react'
import { api, type Ticket } from '../api'
import { StatusBadge } from '../ui'

const SENTIMENT_TONE: Record<string, string> = {
  angry: 'danger',
  negative: 'warn',
  neutral: 'dim',
  positive: 'ok',
}

const SENTIMENT_LABEL: Record<string, string> = {
  angry: '激动',
  negative: '不满',
  neutral: '中性',
  positive: '好评',
}

export default function Tickets() {
  const [items, setItems] = useState<Ticket[]>([])
  const [total, setTotal] = useState(0)
  const [escalatedCount, setEscalatedCount] = useState(0)
  const [onlyEscalated, setOnlyEscalated] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([
      api.tickets({ page_size: 60, only_escalated: onlyEscalated }),
      api.tickets({ page_size: 1, only_escalated: true }),
    ])
      .then(([page, escalated]) => {
        setItems(page.items)
        setTotal(page.total)
        setEscalatedCount(escalated.total)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [onlyEscalated])

  return (
    <>
      <h2 className="page-title">客服工单</h2>
      <p className="page-desc">
        共 {total} 条，其中 <span style={{ color: 'var(--danger)' }}>{escalatedCount} 条已转人工</span>。
        转人工的触发条件是"情绪激动"或"涉及金额纠纷" —— 这两类 Agent 不该自作主张。
      </p>

      {error && <div className="error-box">{error}</div>}

      <div className="toolbar">
        <label style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <input
            type="checkbox"
            checked={onlyEscalated}
            onChange={(e) => setOnlyEscalated(e.target.checked)}
          />
          只看已转人工
        </label>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>工单号</th>
                <th>主题</th>
                <th>意图</th>
                <th>情绪</th>
                <th>优先级</th>
                <th>状态</th>
                <th>处理方</th>
                <th>关联订单</th>
                <th>转人工原因</th>
              </tr>
            </thead>
            <tbody>
              {items.map((ticket) => (
                <tr key={ticket.ticket_no}>
                  <td className="mono">{ticket.ticket_no}</td>
                  <td>{ticket.subject}</td>
                  <td className="muted">{ticket.intent ?? '—'}</td>
                  <td>
                    <span className={`badge ${SENTIMENT_TONE[ticket.sentiment ?? ''] ?? 'dim'}`}>
                      {SENTIMENT_LABEL[ticket.sentiment ?? ''] ?? '—'}
                    </span>
                  </td>
                  <td>
                    <span
                      className={`badge ${
                        ticket.priority === 'urgent'
                          ? 'danger'
                          : ticket.priority === 'high'
                            ? 'warn'
                            : 'dim'
                      }`}
                    >
                      {ticket.priority}
                    </span>
                  </td>
                  <td>
                    <StatusBadge status={ticket.status} />
                  </td>
                  <td>
                    <span className={`badge ${ticket.handler === 'human' ? 'warn' : 'ok'}`}>
                      {ticket.handler === 'human' ? '人工' : 'Agent'}
                    </span>
                  </td>
                  <td className="mono muted">{ticket.order_no ?? '—'}</td>
                  <td className="muted" style={{ maxWidth: 280 }}>
                    {ticket.escalate_reason ?? '—'}
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr>
                  <td colSpan={9} className="empty">
                    没有工单
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
