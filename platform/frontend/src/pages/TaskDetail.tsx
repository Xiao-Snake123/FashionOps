import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  api,
  openTaskStream,
  type Artifact,
  type TaskDetail as TaskDetailData,
  type TaskEvent,
  type Todo,
} from '../api'
import { num, StatusBadge } from '../ui'
import { currentUserSnapshot } from '../auth'

/**
 * 步骤状态 → 中文 + 徽标样式。
 *
 * `waiting_approval` 刻意**不**用失败色：它没出错，只是在等人点一下。
 * 把它显示成红色，看的人第一反应是"去查错误"，而实际该做的是去点确认。
 */
const TODO_STATUS: Record<string, { label: string; badge: string }> = {
  pending: { label: '待执行', badge: 'dim' },
  running: { label: '执行中', badge: 'accent' },
  done: { label: '已完成', badge: 'ok' },
  waiting_approval: { label: '等待确认', badge: 'warn' },
  failed: { label: '失败', badge: 'danger' },
  skipped: { label: '已跳过', badge: 'dim' },
}

/** kind 是"这一步需要哪类工具能力"，不是"哪个 Agent 来干" —— 执行者只有一个。 */
const TODO_KIND: Record<string, string> = {
  selection: '选品',
  analytics: '经营分析',
  notification: '通知',
  generic: '通用',
}

function TodoList({ todos }: { todos: Todo[] }) {
  return (
    <ol className="todo-list">
      {todos.map((todo) => {
        const status = TODO_STATUS[todo.status] ?? { label: todo.status, badge: 'dim' }
        return (
          <li className={`todo-item ${todo.status}`} key={todo.seq}>
            <div className="todo-head">
              <span className="todo-seq">{todo.seq}</span>
              <span className={`badge ${status.badge}`}>{status.label}</span>
              <span className="badge dim">{TODO_KIND[todo.kind] ?? todo.kind}</span>
              <span style={{ flex: 1, fontWeight: 600 }}>{todo.goal}</span>
              {todo.duration_ms !== null && (
                <span className="muted mono" style={{ fontSize: 11.5 }}>
                  {todo.duration_ms} ms · {todo.llm_calls} 次调用 · {num(todo.total_tokens)} token
                </span>
              )}
            </div>

            {/* 这一步的结论。它是下一步的输入 —— 让人看见它，才看得懂
                "为什么下一步会得出那个结论" */}
            {todo.result_summary && <div className="todo-result">{todo.result_summary}</div>}

            {Array.isArray(todo.tool_names) && todo.tool_names.length > 0 && (
              <div className="muted mono" style={{ fontSize: 11.5, marginTop: 5 }}>
                用过的工具：{todo.tool_names.join(' · ')}
              </div>
            )}

            {Array.isArray(todo.artifact_nos) && todo.artifact_nos.length > 0 && (
              <div className="muted mono" style={{ fontSize: 11.5, marginTop: 3 }}>
                产出素材：{todo.artifact_nos.join(' · ')}
              </div>
            )}

            {todo.error && (
              <div className="todo-error">失败原因：{todo.error}</div>
            )}

            {todo.status === 'skipped' && (
              <div className="muted" style={{ fontSize: 12, marginTop: 5 }}>
                上游没做成，这一步没有可依赖的输入，所以被跳过（而不是硬跑出一个
                看起来成功、实则基于缺失数据的结论）。
              </div>
            )}
          </li>
        )
      })}
    </ol>
  )
}

function Artifacts({ items }: { items: Artifact[] }) {
  if (items.length === 0) return null
  return (
    <div className="artifact-list">
      {items.map((item) => (
        <div className="artifact" key={item.ref}>
          <span className="badge dim">{item.kind}</span>
          <span className="name" title={item.name}>
            {item.name}
          </span>
          <span className="muted mono" style={{ fontSize: 11.5 }}>
            {item.ref}
          </span>
          <span className="muted" style={{ fontSize: 11.5, minWidth: 66, textAlign: 'right' }}>
            {item.size_bytes ? `${(item.size_bytes / 1024).toFixed(1)} KB` : ''}
          </span>
          {item.url ? (
            <a className="btn" href={item.url} download>
              下载
            </a>
          ) : (
            <span className="muted" style={{ fontSize: 11.5 }}>
              无下载地址
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

export default function TaskDetail() {
  const { taskNo = '' } = useParams()
  const [task, setTask] = useState<TaskDetailData | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [streamState, setStreamState] = useState<'idle' | 'live' | 'closed' | 'error'>('idle')

  // 审批表单
  const [reason, setReason] = useState('')
  const [deciding, setDeciding] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  // 已收到的最大 seq。用 ref 而不是 state：SSE 回调里要读最新值，
  // 但它不该触发重渲染，也不该进 useEffect 依赖（会让连接反复重开）
  const cursorRef = useRef(0)

  const loadTask = useCallback(
    () =>
      api
        .task(taskNo)
        .then((data) => {
          setTask(data)
          setError(null)
          return data
        })
        .catch((err: Error) => {
          setError(err.message)
          return null
        }),
    [taskNo],
  )

  // 首次加载：先拿一份全量 detail + 事件，再挂 SSE 只收增量。
  // 顺序很重要 —— 先挂 SSE 再拉全量的话，两者之间的空档会漏事件。
  useEffect(() => {
    let cancelled = false
    cursorRef.current = 0
    setEvents([])
    setStreamState('idle')

    Promise.all([api.task(taskNo), api.taskEvents(taskNo)])
      .then(([detail, history]) => {
        if (cancelled) return
        setTask(detail)
        setEvents(history)
        cursorRef.current = history.length > 0 ? history[history.length - 1].seq : 0
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message)
      })

    return () => {
      cancelled = true
    }
  }, [taskNo])

  // SSE：实时收新事件。只在还没跑完时挂 —— 已结束的任务没有新事件可等，
  // 挂上去只会让服务端白开一条一直空转的协程。
  const shouldStream = task !== null && (task.is_pending || task.is_running)

  useEffect(() => {
    if (!shouldStream) return

    setStreamState('live')
    const close = openTaskStream(taskNo, {
      sinceSeq: cursorRef.current,
      onEvent: (event) => {
        cursorRef.current = Math.max(cursorRef.current, event.seq)
        // 按 seq 去重：断线重连时可能重复收到同一批事件
        setEvents((prev) => (prev.some((e) => e.seq === event.seq) ? prev : [...prev, event]))
        // 事件到位后再拉一次详情，progress / current_node / 审批载荷就都更新了。
        // 只为这一件事多打一次接口，比在事件里重算全部业务状态可靠
        void loadTask()
      },
      onDone: () => {
        setStreamState('closed')
        void loadTask()
      },
      onError: (message) => {
        setStreamState('error')
        setError(message)
      },
    })

    return close
    // loadTask 是 useCallback 出来的，依赖它等同于依赖 taskNo，不会因此重开连接
  }, [shouldStream, taskNo, loadTask])

  const decide = async (approved: boolean) => {
    setDeciding(true)
    setActionError(null)
    try {
      await api.approveTask(taskNo, { approved, reason })
      setReason('')
      await loadTask()
    } catch (err) {
      setActionError((err as Error).message)
    } finally {
      setDeciding(false)
    }
  }

  if (error && !task) {
    return (
      <>
        <div className="error-box">{error}</div>
        <Link to="/tasks" className="btn">
          返回任务列表
        </Link>
      </>
    )
  }

  if (!task) return <div className="empty">加载中…</div>

  // 审批载荷是**平铺**的（后端把 payload 展开到顶层），所以直接读字段，
  // 不再套一层 .payload
  const approval = task.approval_payload
  const todos = task.todos ?? []
  const artifacts = ((task.result?.artifacts ?? []) as Artifact[]).filter(Boolean)
  const summary = typeof task.result?.summary === 'string' ? (task.result.summary as string) : null

  return (
    <>
      <div className="toolbar" style={{ justifyContent: 'space-between' }}>
        <div>
          <h2 className="page-title" style={{ marginBottom: 2 }}>
            {task.title} <StatusBadge status={task.status} />
          </h2>
          <div className="muted mono" style={{ fontSize: 12.5 }}>
            {task.task_no}
            {todos.length > 0 && (
              <>
                {' '}
                · 第 {todos.filter((t) => t.status === 'done').length}/{todos.length} 步
              </>
            )}
            {streamState === 'live' && (
              <span style={{ marginLeft: 12, color: 'var(--ok)' }}>
                <i className="live-dot" />
                实时更新中
              </span>
            )}
            {streamState === 'closed' && <span style={{ marginLeft: 12 }}>· 已结束</span>}
          </div>
        </div>
        <Link to="/tasks" className="btn">
          返回列表
        </Link>
      </div>

      {/* 人工闸口：做成常驻横幅而不是弹窗。弹窗可以被随手关掉，
          而“任务正卡在等你点确认”这件事关掉后就没地方看了 */}
      {approval && (
        <div className="banner">
          <h3>⏸ 等待人工确认 —— 任务已暂停在闸口</h3>
          {/* action 是后端写的人话描述，是这里最该被读到的字段 */}
          {approval.action && (
            <div style={{ fontSize: 14, marginBottom: 6 }}>{String(approval.action)}</div>
          )}
          <div className="muted" style={{ fontSize: 12.5 }}>
            第 <span className="mono">{String(approval?.todo_seq ?? '—')}</span> 步 · 工具{' '}
            <span className="mono">{String(approval?.tool ?? '—')}</span>
            {approval?.approval_no && (
              <>
                {' '}
                · 审批单 <span className="mono">{String(approval.approval_no)}</span>
              </>
            )}
            {approval.risk && (
              <>
                {' '}
                · 风险等级 <span className="mono">{String(approval.risk)}</span>
              </>
            )}
          </div>
          {/* reason 解释“为什么要人点”，不显示的话用户只能盲批 */}
          {approval.reason && (
            <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
              原因：{String(approval.reason)}
            </div>
          )}
          {/* preview 是代码按参数生成的人话摘要（`describe_preview`）。
              必须显示 —— 不看内容就点同意那不叫审批，叫背锅。 */}
          {approval?.preview && (
            <div className="draft">
              <div style={{ whiteSpace: 'pre-wrap' }}>{String(approval.preview)}</div>
            </div>
          )}
          {approval.question && (
            <div style={{ marginTop: 8, fontSize: 13 }}>{String(approval.question)}</div>
          )}
          <div className="actions">
            {/* 审批人**不再让前端输入**，而是显示当前登录用户。
                让前端传"我是谁"等于把审计变成自证 —— 谁都能以别人的名义
                批准一封要发出去的邮件，而 HITL 闸口存在的全部意义就是
                "**谁**批准了"。审批人由后端从访问令牌取。 */}
            <span className="approver-chip" title="审批人取自当前登录账号">
              审批人：{currentUserSnapshot()?.full_name || currentUserSnapshot()?.username || '未登录'}
            </span>
            <input
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="备注 / 驳回理由"
              style={{ width: 240 }}
            />
            <button className="btn primary" disabled={deciding} onClick={() => void decide(true)}>
              {deciding ? '处理中…' : '✓ 批准并继续'}
            </button>
            <button className="btn danger" disabled={deciding} onClick={() => void decide(false)}>
              驳回（终止任务）
            </button>
          </div>
          {actionError && (
            <div className="error-box" style={{ marginTop: 10, marginBottom: 0 }}>
              {actionError}
            </div>
          )}
        </div>
      )}

      {/* error_message 不一定是“任务挂了”：任务可能整体成功，但某一步的副作用被
          拦下了（比如邮件被闸门拦住）；也可能是人主动驳回。按终态分开说，
          别一律扣“失败原因”的帽子 —— 措辞错了，读的人就会去查不存在的问题 */}
      {task.error_message &&
        (task.status === 'failed' ? (
          <div className="banner danger">
            <h3>任务失败</h3>
            <div className="mono" style={{ fontSize: 12.5 }}>
              {task.error_message}
            </div>
          </div>
        ) : task.status === 'cancelled' ? (
          <div className="banner">
            <h3>任务已被人工取消</h3>
            <div style={{ fontSize: 12.5 }}>{task.error_message}</div>
          </div>
        ) : (
          <div className="banner">
            <h3>任务已完成，但有需要留意的地方</h3>
            <div style={{ fontSize: 12.5 }}>{task.error_message}</div>
          </div>
        ))}

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>用户原始需求</h3>
        <div>{task.user_request}</div>
        <div className="grid grid-4" style={{ marginTop: 14 }}>
          <div>
            <div className="stat-label">类型</div>
            <div>{task.task_type}</div>
          </div>
          <div>
            <div className="stat-label">当前步骤</div>
            <div className="mono">
              {todos.find((t) => t.status === 'running' || t.status === 'waiting_approval')?.goal ??
                task.current_node ??
                '—'}
            </div>
          </div>
          <div>
            <div className="stat-label">LLM 调用 / Token</div>
            <div>
              {task.llm_calls} 次 / {num(task.total_tokens)}
            </div>
          </div>
          <div>
            <div className="stat-label">累计成本</div>
            <div>¥{Number(task.total_cost).toFixed(4)}</div>
          </div>
        </div>
      </div>

      {/* 执行步骤。**线性列表，不是 DAG** ——
          真实需求就是"选品 → 出报表 → 发邮件"一条线，画成图只增加阅读成本。
          计划由代码按关键词拆（`agents/workplan.py`），所以提交后立刻就能看到。 */}
      <div className="card" style={{ marginBottom: 14 }}>
        <h3>
          执行步骤
          {todos.length > 0 && (
            <span className="muted" style={{ fontSize: 12.5, fontWeight: 400, marginLeft: 8 }}>
              共 {todos.length} 步 · 已完成 {todos.filter((t) => t.status === 'done').length} 步
            </span>
          )}
        </h3>
        {todos.length > 0 ? (
          <TodoList todos={todos} />
        ) : (
          <div className="empty">
            {task.is_pending
              ? '正在开始执行（第一步还没落库）…'
              : '这个任务没有步骤记录 —— 它可能是旧架构跑的（那时的计划存在 tasks.dag 里）。'}
          </div>
        )}
      </div>

      {(artifacts.length > 0 || summary) && (
        <div className="card" style={{ marginBottom: 14 }}>
          <h3>产出</h3>
          {summary && <div style={{ fontSize: 13 }}>{summary}</div>}
          <Artifacts items={artifacts} />
        </div>
      )}

      <div className="card">
        <h3>事件流（共 {events.length} 条）</h3>
        <div className="event-list">
          {events.map((event) => (
            <div className="event-row" key={event.seq}>
              <span className="event-seq">{event.seq}</span>
              <span className="event-type">
                <span className={`badge ${event.level === 'error' ? 'danger' : event.level === 'warn' ? 'warn' : 'dim'}`}>
                  {event.event_type}
                </span>
              </span>
              <span className="mono muted" style={{ minWidth: 96 }}>
                {event.node_name ?? '—'}
              </span>
              <span style={{ flex: 1 }}>{event.message}</span>
              <span className="event-time">
                {event.duration_ms !== null ? `${event.duration_ms} ms` : ''}
              </span>
            </div>
          ))}
          {events.length === 0 && <div className="empty">没有事件记录</div>}
        </div>
      </div>
    </>
  )
}
