import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  ApiError,
  type HumanStatusOption,
  type Ticket,
  type TicketBoardStats,
  type TicketEvent,
  type TicketOutcome,
  type TicketStats,
} from '../api'
import { currentUserSnapshot } from '../auth'
import { PageHeader, StatusBadge } from '../ui'

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

/** 一次批量处理多少条。和接口的默认值对齐。 */
const BATCH_LIMIT = 10

/** 标出这个值是谁定的。 */
function SourceTag({ source }: { source: string }) {
  if (source !== 'rule' && source !== 'model') return null
  return (
    <span className={`src-tag ${source}`} title={source === 'rule' ? '关键词规则判定' : '模型判定'}>
      {source === 'rule' ? '规则' : '模型'}
    </span>
  )
}

/** 把转人工原因归到少数几类，方便聚合定位主因。 */
function categorizeReason(reason: string | null): string {
  const r = reason ?? ''
  if (/尺码|码数|size|偏大|偏小/.test(r)) return '尺码'
  if (/质量|掉色|破损|瑕疵|色差|开线/.test(r)) return '质量'
  if (/金额|退款|价格|差价|赔偿|运费/.test(r)) return '金额'
  if (/情绪|辱骂|激动|态度|纠纷/.test(r)) return '情绪'
  if (/无依据|知识库|查不到/.test(r)) return '无依据'
  return '其他'
}

/** SLA：按优先级给硬约束（urgent 4h / high 8h / 其他 24h），算剩余时间与紧急度。 */
function slaInfo(ticket: Ticket): { text: string; tone: 'danger' | 'warn' | 'dim' } {
  const elapsedMin = Math.max(0, (Date.now() - new Date(ticket.created_at).getTime()) / 60000)
  const limitMin = ticket.priority === 'urgent' ? 240 : ticket.priority === 'high' ? 480 : 1440
  const remain = limitMin - elapsedMin
  if (remain <= 0) return { text: `超 ${Math.floor(-remain / 60)} 时`, tone: 'danger' }
  if (remain <= 60) return { text: `剩 ${Math.floor(remain)} 分`, tone: 'warn' }
  return { text: `剩 ${Math.floor(remain / 60)} 时 ${Math.floor(remain % 60)} 分`, tone: 'dim' }
}

const PRIORITY_TONE: Record<string, string> = {
  urgent: 'danger',
  high: 'warn',
  medium: 'dim',
  low: 'dim',
}

const PRIORITY_LABEL: Record<string, string> = {
  urgent: '紧急',
  high: '高',
  medium: '中',
  low: '低',
}

/** 人工阶段的中文名。**优先用后端给的标签**，没拿到选项表时退回原始值。 */
function humanStatusLabel(value: string | null | undefined, options: HumanStatusOption[]): string {
  if (!value) return '未开始'
  return options.find((o) => o.value === value)?.label ?? value
}

type FilterKey = 'all' | 'angry' | 'urgent' | 'overdue' | 'mine' | 'unassigned'

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'angry', label: '情绪激动' },
  { key: 'urgent', label: '紧急' },
  { key: 'overdue', label: '已超时' },
  { key: 'unassigned', label: '未派单' },
  { key: 'mine', label: '派给我' },
]

export default function Tickets() {
  const [items, setItems] = useState<Ticket[]>([])
  const [total, setTotal] = useState(0)
  const [escalatedCount, setEscalatedCount] = useState(0)
  const [onlyEscalated, setOnlyEscalated] = useState(false)
  const [filter, setFilter] = useState<FilterKey>('all')
  const [error, setError] = useState<string | null>(null)

  // ---- 处理相关的状态 ----
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [stats, setStats] = useState<TicketStats | null>(null)
  const [results, setResults] = useState<TicketOutcome[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // ---- 人工侧：派单与进度 ----
  const [board, setBoard] = useState<TicketBoardStats | null>(null)
  const [statusOptions, setStatusOptions] = useState<HumanStatusOption[]>([])
  const [actNotice, setActNotice] = useState<{ ok: boolean; text: string } | null>(null)
  const me = currentUserSnapshot()?.username ?? ''

  const load = useCallback(async () => {
    const [page, escalated, boardStats] = await Promise.all([
      api.tickets({ page_size: 60, only_escalated: onlyEscalated }),
      api.tickets({ page_size: 1, only_escalated: true }),
      // 看板统计失败**不该**让整页打不开：它只是页头那几个数字
      api.ticketBoardStats().catch(() => null),
    ])
    setItems(page.items)
    setTotal(page.total)
    setEscalatedCount(escalated.total)
    setBoard(boardStats)
    setError(null)
  }, [onlyEscalated])

  useEffect(() => {
    load().catch((e: Error) => setError(e.message))
  }, [load])

  // 人工阶段的可选值从后端取，前端不写死 —— 加一个阶段只改后端一处
  useEffect(() => {
    api
      .ticketStatuses()
      .then((r) => setStatusOptions(r.human_statuses))
      .catch(() => setStatusOptions([]))
  }, [])

  /** 跑一条或一批，共用同一套事件处理。 */
  const run = useCallback(
    async (start: (onEvent: (e: TicketEvent) => void, signal: AbortSignal) => Promise<void>) => {
      setRunning(true)
      setError(null)
      setResults([])
      setStats(null)
      setProgress({ done: 0, total: 1 })
      const controller = new AbortController()
      abortRef.current = controller

      const collected: TicketOutcome[] = []
      try {
        await start((event) => {
          switch (event.type) {
            case 'start':
              setProgress({ done: 0, total: event.total })
              break
            case 'ticket':
              collected.push(event.outcome)
              setProgress((p) => ({ done: collected.length, total: p?.total ?? collected.length }))
              setResults([...collected])
              break
            case 'done':
              setStats(event.stats)
              break
            case 'error':
              setError(event.detail)
              break
            default:
              break
          }
        }, controller.signal)
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err))
        }
      } finally {
        setRunning(false)
        setProgress(null)
        abortRef.current = null
        load().catch(() => undefined)
      }
    },
    [load],
  )

  const handleOne = (ticketNo: string) => run((onEvent, signal) => api.handleTicket(ticketNo, onEvent, signal))
  const handleBatch = (all: boolean) =>
    run((onEvent, signal) => api.handleTicketsBatch({ limit: BATCH_LIMIT, all }, onEvent, signal))
  const stop = () => abortRef.current?.abort()

  const selectedTicket = items.find((t) => t.ticket_no === selected) ?? null
  const selectedOutcome = results.find((r) => r.ticket_no === selected) ?? null

  // 转人工原因归类聚合（只看已转人工的）
  const reasonDist = useMemo(() => {
    const map = new Map<string, number>()
    for (const t of items) {
      if (t.escalated || t.escalate_reason) {
        const cat = categorizeReason(t.escalate_reason)
        map.set(cat, (map.get(cat) ?? 0) + 1)
      }
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1])
  }, [items])

  // 快捷筛选（客户端，基于已加载的列表）
  const visible = useMemo(() => {
    if (filter === 'all') return items
    return items.filter((t) => {
      if (filter === 'angry') return t.sentiment === 'angry'
      if (filter === 'urgent') return t.priority === 'urgent'
      if (filter === 'overdue') return slaInfo(t).tone === 'danger'
      if (filter === 'unassigned') return !t.assignee
      if (filter === 'mine') return !!me && t.assignee === me
      return true
    })
  }, [items, filter, me])

  /** 派单。传 null = 退回未派单。 */
  const assign = async (ticketNo: string, assignee: string | null) => {
    setActNotice(null)
    try {
      const res = await api.assignTicket(ticketNo, assignee)
      setActNotice({ ok: true, text: res.message })
      await load()
    } catch (e) {
      setActNotice({ ok: false, text: e instanceof Error ? e.message : String(e) })
    }
  }

  /** 更新人工进度。**和 Agent 的 resolution 互不覆盖** —— 见 service.py 的注释。 */
  const updateProgress = async (ticketNo: string, humanStatus: string, note?: string) => {
    setActNotice(null)
    try {
      const res = await api.updateTicketProgress(ticketNo, humanStatus, note)
      setActNotice({ ok: true, text: res.message })
      await load()
    } catch (e) {
      setActNotice({ ok: false, text: e instanceof Error ? e.message : String(e) })
    }
  }

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '客服工单']}
        title="客服工单"
        badge={<span className="badge danger">转人工 {escalatedCount}</span>}
        desc={
          <>
            共 {total} 条，其中 <span style={{ color: 'var(--danger)' }}>{escalatedCount} 条已转人工</span>。
            转人工由**代码规则**决定（情绪激动 / 涉及金额纠纷 / 知识库没依据）——模型只能提议，不能决定。
          </>
        }
      />

      {error && <div className="error-box">{error}</div>}
      {actNotice && <div className={actNotice.ok ? 'notice ok' : 'error-box'}>{actNotice.text}</div>}

      {board && (
        <div className="run-summary">
          <strong>人工跟进</strong>
          <span>
            未派单 <b style={{ color: board.unassigned > 0 ? 'var(--danger)' : undefined }}>{board.unassigned}</b>
            {' · '}
            待接单 <b>{board.by_human_status.pending ?? 0}</b>
            {' · '}
            已联系 <b>{board.by_human_status.contacted ?? 0}</b>
            {' · '}
            等客户 <b>{board.by_human_status.waiting_customer ?? 0}</b>
            {' · '}
            已解决 <b>{board.by_human_status.resolved ?? 0}</b>
          </span>
        </div>
      )}

      <div className="toolbar">
        <label style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <input
            type="checkbox"
            checked={onlyEscalated}
            onChange={(e) => setOnlyEscalated(e.target.checked)}
          />
          只看已转人工
        </label>

        <div className="filter-chips">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`filter-chip${filter === f.key ? ' active' : ''}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>

        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          {running ? (
            <>
              <span className="muted">
                处理中 {progress ? `${progress.done}/${progress.total}` : ''}…
              </span>
              <button className="button danger" type="button" onClick={stop}>
                停止
              </button>
            </>
          ) : (
            <>
              <button className="button" type="button" onClick={() => handleBatch(false)}>
                处理待处理（最多 {BATCH_LIMIT} 条）
              </button>
              <button
                className="button"
                type="button"
                title="连已处理的也重跑一遍。会覆盖上一轮判断"
                onClick={() => handleBatch(true)}
              >
                全部重跑一批
              </button>
            </>
          )}
        </div>
      </div>

      {stats && (
        <div className="run-summary">
          <strong>本轮结果</strong>
          <span>
            共 {stats.total} 条 · 快车道 <b>{stats.fast_lane}</b>（0 次模型调用）· Agent{' '}
            <b>{stats.agent_lane}</b> · 转人工 <b>{stats.escalated}</b>
            {stats.failed > 0 && <> · 失败 <b style={{ color: 'var(--danger)' }}>{stats.failed}</b></>} ·{' '}
            {stats.tokens} tokens
          </span>
        </div>
      )}

      {reasonDist.length > 0 && (
        <div className="reason-dist">
          <span className="muted">转人工主因</span>
          {reasonDist.map(([cat, count]) => (
            <span key={cat} className="tag">
              {cat} <b>{count}</b>
            </span>
          ))}
        </div>
      )}

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>工单号</th>
                <th style={{ minWidth: 220 }}>主题</th>
                <th>意图</th>
                <th>情绪</th>
                <th>优先级</th>
                <th>SLA 倒计时</th>
                <th>转人工原因</th>
                <th>状态</th>
                <th>负责人 / 人工进度</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visible.map((ticket) => {
                const outcome = results.find((r) => r.ticket_no === ticket.ticket_no)
                const sla = slaInfo(ticket)
                return (
                  <tr
                    key={ticket.ticket_no}
                    className={selected === ticket.ticket_no ? 'row-active' : undefined}
                    onClick={() => setSelected(ticket.ticket_no)}
                    style={{ cursor: 'pointer' }}
                  >
                    <td className="mono">{ticket.ticket_no}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>{ticket.subject}</td>
                    <td className="muted">
                      {ticket.intent ?? '—'}
                      {outcome && <SourceTag source={outcome.intent_source} />}
                    </td>
                    <td>
                      <span className={`badge ${SENTIMENT_TONE[ticket.sentiment ?? ''] ?? 'dim'}`}>
                        {SENTIMENT_LABEL[ticket.sentiment ?? ''] ?? '—'}
                      </span>
                    </td>
                    <td>
                      <span className={`badge ${PRIORITY_TONE[ticket.priority] ?? 'dim'}`}>
                        {PRIORITY_LABEL[ticket.priority] ?? ticket.priority}
                      </span>
                    </td>
                    <td>
                      <span className={`badge ${sla.tone}`}>{sla.text}</span>
                    </td>
                    <td>
                      {ticket.escalate_reason ? (
                        <span className="reason-cell" title={ticket.escalate_reason}>
                          <span className="tag">{categorizeReason(ticket.escalate_reason)}</span>
                          <span className="muted reason-text">{ticket.escalate_reason}</span>
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <StatusBadge status={ticket.status} />
                    </td>
                    <td>
                      {ticket.assignee ? (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                          <span className="mono" style={{ fontSize: 12 }}>
                            {ticket.assignee}
                            {ticket.assignee === me && <span className="badge ok" style={{ marginLeft: 5 }}>我</span>}
                          </span>
                          <span className="muted" style={{ fontSize: 11.5 }}>
                            {humanStatusLabel(ticket.human_status, statusOptions)}
                          </span>
                        </div>
                      ) : (
                        <span className="badge warn">未派单</span>
                      )}
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: 6 }}>
                        <button
                          className="button small"
                          type="button"
                          disabled={running}
                          onClick={(e) => {
                            e.stopPropagation()
                            handleOne(ticket.ticket_no)
                          }}
                        >
                          处理
                        </button>
                        {!ticket.assignee && (
                          <button
                            className="button small"
                            type="button"
                            disabled={running}
                            title="派给自己"
                            onClick={(e) => {
                              e.stopPropagation()
                              void assign(ticket.ticket_no, me || null)
                            }}
                          >
                            接单
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
              {visible.length === 0 && (
                <tr>
                  <td colSpan={10} className="empty">
                    没有工单
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {selectedTicket && (
        <TicketDetail
          ticket={selectedTicket}
          outcome={selectedOutcome}
          statusOptions={statusOptions}
          me={me}
          onClose={() => setSelected(null)}
          onAssign={(who) => void assign(selectedTicket.ticket_no, who)}
          onProgress={(status, note) => void updateProgress(selectedTicket.ticket_no, status, note)}
        />
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
function TicketDetail({
  ticket,
  outcome,
  statusOptions,
  me,
  onClose,
  onAssign,
  onProgress,
}: {
  ticket: Ticket
  outcome: TicketOutcome | null
  statusOptions: HumanStatusOption[]
  me: string
  onClose: () => void
  onAssign: (assignee: string | null) => void
  onProgress: (humanStatus: string, note?: string) => void
}) {
  const [note, setNote] = useState('')
  const [owner, setOwner] = useState(ticket.assignee ?? '')

  return (
    <div className="card detail-card">
      <div className="detail-head">
        <div>
          <strong className="mono">{ticket.ticket_no}</strong>
          <span className="muted"> · {ticket.subject}</span>
        </div>
        <button className="button small" type="button" onClick={onClose}>
          收起
        </button>
      </div>

      {/* 人工跟进区。**和 Agent 的判断分开显示**：
          "机器说解决了、人还在等退款"是最需要被看见的状态，
          混在一起就会被其中一方盖掉。 */}
      <div className="detail-block">
        <div className="detail-label">
          人工跟进
          {ticket.assignee && (
            <span className="muted"> · 负责人 {ticket.assignee}</span>
          )}
          {ticket.human_updated_at && (
            <span className="muted">
              {' '}
              · 更新于 {new Date(ticket.human_updated_at).toLocaleString('zh-CN')}
            </span>
          )}
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'flex-end', marginTop: 6 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="muted">负责人</span>
            <input
              className="input"
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              placeholder="用户名，留空 = 退回未派单"
              style={{ width: 180 }}
            />
          </label>
          <button type="button" className="button small" onClick={() => onAssign(owner.trim() || null)}>
            {owner.trim() ? '派单' : '退回未派单'}
          </button>
          {me && owner.trim() !== me && (
            <button type="button" className="button small" onClick={() => onAssign(me)}>
              派给我
            </button>
          )}
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'flex-end', marginTop: 10 }}>
          {statusOptions.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`button small${ticket.human_status === option.value ? ' primary' : ''}`}
              onClick={() => onProgress(option.value, note.trim() || undefined)}
            >
              {option.label}
            </button>
          ))}
          <input
            className="input"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="这一条的说明（可选）"
            style={{ flex: 1, minWidth: 180 }}
          />
        </div>

        {ticket.human_note && (
          <div className="muted detail-reason" style={{ marginTop: 8 }}>
            最近备注：{ticket.human_note}
          </div>
        )}
      </div>

      {ticket.content && (
        <div className="detail-block">
          <div className="detail-label">客户原话</div>
          <div className="detail-body">{ticket.content}</div>
        </div>
      )}

      {outcome && (
        <div className="detail-block">
          <div className="detail-label">本次判断</div>
          <div className="detail-tags">
            <span className="badge dim">通道 {outcome.lane === 'fast' ? '快车道' : 'Agent'}</span>
            <span className="badge dim">
              {outcome.intent || '未判出'} <SourceTag source={outcome.intent_source} />
            </span>
            <span className="badge dim">
              {outcome.sentiment} <SourceTag source={outcome.sentiment_source} />
            </span>
            <span className="badge dim">置信度 {outcome.confidence.toFixed(2)}</span>
            <span className={`badge ${outcome.escalated ? 'warn' : 'ok'}`}>
              {outcome.escalated ? '转人工' : '自动处理'}
            </span>
          </div>
          <div className="muted detail-reason">
            为什么走这条路：{outcome.lane_reason}
            {outcome.escalate_reason && <> ｜ 转人工原因：{outcome.escalate_reason}</>}
          </div>
          <div className="muted detail-reason">
            开销：{outcome.steps} 轮模型调用 / {outcome.tool_calls} 次工具调用 / {outcome.tokens} tokens /{' '}
            {outcome.duration_ms} ms
          </div>
        </div>
      )}

      {ticket.resolution && (
        <div className="detail-block">
          <div className="detail-label">
            回复草稿
            {ticket.confidence != null && (
              <span className="muted"> · 置信度 {Number(ticket.confidence).toFixed(2)}</span>
            )}
          </div>
          <pre className="draft-box">{ticket.resolution}</pre>
          {ticket.cited_article_nos && ticket.cited_article_nos.length > 0 && (
            <div className="muted detail-reason">
              知识库依据：{ticket.cited_article_nos.join('、')}
            </div>
          )}
        </div>
      )}

      {!outcome && !ticket.resolution && (
        <div className="muted">这条还没被 Agent 处理过（列表里的值是历史数据）。点「处理」跑一遍。</div>
      )}
    </div>
  )
}
