import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api,
  ApiError,
  type ChatApproval,
  type ChatEvent,
  type ChatMessage,
  type Conversation,
} from '../api'

/**
 * 对话页 —— Agent 的主入口。
 *
 * ## 三个和普通聊天框不一样的地方
 *
 * 1. **工具调用要看得见**。模型调了什么工具、成没成功，直接折叠展示出来。
 *    这是对话式 Agent 和普通聊天机器人最大的区别 —— 不展示的话，
 *    用户没法判断"这个数字是查出来的还是编的"，而那是这个系统的立身之本。
 * 2. **写操作会插一张确认卡片**。发邮件这类动作不会闷头执行，
 *    而是在对话流里插一张卡片，点了才做。
 * 3. **任务卡片**。多步需求会被转成一个真正的任务，卡片能点进任务页
 *    看 DAG 和实时进度 —— 对话负责"听懂"，任务负责"跑完并留痕"。
 */

/** 正在流式生成中的内容。落库的消息和它分开存 —— 它的生命周期只有一个回合。 */
interface LiveState {
  text: string
  steps: { name: string; state: 'running' | 'done'; ok?: boolean }[]
}

const EMPTY_LIVE: LiveState = { text: '', steps: [] }

export default function Chat() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [current, setCurrent] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [pending, setPending] = useState<ChatApproval[]>([])
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE)
  const [streaming, setStreaming] = useState(false)
  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  // ---------------------------------------------------------------- 数据加载
  const loadConversations = useCallback(async () => {
    const data = await api.conversations({ limit: 50 })
    setConversations(data.items)
    return data.items
  }, [])

  const loadConversation = useCallback(async (conversationNo: string) => {
    const data = await api.conversation(conversationNo)
    setMessages(data.messages)
    setPending(data.pending_approvals)
  }, [])

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        let items = await loadConversations()
        if (!alive) return
        // 一条都没有就自动开一个 —— 让用户落地就能打字，
        // 而不是先面对一个"新建会话"的空壳按钮
        if (items.length === 0) {
          const created = await api.createConversation()
          if (!alive) return
          items = [created.conversation]
          setConversations(items)
        }
        setCurrent(items[0].conversation_no)
        await loadConversation(items[0].conversation_no)
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err))
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [loadConversation, loadConversations])

  // 新内容进来就滚到底。用 requestAnimationFrame 等 DOM 更新完再滚，
  // 否则会滚到"上一条的高度"，表现是永远差一行
  useEffect(() => {
    requestAnimationFrame(() => {
      const el = scrollRef.current
      if (el) el.scrollTop = el.scrollHeight
    })
  }, [messages, live, pending])

  // ---------------------------------------------------------------- 事件处理
  const handleEvent = useCallback((event: ChatEvent) => {
    switch (event.type) {
      case 'message':
        setMessages((prev) => {
          if (prev.some((m) => m.id === event.message.id)) return prev
          return [...prev, event.message]
        })
        // 落库的助手正文和 live 里那份是同一段内容，别显示两遍
        if (event.message.role === 'assistant' && event.message.content) {
          setLive((prev) => ({ ...prev, text: '' }))
        }
        break
      case 'token':
        setLive((prev) => ({ ...prev, text: prev.text + event.text }))
        break
      case 'tool_call':
        setLive((prev) => ({
          ...prev,
          text: '',
          steps: [...prev.steps, { name: event.name, state: 'running' }],
        }))
        break
      case 'tool_result':
        setLive((prev) => {
          const steps = [...prev.steps]
          for (let i = steps.length - 1; i >= 0; i -= 1) {
            if (steps[i].name === event.name && steps[i].state === 'running') {
              steps[i] = { name: event.name, state: 'done', ok: event.ok }
              break
            }
          }
          return { ...prev, steps }
        })
        break
      case 'approval_required':
        setPending((prev) =>
          prev.some((a) => a.approval_no === event.approval.approval_no)
            ? prev
            : [...prev, event.approval],
        )
        break
      case 'approval_decided':
        setPending((prev) => prev.filter((a) => a.approval_no !== event.approval_no))
        break
      case 'error':
        setError(event.detail)
        break
      case 'ping':
      case 'task_created':
      case 'done':
        break
      default:
        break
    }
  }, [])

  const runStream = useCallback(
    async (start: (onEvent: (e: ChatEvent) => void, signal: AbortSignal) => Promise<void>) => {
      setError(null)
      setStreaming(true)
      setLive(EMPTY_LIVE)
      const controller = new AbortController()
      abortRef.current = controller
      try {
        await start(handleEvent, controller.signal)
      } catch (err) {
        // 主动中止不算错误 —— 用户点了"停止"，弹一个红字框是噪音
        if (controller.signal.aborted) return
        if (err instanceof ApiError) setError(err.message)
        else setError(err instanceof Error ? err.message : String(err))
      } finally {
        setStreaming(false)
        setLive(EMPTY_LIVE)
        abortRef.current = null
        // 重新拉一遍会话列表：标题、计数、成本这些汇总值变了
        loadConversations().catch(() => undefined)
        if (current) loadConversation(current).catch(() => undefined)
      }
    },
    [current, handleEvent, loadConversation, loadConversations],
  )

  const send = useCallback(async () => {
    const text = input.trim()
    if (!text || streaming || !current) return
    setInput('')
    // 乐观追加一条用户消息：等落库回来再显示会有一段"按了没反应"的空档
    const optimistic: ChatMessage = {
      id: -Date.now(),
      seq: Number.MAX_SAFE_INTEGER,
      role: 'user',
      content: text,
      tool_call_id: null,
      tool_name: null,
      tool_arguments: null,
      tool_result: null,
      tool_ok: null,
      tool_duration_ms: null,
      task_no: null,
      model: null,
      prompt_tokens: 0,
      completion_tokens: 0,
      cost: 0,
      latency_ms: null,
      finish_reason: null,
      interrupted: false,
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, optimistic])
    await runStream((onEvent, signal) => api.sendChatMessage(current, text, onEvent, signal))
  }, [current, input, runStream, streaming])

  const decide = useCallback(
    async (approval: ChatApproval, approved: boolean) => {
      if (!current || streaming) return
      await runStream((onEvent, signal) =>
        api.decideChatApproval(current, approval.approval_no, approved, '', onEvent, signal),
      )
    },
    [current, runStream, streaming],
  )

  const stop = useCallback(() => {
    abortRef.current?.abort()
    setStreaming(false)
  }, [])

  const newConversation = useCallback(async () => {
    const created = await api.createConversation()
    setConversations((prev) => [created.conversation, ...prev])
    setCurrent(created.conversation.conversation_no)
    setMessages([])
    setPending([])
    setLive(EMPTY_LIVE)
    inputRef.current?.focus()
  }, [])

  // 工具消息按 tool_call_id 归并到发起它的那条 assistant 消息上，
  // 这样"调用 + 结果"在界面上是一张卡片，而不是两条谁也不挨着的记录
  const toolResults = useMemo(() => {
    const map = new Map<string, ChatMessage>()
    for (const m of messages) {
      if (m.role === 'tool' && m.tool_call_id) map.set(m.tool_call_id, m)
    }
    return map
  }, [messages])

  const visible = messages.filter((m) => m.role !== 'tool')
  const pendingByMessage = useMemo(() => {
    const map = new Map<number, ChatApproval>()
    for (const a of pending) if (a.message_id) map.set(a.message_id, a)
    return map
  }, [pending])

  const currentConversation = conversations.find((c) => c.conversation_no === current)

  return (
    <div className="chat-page">
      <aside className="chat-list">
        <button className="button primary block" type="button" onClick={newConversation}>
          ＋ 新对话
        </button>
        <div className="chat-list-items">
          {conversations.map((c) => (
            <button
              key={c.conversation_no}
              type="button"
              className={`chat-list-item${c.conversation_no === current ? ' active' : ''}`}
              onClick={() => {
                setCurrent(c.conversation_no)
                setLive(EMPTY_LIVE)
                setError(null)
                loadConversation(c.conversation_no).catch((err: Error) => setError(err.message))
              }}
            >
              <div className="chat-list-title">{c.title || '新对话'}</div>
              <div className="chat-list-meta">
                {c.message_count} 条 · {c.updated_at?.slice(5, 16).replace('T', ' ')}
              </div>
            </button>
          ))}
        </div>
      </aside>

      <section className="chat-main">
        <header className="chat-head">
          <div>
            <h2>{currentConversation?.title || '对话'}</h2>
            <div className="chat-head-sub">
              {currentConversation
                ? `${currentConversation.llm_calls} 次模型调用 · ${currentConversation.tool_calls} 次工具调用 · ${currentConversation.total_tokens} tokens`
                : '加载中…'}
            </div>
          </div>
        </header>

        <div className="chat-scroll" ref={scrollRef}>
          {loading && <div className="chat-hint">正在加载…</div>}

          {!loading && visible.length === 0 && (
            <div className="chat-empty">
              <h3>问点什么</h3>
              <p>可以直接提问，也可以让它去干一件多步的事。</p>
              <div className="chat-samples">
                {[
                  'SKU00042 还有多少库存？',
                  '退货流程是怎么规定的？',
                  '最近 30 天哪个类目卖得最好？',
                  '帮我选 3 款秋季女装并出一份报表发给运营',
                ].map((sample) => (
                  <button
                    key={sample}
                    type="button"
                    className="prompt-chip"
                    onClick={() => setInput(sample)}
                  >
                    {sample}
                  </button>
                ))}
              </div>
            </div>
          )}

          {visible.map((m) => (
            <MessageRow
              key={m.id}
              message={m}
              toolResult={m.tool_call_id ? toolResults.get(m.tool_call_id) : undefined}
              pending={pendingByMessage.get(m.id)}
              streaming={streaming}
              onDecide={decide}
            />
          ))}

          {live.steps.length > 0 && (
            <div className="chat-row assistant">
              <div className="chat-tool-stack">
                {live.steps.map((step, index) => (
                  <div key={`${step.name}-${index}`} className={`tool-chip ${step.state}`}>
                    <span className="tool-chip-icon">
                      {step.state === 'running' ? '⏳' : step.ok ? '✓' : '✗'}
                    </span>
                    <span className="tool-chip-name">{step.name}</span>
                    <span className="tool-chip-state">
                      {step.state === 'running' ? '调用中…' : step.ok ? '成功' : '失败'}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {live.text && (
            <div className="chat-row assistant">
              <div className="bubble assistant">
                {live.text}
                <span className="caret" />
              </div>
            </div>
          )}

          {streaming && !live.text && live.steps.length === 0 && (
            <div className="chat-row assistant">
              <div className="bubble assistant thinking">思考中…</div>
            </div>
          )}
        </div>

        {error && (
          <div className="chat-error">
            <span>{error}</span>
            <button type="button" className="button small" onClick={() => setError(null)}>
              知道了
            </button>
          </div>
        )}

        <div className="chat-composer">
          <textarea
            ref={inputRef}
            value={input}
            rows={2}
            placeholder="说点什么…（Enter 发送，Shift+Enter 换行）"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void send()
              }
            }}
            disabled={loading}
          />
          {streaming ? (
            <button className="button danger" type="button" onClick={stop}>
              停止
            </button>
          ) : (
            <button
              className="button primary"
              type="button"
              onClick={() => void send()}
              disabled={!input.trim()}
            >
              发送
            </button>
          )}
        </div>
      </section>
    </div>
  )
}

// ---------------------------------------------------------------------------
function MessageRow({
  message,
  toolResult,
  pending,
  streaming,
  onDecide,
}: {
  message: ChatMessage
  toolResult?: ChatMessage
  pending?: ChatApproval
  streaming: boolean
  onDecide: (approval: ChatApproval, approved: boolean) => void
}) {
  if (message.role === 'user') {
    return (
      <div className="chat-row user">
        <div className="bubble user">{message.content}</div>
      </div>
    )
  }

  const isApprovalCard = message.finish_reason === 'approval_required'

  return (
    <div className="chat-row assistant">
      {message.tool_name && (
        <ToolChip name={message.tool_name} args={message.tool_arguments} result={toolResult} />
      )}

      {message.content && <div className="bubble assistant">{message.content}</div>}

      {pending && (
        <div className="approval-card">
          <div className="approval-head">
            <span className="approval-badge">需要你确认</span>
            <span className="approval-tool">{pending.tool_name}</span>
          </div>
          {/* 摘要由**工具自己**生成，不是模型编的 —— 让运营去读 JSON 再点
              "同意"，那不叫审批，叫背锅 */}
          <pre className="approval-preview">{pending.preview}</pre>
          <div className="approval-actions">
            <button
              className="button primary"
              type="button"
              disabled={streaming}
              onClick={() => onDecide(pending, true)}
            >
              确认执行
            </button>
            <button
              className="button"
              type="button"
              disabled={streaming}
              onClick={() => onDecide(pending, false)}
            >
              拒绝
            </button>
          </div>
        </div>
      )}

      {isApprovalCard && !pending && (
        <div className="approval-done">这张确认卡片已经处理过了</div>
      )}

      {toolResult?.task_no && (
        <Link className="task-card" to={`/tasks/${toolResult.task_no}`}>
          <div className="task-card-title">任务已启动</div>
          <div className="task-card-no">{toolResult.task_no}</div>
          <div className="task-card-sub">点这里看 DAG 与实时进度 →</div>
        </Link>
      )}
    </div>
  )
}

function ToolChip({
  name,
  args,
  result,
}: {
  name: string
  args: Record<string, unknown> | null
  result?: ChatMessage
}) {
  const ok = result?.tool_ok
  const state = result ? (ok ? 'done' : 'failed') : 'running'
  return (
    <details className={`tool-chip ${state}`}>
      <summary>
        <span className="tool-chip-icon">{result ? (ok ? '✓' : '✗') : '⏳'}</span>
        <span className="tool-chip-name">{name}</span>
        <span className="tool-chip-state">
          {result ? (ok ? '成功' : '失败') : '调用中…'}
        </span>
      </summary>
      <div className="tool-chip-body">
        <div className="tool-chip-label">入参</div>
        <pre>{JSON.stringify(args ?? {}, null, 2)}</pre>
        {result && (
          <>
            <div className="tool-chip-label">返回</div>
            <pre>
              {JSON.stringify(
                (result.tool_result as Record<string, unknown> | null) ?? result.content,
                null,
                2,
              ).slice(0, 2000)}
            </pre>
          </>
        )}
      </div>
    </details>
  )
}
