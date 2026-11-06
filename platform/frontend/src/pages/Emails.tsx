import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, type EmailGateStats, type EmailMessage, type MailPolicy } from '../api'
import { num, PageHeader } from '../ui'

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

/** 邮件状态中文名（草稿/已发送/失败分流）。 */
const EMAIL_STATUS_LABEL: Record<string, string> = {
  sent: '已发送',
  draft: '草稿',
  failed: '发送失败',
  blocked: '已拦截',
  queued: '排队中',
}

function emailStatusLabel(s: string): string {
  return EMAIL_STATUS_LABEL[s] ?? s
}

function emailStatusTone(s: string): string {
  if (s === 'sent') return 'ok'
  if (s === 'failed' || s === 'blocked') return 'danger'
  return 'dim'
}

type StatusFilter = 'all' | 'draft' | 'sent' | 'failed'

const STATUS_FILTERS: { key: StatusFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'draft', label: '草稿' },
  { key: 'sent', label: '已发送' },
  { key: 'failed', label: '失败' },
]

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
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<number | null>(null)

  // ---- 策略运维 ----
  const [policy, setPolicy] = useState<MailPolicy | null>(null)
  const [allowlist, setAllowlist] = useState('')
  const [saving, setSaving] = useState(false)
  const [retrying, setRetrying] = useState<number | null>(null)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)

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

  // 策略面板只在有权限时才出现。**靠后端 403 决定，不在前端猜权限** ——
  // 前端那份权限快照可能过期，猜错的结果是给出一组点了必然报错的按钮。
  useEffect(() => {
    api
      .mailPolicy()
      .then((p) => {
        setPolicy(p)
        setAllowlist(p.allowlist.join(','))
      })
      .catch(() => setPolicy(null))
  }, [])

  const savePolicy = async (patch: Parameters<typeof api.updateMailPolicy>[0]) => {
    setSaving(true)
    setNotice(null)
    try {
      const updated = await api.updateMailPolicy(patch)
      setPolicy(updated)
      setAllowlist(updated.allowlist.join(','))
      if (updated.errors && updated.errors.length > 0) {
        setNotice({ ok: false, text: updated.errors.join('；') })
      } else {
        setNotice({ ok: true, text: '策略已更新，后续发信立即按新策略判定' })
      }
    } catch (e) {
      setNotice({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally {
      setSaving(false)
    }
  }

  const retry = async (item: EmailMessage) => {
    setRetrying(item.id)
    setNotice(null)
    try {
      const res = await api.retryEmail(item.message_no)
      // 被拦不是"操作失败"：闸门重新判了一遍并给出了原因，那正是要显示给人的东西
      setNotice({
        ok: res.sent,
        text: res.hint || res.reason || `已重发（第 ${res.retry_count} 次）`,
      })
      setExpanded(item.id)
      await refresh()
    } catch (e) {
      setNotice({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally {
      setRetrying(null)
    }
  }

  /** 哪些信件值得重发：被拦的、失败的、还有停留在演练态的草稿。 */
  const canRetry = (item: EmailMessage) => item.blocked || item.status === 'failed' || item.status === 'draft'

  const blocked = stats?.blocked_total ?? 0

  const visible = statusFilter === 'all' ? items : items.filter((i) => i.status === statusFilter)

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '邮件闸门']}
        title="邮件闸门"
        desc='每一封邮件（放行的和被拦的都算）都会落库。只展示"发成功的"会让被拦的完全不可见 —— 运维看到"这周只发了 12 封"，却不知道还有 8 封被策略挡住、而业务方以为已经通知了。'
      />

      {blocked > 0 && (
        <div className="risk-banner" style={{ marginTop: 0 }}>
          <b>⚠ {blocked} 封邮件被拦截</b>
          <span>收件人没收到通知，可能影响业务。勾选「只看被拦截」排查。</span>
        </div>
      )}

      {error && <div className="error-box">{error}</div>}
      {notice && (
        <div className={notice.ok ? 'notice ok' : 'error-box'}>{notice.text}</div>
      )}

      {policy && (
        <div className="card" style={{ marginBottom: 14 }}>
          <h3>
            邮件策略
            <span className={`badge ${policy.smtp_configured ? 'ok' : 'warn'}`} style={{ marginLeft: 8 }}>
              {policy.smtp_configured ? '可真发' : '仅演练'}
            </span>
          </h3>
          <p className="muted" style={{ margin: '4px 0 12px', fontSize: 12.5, lineHeight: 1.7 }}>
            白名单为空 = 禁止发送任何邮件（fail-closed）。改这里只改运行时覆盖，
            不动 <span className="mono">.env</span>；Redis 一清就自动回到配置里的安全值。
            {Object.keys(policy.overrides).length > 0 && (
              <>
                {' '}
                当前有 <b>{Object.keys(policy.overrides).length}</b> 项被运行时覆盖
                （来源：{policy.override_source === 'redis' ? 'Redis，重启后仍生效' : '内存，重启即失效'}）。
              </>
            )}
          </p>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4, flex: 1, minWidth: 260 }}>
              <span className="muted">收件人白名单（逗号分隔）</span>
              <input
                className="input"
                value={allowlist}
                onChange={(e) => setAllowlist(e.target.value)}
                placeholder="@example.com, ops@corp.com"
              />
            </label>
            <button
              type="button"
              className="button"
              disabled={saving}
              onClick={() => void savePolicy({ mail_send_allowlist: allowlist })}
            >
              保存白名单
            </button>
            {/* 演练 → 真发。这是整个页面最危险的一个按钮，所以它自带确认。 */}
            <button
              type="button"
              className={`button ${policy.dry_run ? '' : 'danger'}`}
              disabled={saving}
              onClick={() => {
                if (policy.dry_run) {
                  const ok = window.confirm(
                    '关闭演练模式后，Agent 真的会对外发信（邮件发出去收不回来）。确定要开闸吗？',
                  )
                  if (!ok) return
                  void savePolicy({ mail_dry_run: false, mail_enabled: true })
                } else {
                  void savePolicy({ mail_dry_run: true })
                }
              }}
            >
              {policy.dry_run ? '转为真发…' : '回到演练模式'}
            </button>
            <button
              type="button"
              className="button"
              disabled={saving}
              onClick={() =>
                void api
                  .resetMailPolicy()
                  .then((r) => {
                    setPolicy(r.policy)
                    setAllowlist(r.policy.allowlist.join(','))
                    setNotice({ ok: true, text: '已清空运行时覆盖，策略回到 .env' })
                  })
                  .catch((e: Error) => setNotice({ ok: false, text: e.message }))
              }
            >
              恢复 .env
            </button>
          </div>

          <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            总开关 {policy.enabled ? '开' : '关'} · 单封上限 {policy.max_recipients} 人 · 每收件人每分钟{' '}
            {policy.rate_limit_per_minute} 封 · 发件人 <span className="mono">{policy.from}</span> · SMTP{' '}
            <span className="mono">{policy.smtp_host}</span>
          </div>
        </div>
      )}

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
            <div className="gate-flow" style={{ marginTop: 14 }}>
              {['总开关', '白名单/黑名单', '限流', '体积'].map((step, i) => (
                <Fragment key={step}>
                  <span className="gate-step">{step}</span>
                  {i < 3 && <span className="gate-arrow">→</span>}
                </Fragment>
              ))}
            </div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 10, lineHeight: 1.7 }}>
              四层闸门按顺序判定，任何一层没通过就拦下，且拦下也要写库（fail-closed）。
            </div>
          </div>
        </div>
      )}

      <div className="toolbar">
        <div className="filter-chips">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`filter-chip${statusFilter === f.key ? ' active' : ''}`}
              onClick={() => setStatusFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <input type="checkbox" checked={onlyBlocked} onChange={(e) => setOnlyBlocked(e.target.checked)} />
          只看被拦截
        </label>
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
              {visible.map((item) => (
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
                      <span className={`badge ${emailStatusTone(item.status)}`}>
                        {emailStatusLabel(item.status)}
                      </span>
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
                        {canRetry(item) && (
                          <div style={{ marginTop: 10, display: 'flex', gap: 8, alignItems: 'center' }}>
                            <button
                              type="button"
                              className="button small"
                              disabled={retrying === item.id}
                              onClick={() => void retry(item)}
                            >
                              {retrying === item.id ? '重发中…' : '重新发送'}
                            </button>
                            <span className="muted" style={{ fontSize: 12 }}>
                              重发会重新过一遍闸门（含白名单和限流），不是照着上次名单直接投
                            </span>
                          </div>
                        )}
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
              {visible.length === 0 && (
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
