import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api,
  ApiError,
  type AgentMemory,
  type ChatApproval,
  type ChatEvent,
  type ChatMessage,
  type Conversation,
} from '../api'
import { Icon, num } from '../ui'
import { ToolResultView } from '../resultViews'

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

/** 会话列表每页条数。 */
const CONVERSATION_PAGE_SIZE = 50

export default function Chat() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  /** 会话总数（后端真实值），用来判断还有没有下一页 */
  const [convTotal, setConvTotal] = useState(0)
  const [loadingMoreConv, setLoadingMoreConv] = useState(false)
  const [current, setCurrent] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [pending, setPending] = useState<ChatApproval[]>([])
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE)
  const [streaming, setStreaming] = useState(false)
  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [listStatus, setListStatus] = useState<'active' | 'archived' | 'all'>('active')
  const [memPanelOpen, setMemPanelOpen] = useState(false)
  /** 还有更早的历史可以翻（后端 limit 之后由 has_more 告知）。 */
  const [hasMore, setHasMore] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  /** 往上翻历史时置位：这一轮不要执行"滚到底"，否则等于白翻。 */
  const keepScrollRef = useRef(false)

  // ---------------------------------------------------------------- 数据加载
  /**
   * 拉会话列表。
   *
   * `offset > 0` 表示"再往后翻一页"，结果**追加**在后面；
   * 否则是重新拉首页，结果**替换**。
   *
   * 后端现在返回的是**真实总数**（以前是 `len(items)`，最多 50），
   * 所以这里能知道"还有没有下一页"。
   */
  const loadConversations = useCallback(async (status?: string, offset = 0) => {
    const data = await api.conversations({ limit: CONVERSATION_PAGE_SIZE, status, offset })
    setConvTotal(data.total)
    setConversations((prev) => (offset > 0 ? [...prev, ...data.items] : data.items))
    return data.items
  }, [])

  /** 翻下一页会话。`conversations.length` 就是下一页的起点 ——
   *  列表里每一行都对应服务端的一行，不存在"本地有、服务端没有"的项。 */
  const loadMoreConversations = useCallback(async () => {
    if (loadingMoreConv) return
    setLoadingMoreConv(true)
    try {
      await loadConversations(listStatus, conversations.length)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingMoreConv(false)
    }
  }, [conversations.length, listStatus, loadConversations, loadingMoreConv])

  /**
   * 拉取会话详情。
   *
   * 默认只取**最近**的一批（后端 `DEFAULT_HISTORY_LIMIT`）；
   * 传 `beforeSeq` 则是"往回翻更早的一批"，此时要把结果**拼在前面**
   * 而不是覆盖 —— 覆盖会让已经在屏幕上的新消息整段消失。
   */
  const loadConversation = useCallback(
    async (conversationNo: string, opts?: { beforeSeq?: number }) => {
      const data = await api.conversation(conversationNo, { before_seq: opts?.beforeSeq })
      if (opts?.beforeSeq) {
        setMessages((prev) => [...data.messages, ...prev])
      } else {
        setMessages(data.messages)
      }
      setHasMore(data.has_more)
      setPending(data.pending_approvals)
    },
    [],
  )

  /** 翻更早的消息。翻完要把视口挪回原位，不然会被自动滚动拽到底部。 */
  const loadOlder = useCallback(async () => {
    const oldest = messages[0]
    if (!current || loadingOlder || !hasMore || !oldest) return
    const el = scrollRef.current
    const prevHeight = el?.scrollHeight ?? 0
    const prevTop = el?.scrollTop ?? 0
    setLoadingOlder(true)
    try {
      keepScrollRef.current = true
      await loadConversation(current, { beforeSeq: oldest.seq })
      // DOM 更新后补偿新增内容的高度：用户视线停在原来的那条消息上
      requestAnimationFrame(() => {
        const node = scrollRef.current
        if (node) node.scrollTop = prevTop + (node.scrollHeight - prevHeight)
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingOlder(false)
    }
  }, [current, hasMore, loadConversation, loadingOlder, messages])

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
    // 翻历史那一次不滚到底 —— 见 keepScrollRef
    if (keepScrollRef.current) {
      keepScrollRef.current = false
      return
    }
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
          // 发送时先乐观插了一条用户消息（id 是**负数**占位值）。服务端落库后
          // 会把**同一条**用户消息再推回来，这时 id 才是真实值 —— 直接 append
          // 会让同一句话显示两遍（本次要修的就是这个）。所以先按"负 id + 同内容"
          // 认出那条占位，**就地替换**成落库版本；认不出才当新消息追加。
          if (event.message.role === 'user') {
            const at = prev.findIndex(
              (m) => m.id < 0 && m.role === 'user' && m.content === event.message.content,
            )
            if (at >= 0) {
              const next = [...prev]
              next[at] = event.message
              return next
            }
          }
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
        // 工具消息是**搭在 tool_result 事件里**下发的（不是 type=message）。
        // 必须自己收进 messages —— 否则"调用 + 结果"卡片永远拿不到返回内容。
        // 以前看不出问题，是因为每回合结束后会全量重拉会话把它补回来，
        // 代价是长对话每回合都要把整段历史重新拉一遍。
        if (event.message?.id) {
          setMessages((prev) =>
            prev.some((m) => m.id === event.message.id) ? prev : [...prev, event.message],
          )
        }
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
        // 只刷新**会话列表**：标题、计数、成本这些汇总值变了。
        //
        // 消息本身已经由 SSE 增量补齐（包括随 tool_result 下发的工具消息，
        // 见 handleEvent），所以不再重拉会话详情 —— 以前那一趟纯粹是为了
        // 补工具消息，代价是长会话每回合都要把整段历史再拉一遍。
        loadConversations().catch(() => undefined)
      }
    },
    [handleEvent, loadConversations],
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
    setHasMore(false)
    setLive(EMPTY_LIVE)
    inputRef.current?.focus()
  }, [])

  const toggleArchive = useCallback(
    async (c: Conversation) => {
      const archived = c.status === 'archived'
      try {
        if (archived) await api.unarchiveConversation(c.conversation_no)
        else await api.archiveConversation(c.conversation_no)
        // 列表按当前筛选条件刷新；正在看的这条若是它自己，也重新拉一下状态
        await loadConversations(listStatus)
        if (c.conversation_no === current) {
          loadConversation(current).catch(() => undefined)
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
    },
    [current, listStatus, loadConversations, loadConversation],
  )

  // 工具消息按 tool_call_id 归并到发起它的那条 assistant 消息上，
  // 这样"调用 + 结果"在界面上是一张卡片，而不是两条谁也不挨着的记录
  const toolResults = useMemo(() => {
    const map = new Map<string, ChatMessage>()
    for (const m of messages) {
      if (m.role === 'tool' && m.tool_call_id) map.set(m.tool_call_id, m)
    }
    return map
  }, [messages])

  // 工具消息要保留在 messages 里（结果卡片要用），只是不单独成行。
  // 包 useMemo：否则每次输入一个字、每次 token 到达都要重过滤一遍。
  const visible = useMemo(() => messages.filter((m) => m.role !== 'tool'), [messages])
  const pendingByMessage = useMemo(() => {
    const map = new Map<number, ChatApproval>()
    for (const a of pending) if (a.message_id) map.set(a.message_id, a)
    return map
  }, [pending])

  const currentConversation = conversations.find((c) => c.conversation_no === current)

  // 只给"最后一条助手消息"挂快捷操作，避免每条回答下面都堆一排按钮
  const lastAssistantId = useMemo(() => {
    for (let i = visible.length - 1; i >= 0; i -= 1) {
      if (visible[i].role === 'assistant') return visible[i].id
    }
    return null
  }, [visible])

  return (
    <div className="chat-page">
      <aside className="chat-list">
        <button className="button primary block" type="button" onClick={newConversation}>
          <Icon name="plus" size={15} />
          新对话
        </button>
        <div className="chat-filter">
          {(['active', 'archived', 'all'] as const).map((s) => (
            <button
              key={s}
              type="button"
              className={`chat-filter-btn${listStatus === s ? ' active' : ''}`}
              onClick={() => {
                setListStatus(s)
                loadConversations(s).catch((err: Error) => setError(err.message))
              }}
            >
              {s === 'active' ? '进行中' : s === 'archived' ? '已归档' : '全部'}
            </button>
          ))}
        </div>
        <div className="chat-list-items">
          {conversations.map((c) => (
            <div
              key={c.conversation_no}
              className={`chat-list-item${c.conversation_no === current ? ' active' : ''}`}
            >
              <button
                type="button"
                className="chat-list-main"
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
              <button
                type="button"
                className="chat-list-archive"
                title={c.status === 'archived' ? '取消归档' : '归档'}
                onClick={() => void toggleArchive(c)}
              >
                {c.status === 'archived' ? '↩' : '🗄'}
              </button>
            </div>
          ))}
        </div>

        {conversations.length < convTotal && (
          <button
            type="button"
            className="chat-list-more"
            disabled={loadingMoreConv}
            onClick={() => void loadMoreConversations()}
          >
            {loadingMoreConv
              ? '加载中…'
              : `加载更多（还有 ${convTotal - conversations.length} 个）`}
          </button>
        )}
      </aside>

      <section className="chat-main">
        <header className="chat-head">
          <div style={{ minWidth: 0 }}>
            <h2>{currentConversation?.title || '对话'}</h2>
            <div className="chat-head-sub">
              {currentConversation
                ? `${currentConversation.message_count} 条消息 · 创建于 ${currentConversation.created_at?.slice(0, 16).replace('T', ' ')}`
                : '加载中…'}
            </div>
            {currentConversation && (
              <div className="chat-head-tags">
                <span className={`badge ${currentConversation.status === 'archived' ? 'dim' : 'ok'}`}>
                  {currentConversation.status === 'archived' ? '已归档' : '进行中'}
                </span>
                <span className="tag">{currentConversation.llm_calls} 次模型调用</span>
                <span className="tag">{currentConversation.tool_calls} 次工具调用</span>
                <span className="tag accent">{num(currentConversation.total_tokens)} tokens</span>
              </div>
            )}
          </div>
          <div className="chat-head-actions">
            {currentConversation?.status === 'archived' && (
              <button
                type="button"
                className="button small"
                onClick={() => currentConversation && void toggleArchive(currentConversation)}
              >
                <Icon name="archive" size={14} />
                取消归档
              </button>
            )}
            <button type="button" className="button small" onClick={() => setMemPanelOpen(true)}>
              <Icon name="memory" size={14} />
              记忆
            </button>
          </div>
        </header>

        <div className="chat-scroll" ref={scrollRef}>
          {loading && <div className="chat-hint">正在加载…</div>}

          {/* 后端只回最近一批，更早的按需翻 —— 长会话不用为了看最新几句
              就把整段历史（含工具返回的大段 JSON）全量拉下来 */}
          {!loading && hasMore && (
            <div className="chat-load-older">
              <button
                type="button"
                className="button small"
                disabled={loadingOlder}
                onClick={() => void loadOlder()}
              >
                {loadingOlder ? '加载中…' : '↑ 加载更早的消息'}
              </button>
            </div>
          )}

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
              showActions={!streaming && m.id === lastAssistantId}
              onDecide={decide}
            />
          ))}

          {live.steps.length > 0 && (
            <div className="chat-row assistant">
              <div className="msg-avatar assistant-avatar">
                <Icon name="sparkle" size={15} />
              </div>
              <div className="msg-body">
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
            </div>
          )}

          {live.text && (
            <div className="chat-row assistant">
              <div className="msg-avatar assistant-avatar">
                <Icon name="sparkle" size={15} />
              </div>
              <div className="msg-body">
                <div className="bubble assistant">
                  {live.text}
                  <span className="caret" />
                </div>
              </div>
            </div>
          )}

          {streaming && !live.text && live.steps.length === 0 && (
            <div className="chat-row assistant">
              <div className="msg-avatar assistant-avatar">
                <Icon name="sparkle" size={15} />
              </div>
              <div className="msg-body">
                <div className="bubble assistant thinking">思考中…</div>
              </div>
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
            rows={1}
            placeholder="说点什么…"
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
            <button className="send-button stop" type="button" onClick={stop} title="停止">
              <Icon name="close" size={17} />
            </button>
          ) : (
            <button
              className="send-button"
              type="button"
              onClick={() => void send()}
              disabled={!input.trim()}
              title="发送（Enter）"
            >
              <Icon name="send" size={17} />
            </button>
          )}
        </div>
        <div className="composer-foot">
          <span>Agent 可能会调用工具并产生费用，写操作会先找你确认。</span>
          <span>
            <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行
          </span>
        </div>
      </section>

      {memPanelOpen && <MemoriesPanel onClose={() => setMemPanelOpen(false)} />}
    </div>
  )
}

// ---------------------------------------------------------------------------
/** 长期记忆面板：看清 Agent 记住了关于你的哪些事，并可以"让它忘掉"。 */
function MemoriesPanel({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<AgentMemory[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.memories()
      setItems(data.items)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const remove = async (m: AgentMemory) => {
    setBusyId(m.id)
    try {
      await api.deleteMemory(m.id)
      setItems((prev) => prev.filter((x) => x.id !== m.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  const kindLabel: Record<string, string> = { preference: '偏好', fact: '事实', note: '备注' }

  return (
    <div className="mem-mask" onClick={onClose}>
      <div className="mem-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="mem-head">
          <h3>长期记忆</h3>
          <div className="mem-head-actions">
            <button type="button" className="button small" onClick={() => void load()}>
              刷新
            </button>
            <button type="button" className="button small" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>
        <div className="mem-body">
          {loading && <div className="chat-hint">加载中…</div>}
          {!loading && error && <div className="chat-error"><span>{error}</span></div>}
          {!loading && !error && items.length === 0 && (
            <div className="mem-empty">
              还没有记住任何关于你的事。在对话里告诉它你的偏好（例如"报表只发 CSV"），
              它会记住并在以后自动照做。
            </div>
          )}
          {!loading &&
            items.map((m) => (
              <div key={m.id} className="mem-card">
                <div className="mem-card-head">
                  <span className="mem-topic">{m.topic}</span>
                  <span className="badge dim">{kindLabel[m.kind] ?? m.kind}</span>
                </div>
                <div className="mem-content">{m.content}</div>
                <div className="mem-foot">
                  <span className="muted">更新于 {m.updated_at?.slice(0, 10)}</span>
                  <button
                    type="button"
                    className="button small danger"
                    disabled={busyId === m.id}
                    onClick={() => void remove(m)}
                  >
                    {busyId === m.id ? '删除中…' : '忘记'}
                  </button>
                </div>
              </div>
            ))}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
function MessageRow({
  message,
  toolResult,
  pending,
  streaming,
  showActions,
  onDecide,
}: {
  message: ChatMessage
  toolResult?: ChatMessage
  pending?: ChatApproval
  streaming: boolean
  showActions: boolean
  onDecide: (approval: ChatApproval, approved: boolean) => void
}) {
  if (message.role === 'user') {
    return (
      <div className="chat-row user">
        <div className="msg-avatar user-avatar">我</div>
        <div className="msg-body">
          <div className="bubble user">{message.content}</div>
        </div>
      </div>
    )
  }

  const isApprovalCard = message.finish_reason === 'approval_required'

  return (
    <div className="chat-row assistant">
      <div className="msg-avatar assistant-avatar">
        <Icon name="sparkle" size={15} />
      </div>
      <div className="msg-body">
        {message.tool_name && (
          <ToolChip name={message.tool_name} args={message.tool_arguments} result={toolResult} />
        )}

        {/* 工具返回的数据独立渲染成卡片 —— 数字来自 tool_result 本身，
            不是模型转述的。模型说错话，卡片上的数字照样是对的。 */}
        {toolResult && (
          <ToolResultView
            tool={toolResult.tool_name ?? message.tool_name}
            data={toolResult.tool_result}
          />
        )}

        {message.content && (
          <>
            {/* 数据来源就是这条消息真实调用过的工具 —— 不是装饰，
                让运营一眼看清"这个数字是从哪来的" */}
            {message.tool_name && (
              <div className="msg-sources">
                <span className="msg-sources-label">数据来源</span>
                <span className="tag accent">
                  <span className="tag-dot" />
                  {message.tool_name}
                </span>
              </div>
            )}
            <div className="bubble assistant">{message.content}</div>
          </>
        )}

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

        {/* 模型自己不确定、问了一句 —— 和审批卡片是两回事：
            那个要"点确认/拒绝"，这个只要用文字回答。 */}
        {message.finish_reason === 'awaiting_user' && (
          <div className="chat-awaiting">等你回答 —— 直接在下面输入即可</div>
        )}

        {toolResult?.task_no && (
          <Link className="task-card" to={`/tasks/${toolResult.task_no}`}>
            <div className="task-card-title">任务已启动</div>
            <div className="task-card-no">{toolResult.task_no}</div>
            <div className="task-card-sub">点这里看 DAG 与实时进度 →</div>
          </Link>
        )}

        {showActions && message.content && <MessageActions content={message.content} />}
      </div>
    </div>
  )
}

/** 最后一条回答下面的"接下来可以"。两个动作都是真的：复制、跳转。 */
function MessageActions({ content }: { content: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      /* 浏览器不给剪贴板权限就静默失败，不值得弹错误 */
    }
  }
  return (
    <div className="msg-actions">
      <button type="button" className="msg-action" onClick={() => void copy()}>
        <Icon name={copied ? 'check' : 'book'} size={14} />
        {copied ? '已复制' : '复制回答'}
      </button>
      <Link className="msg-action" to="/tasks">
        <Icon name="tasks" size={14} />
        查看任务进度
      </Link>
      <Link className="msg-action" to="/dashboard">
        <Icon name="dashboard" size={14} />
        打开数据看板
      </Link>
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
