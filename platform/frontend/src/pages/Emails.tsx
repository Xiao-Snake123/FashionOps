import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, type EmailGateStats, type EmailMessage } from '../api'
import { num, StatusBadge } from '../ui'

/**
 * 闸门决策码 → 人能看懂的说法。
 *
 * 后端给的是 `blocked_whitelist` 这种机器码，直接显示成标签没意义。
 * 这里也是**唯一**需要维护映射的地方 —— 新增决策码时补一行即可，
 * 漏了会退回显示原始码（不会白屏），所以不会静默出错。
 */
const DECISION_LABEL: Record<string, string> = {
  allowed: '放行',
  dry_run: '演练（未真发）',
  blocked_disabled: '拦截 · 总开关关闭',
  blocked_whitelist: '拦截 · 收件人不在白名单',
  blocked_blacklist: '拦截 · 命中黑名单',
  blocked_ratelimit: '拦截 · 触发限流',
  blocked_size: '拦截 · 附件过大',
  blocked_recipients: '拦截 · 无有效收件人',
  blocked_error: '拦截 · 发送异常',
}

function decisionLabel(code: string): string {
  return DECISION_LABEL[code] ?? code
}

function decisionTone(code: string): string {
  if (code.startsWith('blocked')) return 'danger'
  if (code === 'dry_run') return 'warn'
  if (code === 'allowed') return 'ok'
  return 'dim'
}

/** 决策码分布条。宽度按最大计数归一化，不然小数量永远看不见。 */
function DecisionBars({ stats }: { stats: EmailGateStats }) {
  const rows = [...stats.by_decision].sort((a, b) => b.count - a.count)
  const max = Math.max(1, ...rows.map((r) => r.count))
  if (rows.length === 0) return <div className="empty">还没有邮件记录</div>

  return (
    <div>
      {rows.map((row) => (
        <div className="bar-row" key={row.decision}>
          <span title={row.decision}>{decisionLabel(row.decision)}</span>
          <div className="bar-track">
            <span
              className={decisionTone(row.decision)}
              style={{ width: `${Math.max(2, (row.count / max) * 100)}%` }}
            />
          </div>
          <span className="count">{row.count}</span>
        </div>
      ))}
    </div>
  )
}

export default function Emails() {
  const [items, setItems] = useState<EmailMessage[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState<EmailGateStats | null>(null)
  const [onlyBlocked, setOnlyBlocked] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<number | null>(null)

  const refresh = useCallback(
    () =>
      Promise.all([api.emails({ page_size: 50, only_blocked: onlyBlocked }), api.emailStats()])
        .then(([page, gate]) => {
          setItems(page.items)
          setTotal(page.total)
          setStats(gate)
          setError(null)
        })
        .catch((e: Error) => setError(e.message)),
    [onlyBlocked],
  )

  useEffect(() => {
    void refresh()
  }, [refresh])

  const blocked = stats?.blocked_total ?? 0

  return (
    <>
      <h2 className="page-title">邮件闸门</h2>
      <p className="page-desc">
        每一封邮件（放行的和被拦的都算）都会落库。只展示"发成功的"会让被拦的完全不可见 ——
        运维看到"这周只发了 12 封"，却不知道还有 8 封被策略挡住、而业务方以为已经通知了。
      </p>

      {error && <div className="error-box">{error}</div>}

      {stats && (
        <div className="grid grid-2" style={{ marginBottom: 14 }}>
          <div className="card">
            <h3>闸门决策分布</h3>
            <DecisionBars stats={stats} />
          </div>
          <div className="card">
            <h3>概览</h3>
            <div className="grid grid-2">
              <div>
                <div className="stat-label">邮件总数</div>
                <div className="stat-value small">{num(stats.total)}</div>
              </div>
              <div>
                <div className="stat-label">被拦截</div>
                <div className="stat-value small" style={{ color: blocked > 0 ? 'var(--danger)' : undefined }}>
                  {num(blocked)}
                </div>
              </div>
            </div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 14, lineHeight: 1.7 }}>
              四层闸门按顺序判定：总开关 → 白名单/黑名单 → 限流 → 体积。
              <br />
              任何一层没通过就拦下，且拦下也要写库（fail-closed）。
            </div>
          </div>
        </div>
      )}

      <div className="toolbar">
        <select value={onlyBlocked ? 'blocked' : ''} onChange={(e) => setOnlyBlocked(e.target.value === 'blocked')}>
          <option value="">全部邮件</option>
          <option value="blocked">只看被拦截</option>
        </select>
        <span className="muted" style={{ fontSize: 12.5 }}>
          共 {total} 条
        </span>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>邮件号</th>
                <th>收件人</th>
                <th>主题</th>
                <th>状态</th>
                <th>闸门决策</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                // 用 Fragment 而不是 <>：展开行和主行是同一个列表项，
                // 而简写片段不能带 key，React 会报列表 key 缺失
                <Fragment key={item.id}>
                  <tr
                    className="clickable"
                    onClick={() => setExpanded(expanded === item.id ? null : item.id)}
                  >
                    <td className="mono">{item.message_no}</td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {(item.to_addrs ?? []).join(', ') || '—'}
                    </td>
                    <td>{item.subject ?? '—'}</td>
                    <td>
                      <StatusBadge status={item.status} />
                    </td>
                    <td>
                      <span className={`badge ${decisionTone(item.gate_decision ?? '')}`}>
                        {decisionLabel(item.gate_decision ?? 'unknown')}
                      </span>
                    </td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {new Date(item.created_at).toLocaleString('zh-CN')}
                    </td>
                  </tr>
                  {expanded === item.id && (
                    <tr>
                      <td colSpan={6} style={{ background: 'var(--panel-2)' }}>
                        {item.gate_reason && (
                          <div style={{ marginBottom: 8 }}>
                            <span className="stat-label">拦截原因</span>
                            <div>{item.gate_reason}</div>
                          </div>
                        )}
                        {item.error_message && (
                          <div className="error-box" style={{ marginBottom: 8 }}>
                            {item.error_message}
                          </div>
                        )}
                        <div className="stat-label">正文</div>
                        <div className="draft" style={{ marginTop: 4 }}>
                          {item.body_text || '（无正文）'}
                        </div>
                        {item.cc_addrs && item.cc_addrs.length > 0 && (
                          <div className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
                            抄送：<span className="mono">{item.cc_addrs.join(', ')}</span>
                          </div>
                        )}
                        {item.bcc_addrs && item.bcc_addrs.length > 0 && (
                          <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
                            密送：<span className="mono">{item.bcc_addrs.join(', ')}</span>
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
              {items.length === 0 && (
                <tr>
                  <td colSpan={6} className="empty">
                    没有邮件记录
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
