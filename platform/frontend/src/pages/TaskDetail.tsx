import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Background, Controls, ReactFlow, Position, type Edge, type Node } from '@xyflow/react'
import { api, type Dag, type Task, type TaskEvent } from '../api'
import { num, StatusBadge } from '../ui'

const NODE_W = 158
const NODE_H = 64
const GAP_X = 62
const GAP_Y = 26

/**
 * 按拓扑层级给节点算坐标。
 *
 * 为什么不用 dagre 之类的自动布局：这个 DAG 是流水线形状（每个节点最多一个前驱），
 * 自己算层级的代码只有十几行，还能完全控制间距 —— 引一个布局库反而更重。
 * 图变复杂了（多前驱、需要最小化连线交叉）再换自动布局。
 */
function layout(dag: Dag): Map<string, { x: number; y: number }> {
  const depth = new Map<string, number>()
  const incoming = new Map<string, string[]>()
  for (const node of dag.nodes) incoming.set(node.id, [])
  for (const [from, to] of dag.edges) incoming.get(to)?.push(from)

  const resolve = (id: string, seen = new Set<string>()): number => {
    if (depth.has(id)) return depth.get(id)!
    if (seen.has(id)) return 0 // 防御：数据有问题时别无限递归
    seen.add(id)
    const parents = incoming.get(id) ?? []
    const value = parents.length === 0 ? 0 : Math.max(...parents.map((p) => resolve(p, seen))) + 1
    depth.set(id, value)
    return value
  }
  for (const node of dag.nodes) resolve(node.id)

  // 按层级分组，组内按 DAG 声明顺序排列
  const buckets = new Map<number, string[]>()
  for (const node of dag.nodes) {
    const level = depth.get(node.id) ?? 0
    if (!buckets.has(level)) buckets.set(level, [])
    buckets.get(level)!.push(node.id)
  }

  const positions = new Map<string, { x: number; y: number }>()
  for (const [level, ids] of buckets) {
    ids.forEach((id, index) => {
      positions.set(id, { x: level * (NODE_W + GAP_X), y: index * (NODE_H + GAP_Y) })
    })
  }
  return positions
}

export default function TaskDetail() {
  const { taskNo = '' } = useParams()
  const [task, setTask] = useState<Task | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.task(taskNo), api.taskEvents(taskNo)])
      .then(([t, e]) => {
        setTask(t)
        setEvents(e)
        setError(null)
      })
      .catch((err: Error) => setError(err.message))
  }, [taskNo])

  /** 每个节点的执行状态：成功跑完 / 正在跑 / 失败。 */
  const nodeState = useMemo(() => {
    const state = new Map<string, 'done' | 'running' | 'failed'>()
    for (const event of events) {
      if (!event.node_name) continue
      if (event.event_type === 'node_end') state.set(event.node_name, 'done')
      if (event.event_type === 'node_start') state.set(event.node_name, 'running')
      if (event.event_type === 'error') state.set(event.node_name, 'failed')
    }
    return state
  }, [events])

  const { nodes, edges } = useMemo(() => {
    if (!task?.dag) return { nodes: [] as Node[], edges: [] as Edge[] }
    const positions = layout(task.dag)
    const nodes: Node[] = task.dag.nodes.map((node) => {
      const state = nodeState.get(node.id)
      const classes = ['dag-node']
      if (node.interrupt) classes.push('gate')
      if (state) classes.push(state)
      return {
        id: node.id,
        position: positions.get(node.id) ?? { x: 0, y: 0 },
        data: {
          label: (
            <div>
              <div style={{ fontWeight: 600 }}>{node.name}</div>
              <div className="agent">
                {node.interrupt ? '人工闸口' : node.agent}
              </div>
            </div>
          ),
        },
        className: classes.join(' '),
        style: { width: NODE_W, height: NODE_H, padding: 0 },
        // 显式指定端口方向，否则默认是上下端口，横向排列时会绕成"从底部出发再回到底部"的弧线
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        draggable: true,
      }
    })
    const edges: Edge[] = task.dag.edges.map(([from, to]) => ({
      id: `${from}->${to}`,
      source: from,
      target: to,
      animated: nodeState.get(from) === 'done' && nodeState.get(to) === 'running',
      style: { stroke: '#3b4a63' },
    }))
    return { nodes, edges }
  }, [task, nodeState])

  if (error) {
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

  return (
    <>
      <div className="toolbar" style={{ justifyContent: 'space-between' }}>
        <div>
          <h2 className="page-title" style={{ marginBottom: 2 }}>
            {task.title}{' '}
            <StatusBadge status={task.status} />
          </h2>
          <div className="muted mono" style={{ fontSize: 12.5 }}>
            {task.task_no} · thread_id={task.task_no}
          </div>
        </div>
        <Link to="/tasks" className="btn">
          返回列表
        </Link>
      </div>

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
        {task.error_message && (
          <div className="error-box" style={{ marginTop: 14, marginBottom: 0 }}>
            失败原因：{task.error_message}
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>编排 DAG</h3>
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
        <div style={{ height: 340, border: '1px solid var(--border)', borderRadius: 8 }}>
          <ReactFlow nodes={nodes} edges={edges} fitView proOptions={{ hideAttribution: true }}>
            <Background color="#222c3e" gap={18} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      </div>

      <div className="card">
        <h3>事件流（共 {events.length} 条）</h3>
        <div className="event-list">
          {events.map((event) => (
            <div className="event-row" key={event.id}>
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
