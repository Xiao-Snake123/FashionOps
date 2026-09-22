import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type Ticket, type TicketEvent, type TicketOutcome, type TicketStats } from '../api'
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

/** 标出这个值是谁定的。
 *
 *  不显示来源的话，「规则优先、模型兜底」这个架构决定就**看不出效果** ——
 *  盯着"意图=退换货"分不出它来自规则还是模型，也就不知道该调哪一边。 */
function SourceTag({ source }: { source: string }) {
  if (source !== 'rule' && source !== 'model') return null
  return (
    <span className={`src-tag ${source}`} title={source === 'rule' ? '关键词规则判定' : '模型判定'}>
      {source === 'rule' ? '规则' : '模型'}
    </span>
  )
}

export default function Tickets() {
  const [items, setItems] = useState<Ticket[]>([])
  const [total, setTotal] = useState(0)
  const [escalatedCount, setEscalatedCount] = useState(0)
  const [onlyEscalated, setOnlyEscalated] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // ---- 处理相关的状态 ----
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [stats, setStats] = useState<TicketStats | null>(null)
  const [results, setResults] = useState<TicketOutcome[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const load = useCallback(async () => {
    const [page, escalated] = await Promise.all([
      api.tickets({ page_size: 60, only_escalated: onlyEscalated }),
      api.tickets({ page_size: 1, only_escalated: true }),
    ])
    setItems(page.items)
    setTotal(page.total)
    setEscalatedCount(escalated.total)
    setError(null)
  }, [onlyEscalated])

  useEffect(() => {
    load().catch((e: Error) => setError(e.message))
  }, [load])

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
              // 用 events.length 而不是 index：单条处理的事件里没有 index
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
        // 处理完必须重新拉列表：意图/情绪/是否转人工/草稿全都变了，
        // 不刷新的话界面显示的还是处理前的旧值，会让人以为"点了没反应"
        load().catch(() => undefined)
      }
    },
    [load],
  )

  const handleOne = (ticketNo: string) =>
    run((onEvent, signal) => api.handleTicket(ticketNo, onEvent, signal))

  const handleBatch = (all: boolean) =>
    run((onEvent, signal) => api.handleTicketsBatch({ limit: BATCH_LIMIT, all }, onEvent, signal))

  const stop = () => abortRef.current?.abort()

  const selectedTicket = items.find((t) => t.ticket_no === selected) ?? null
  const selectedOutcome = results.find((r) => r.ticket_no === selected) ?? null

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '客服工单']}
        title="客服工单"
        badge={<span className="badge danger">转人工 {escalatedCount}</span>}
        desc={
          <>
            共 {total} 条，其中 <span style={{ color: 'var(--danger)' }}>{escalatedCount} 条已转人工</span>。
            转人工由**代码规则**决定（情绪激动 / 涉及金额纠纷 / 知识库没依据）——
            模型只能提议，不能决定。点「处理」看两层流程怎么跑。
          </>
        }
      />

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
            共 {stats.total} 条 · 快车道{' '}
            <b>{stats.fast_lane}</b>（0 次模型调用）· Agent <b>{stats.agent_lane}</b> · 转人工{' '}
            <b>{stats.escalated}</b>
            {stats.failed > 0 && <> · 失败 <b style={{ color: 'var(--danger)' }}>{stats.failed}</b></>} ·{' '}
            {stats.tokens} tokens
          </span>
        </div>
      )}

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
                <th>通道</th>
                <th>状态</th>
                <th>处理方</th>
                <th>转人工原因</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((ticket) => {
                const outcome = results.find((r) => r.ticket_no === ticket.ticket_no)
                return (
                  <tr
                    key={ticket.ticket_no}
                    className={selected === ticket.ticket_no ? 'row-active' : undefined}
                    onClick={() => setSelected(ticket.ticket_no)}
                    style={{ cursor: 'pointer' }}
                  >
                    <td className="mono">{ticket.ticket_no}</td>
                    <td>{ticket.subject}</td>
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
                      {ticket.agent_lane ? (
                        <span className={`badge ${ticket.agent_lane === 'fast' ? 'ok' : 'dim'}`}>
                          {ticket.agent_lane === 'fast' ? '快车道' : 'Agent'}
                        </span>
                      ) : (
                        <span className="muted">没跑过</span>
                      )}
                    </td>
                    <td>
                      <StatusBadge status={ticket.status} />
                    </td>
                    <td>
                      <span className={`badge ${ticket.handler === 'human' ? 'warn' : 'ok'}`}>
                        {ticket.handler === 'human' ? '人工' : 'Agent'}
                      </span>
                    </td>
                    <td className="muted" style={{ maxWidth: 240 }}>
                      {ticket.escalate_reason ?? '—'}
                    </td>
                    <td>
                      <button
                        className="button small"
                        type="button"
                        disabled={running}
                        onClick={(e) => {
                          // 别让点击冒泡到行选择 —— 否则点"处理"会顺带把面板也切了
                          e.stopPropagation()
                          handleOne(ticket.ticket_no)
                        }}
                      >
                        处理
                      </button>
                    </td>
                  </tr>
                )
              })}
              {items.length === 0 && (
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
        <TicketDetail ticket={selectedTicket} outcome={selectedOutcome} onClose={() => setSelected(null)} />
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
function TicketDetail({
  ticket,
  outcome,
  onClose,
}: {
  ticket: Ticket
  outcome: TicketOutcome | null
  onClose: () => void
}) {
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

      {ticket.content && <div className="detail-block">
        <div className="detail-label">客户原话</div>
        <div className="detail-body">{ticket.content}</div>
      </div>}

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
            开销：{outcome.steps} 轮模型调用 / {outcome.tool_calls} 次工具调用 / {outcome.tokens}{' '}
            tokens / {outcome.duration_ms} ms
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
          {/* 用 pre-wrap 原样保留换行：草稿是给人读的，
              挤成一行或者被 markdown 渲染掉格式都会让人不敢用它 */}
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

