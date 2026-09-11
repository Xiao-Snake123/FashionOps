import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type Task } from '../api'
import { num, ProgressBar, StatusBadge } from '../ui'

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [total, setTotal] = useState(0)
  const [status, setStatus] = useState('')
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    api
      .tasks({ page_size: 50, status: status || undefined })
      .then((page) => {
        setTasks(page.items)
        setTotal(page.total)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [status])

  return (
    <>
      <h2 className="page-title">任务工作台</h2>
      <p className="page-desc">
        点任意一行进入任务详情，可以看到编排 DAG 与逐步事件流。共 {total} 条。
      </p>

      {error && <div className="error-box">{error}</div>}

      <div className="toolbar">
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">全部状态</option>
          <option value="succeeded">成功</option>
          <option value="running">运行中</option>
          <option value="waiting_approval">等待人工确认</option>
          <option value="failed">失败</option>
          <option value="pending">待执行</option>
          <option value="cancelled">已取消</option>
        </select>
        <span className="muted" style={{ fontSize: 12.5 }}>
          失败任务的 error_message 是错误归因的输入
        </span>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>任务号</th>
                <th>需求</th>
                <th>类型</th>
                <th>状态</th>
                <th>当前节点</th>
                <th style={{ width: 130 }}>进度</th>
                <th>LLM 调用</th>
                <th>Token</th>
                <th>成本</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.task_no} className="clickable" onClick={() => navigate(`/tasks/${task.task_no}`)}>
                  <td className="mono">{task.task_no}</td>
                  <td>{task.title}</td>
                  <td className="muted">{task.task_type}</td>
                  <td>
                    <StatusBadge status={task.status} />
                  </td>
                  <td className="mono muted">{task.current_node ?? '—'}</td>
                  <td>
                    <ProgressBar value={task.progress} status={task.status} />
                  </td>
                  <td>{task.llm_calls}</td>
                  <td>{num(task.total_tokens)}</td>
                  <td>¥{Number(task.total_cost).toFixed(4)}</td>
                </tr>
              ))}
              {tasks.length === 0 && (
                <tr>
                  <td colSpan={9} className="empty">
                    没有任务。先跑 <span className="mono">uv run fashionops-seed</span> 灌假数据
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
