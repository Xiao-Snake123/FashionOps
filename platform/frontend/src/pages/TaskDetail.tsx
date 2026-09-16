import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Background, Controls, ReactFlow, Position, type Edge, type Node } from '@xyflow/react'
import {
  api,
  openTaskStream,
  type Artifact,
  type Dag,
  type TaskDetail as TaskDetailData,
  type TaskEvent,
} from '../api'
import { num, StatusBadge } from '../ui'
import { currentUserSnapshot } from '../auth'

const NODE_W = 158
const NODE_H = 64
const GAP_X = 62
const GAP_Y = 26

type NodeState = 'done' | 'running' | 'failed' | 'waiting'

/**
 * 按拓扑层级给节点算坐标。
 *
 * 为什么不用 dagre 之类的自动布局：这个 DAG 是流水线形状（每个节点最多一个前驱），
 * 自己算层级的代码只有十几行，还能完全控制间距 —— 引一个布局库反而更重。
 *
 * 多前驱的情形这里也能算（取所有前驱层级的最大值），只是不保证连线交叉最少。
 * 真到图复杂了再换自动布局。
 */
function layout(dag: Dag): Map<string, { x: number; y: number }> {
  const incoming = new Map<string, string[]>()
  for (const step of dag.steps) incoming.set(step.step_id, [])
  for (const step of dag.steps) {
    for (const dep of step.depends_on ?? []) incoming.get(step.step_id)?.push(dep)
  }

  const depth = new Map<string, number>()
  const resolve = (id: string, seen = new Set<string>()): number => {
    if (depth.has(id)) return depth.get(id)!
    if (seen.has(id)) return 0 // 防御：数据有环时别无限递归
    seen.add(id)
    const parents = incoming.get(id) ?? []
    const value = parents.length === 0 ? 0 : Math.max(...parents.map((p) => resolve(p, seen))) + 1
    depth.set(id, value)
    return value
  }
  for (const step of dag.steps) resolve(step.step_id)

  // 按层级分桶，桶内保持计划里的声明顺序
  const buckets = new Map<number, string[]>()
  for (const step of dag.steps) {
    const level = depth.get(step.step_id) ?? 0
    if (!buckets.has(level)) buckets.set(level, [])
    buckets.get(level)!.push(step.step_id)
  }

  const positions = new Map<string, { x: number; y: number }>()
  for (const [level, ids] of buckets) {
    ids.forEach((id, index) => {
      positions.set(id, { x: level * (NODE_W + GAP_X), y: index * (NODE_H + GAP_Y) })
    })
  }
  return positions
}

/** 从事件流反推每个节点的状态。 */
function deriveNodeStates(events: TaskEvent[]): Map<string, NodeState> {
  const states = new Map<string, NodeState>()
  for (const event of events) {
    const node = event.node_name
    if (!node) continue
    switch (event.event_type) {
      case 'node_finished':
        states.set(node, 'done')
        break
      case 'approval_required':
        states.set(node, 'waiting')
        break
      case 'task_failed':
        states.set(node, 'failed')
        break
      default:
        // node_start 之类不直接映射成 running —— 只靠事件流判断“谁在跑”
        // 并不可靠（最后一条 node_start 未必还是当前节点），交给
        // 后端的 current_node 字段
        break
    }
  }
  return states
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

  const nodeState = useMemo(() => {
    const states = deriveNodeStates(events)
    // current_node 是后端记的“正在执行的节点”，比从事件流猜准
    const current = task?.current_node
    if (current && task?.status !== 'succeeded' && task?.status !== 'failed') {
      states.set(current, states.get(current) === 'done' ? 'done' : 'running')
    }
    return states
  }, [events, task])

  const { nodes, edges } = useMemo(() => {
    if (!task?.dag?.steps?.length) return { nodes: [] as Node[], edges: [] as Edge[] }
    const positions = layout(task.dag)
    const nodes: Node[] = task.dag.steps.map((step) => {
      const state = nodeState.get(step.step_id)
      const classes = ['dag-node']
      if (step.approval_required) classes.push('gate')
      if (state) classes.push(state)
      return {
        id: step.step_id,
        position: positions.get(step.step_id) ?? { x: 0, y: 0 },
        data: {
          label: (
            <div>
              <div style={{ fontWeight: 600 }}>{step.step_id}</div>
              <div className="agent">
                {step.approval_required ? `${step.agent} · 需确认` : step.agent}
              </div>
            </div>
          ),
        },
        className: classes.join(' '),
        style: { width: NODE_W, height: NODE_H, padding: 0 },
        // 显式指定端口方向，否则默认是上下端口，横向排列时会绕成“从底部出发再回到底部”的弧线
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        draggable: true,
      }
    })
    const edges: Edge[] = task.dag.steps.flatMap((step) =>
      (step.depends_on ?? []).map((dep) => ({
        id: `${dep}->${step.step_id}`,
        source: dep,
        target: step.step_id,
        animated: nodeState.get(dep) === 'done' && nodeState.get(step.step_id) === 'running',
        style: { stroke: '#3b4a63' },
      })),
    )
    return { nodes, edges }
  }, [task, nodeState])

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

  const approval = task.approval_payload
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
            {task.task_no} · thread_id={task.task_no}
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
            由 <span className="mono">{String(approval.agent ?? '—')}</span> 在{' '}
            <span className="mono">{String(approval.step_id ?? '—')}</span> 触发
            {approval.risk && (
              <>
                {' '}
                · 风险等级 <span className="mono">{String(approval.risk)}</span>
              </>
            )}
            {Array.isArray(approval.recipients) && approval.recipients.length > 0 && (
              <>
                {' '}
                · 收件人 <span className="mono">{approval.recipients.join(', ')}</span>
              </>
            )}
          </div>
          {/* reason 解释“为什么要人点”，不显示的话用户只能盲批 */}
          {approval.reason && (
            <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
              原因：{String(approval.reason)}
            </div>
          )}
          {(approval.subject || approval.body) && (
            <div className="draft">
              {approval.subject && (
                <div style={{ fontWeight: 600 }}>主题：{String(approval.subject)}</div>
              )}
              {approval.body && <div style={{ marginTop: 6 }}>{String(approval.body)}</div>}
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
            <div className="stat-label">当前节点</div>
            <div className="mono">{task.current_node ?? '—'}</div>
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

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>编排计划</h3>
        {task.dag?.reasoning && (
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 10 }}>
            规划理由：{task.dag.reasoning}
          </div>
        )}
        {task.dag?.legacy && (
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 10 }}>
            这是 seed 灌入的演示任务，DAG 形状已在后端归一化。
          </div>
        )}
        <div className="dag-legend">
          <span>
            <i className="dot" style={{ background: 'var(--ok)' }} />
            已完成
          </span>
          <span>
            <i className="dot" style={{ background: 'var(--accent)' }} />
            执行中
          </span>
          <span>
            <i className="dot" style={{ background: 'var(--danger)' }} />
            失败
          </span>
          <span>
            <i className="dot" style={{ background: 'var(--warn)' }} />
            人工确认闸口（LangGraph interrupt）
          </span>
        </div>
        {nodes.length > 0 ? (
          <div style={{ height: 340, border: '1px solid var(--border)', borderRadius: 8 }}>
            <ReactFlow nodes={nodes} edges={edges} fitView proOptions={{ hideAttribution: true }}>
              <Background color="#222c3e" gap={18} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        ) : (
          // 刚提交的任务在 plan 节点跑完前是没有 DAG 的（模型拆解要 15~30 秒）。
          // 这里得说一句「在生成」，否则一块空白看起来像坏了
          <div className="empty">
            {task.is_pending ? '正在生成计划（模型拆解需求中，约 15~30 秒）…' : '这个任务没有编排计划'}
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
