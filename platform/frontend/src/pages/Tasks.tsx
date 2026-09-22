import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type Task } from '../api'
import { num, PageHeader, ProgressBar, StatusBadge } from '../ui'

/**
 * 示例需求。点一下填进输入框 —— 这是**发现功能的主要入口**：
 * 用户不知道该问什么的时候，光给一个空文本框等于没告诉他这个系统能干什么。
 */
const EXAMPLES = [
  '帮我选 3 款秋季女装，再出一份近 30 天的经营分析报表发给 ops@example.com',
  '看一下最近 30 天的经营情况，把异常指标汇总后发邮件通知我',
  '挑 5 款适合上新的秋季连衣裙',
]

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [total, setTotal] = useState(0)
  const [status, setStatus] = useState('')
  const [error, setError] = useState<string | null>(null)

  const [request, setRequest] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const navigate = useNavigate()
  // 提交后要立刻打开一个「还没写进列表」的任务详情页，
  // 所以这里不能靠刷新列表，直接用后端返回的任务号跳走
  const submittingRef = useRef(false)

  const refresh = useCallback(
    () =>
      api
        .tasks({ page_size: 50, status: status || undefined })
        .then((page) => {
          setTasks(page.items)
          setTotal(page.total)
          setError(null)
        })
        .catch((e: Error) => setError(e.message)),
    [status],
  )

  useEffect(() => {
    void refresh()
  }, [refresh])

  const submit = async () => {
    const text = request.trim()
    if (text.length < 2 || submittingRef.current) return

    submittingRef.current = true
    setSubmitting(true)
    setSubmitError(null)
    try {
      const result = await api.submitTask({ user_request: text })
      setRequest('')
      navigate(`/tasks/${result.task_no}`)
    } catch (err) {
      setSubmitError((err as Error).message)
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '任务工作台']}
        title="任务工作台"
        badge={<span className="tag">共 {total} 条</span>}
        desc="用一句话交办需求，系统会按关键词把它拆成有序的步骤（选品 → 分析 → 通知）逐步执行；会发邮件这类不可撤销的动作会停下来等你点确认。点任意一行进入详情可以看到每一步的状态、结论与实时事件流。"
      />

      {error && <div className="error-box">{error}</div>}

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>交办新需求</h3>
        <label className="field-label" htmlFor="user-request">
          用中文描述你想让 Agent 做什么（可同时包含选品、分析、通知，系统会自己拆步骤）
        </label>
        <textarea
          id="user-request"
          value={request}
          onChange={(e) => setRequest(e.target.value)}
          disabled={submitting}
          placeholder="例如：帮我选 3 款秋季女装，再出一份近 30 天的经营分析报表发给 ops@example.com"
          // Ctrl/Cmd + Enter 提交：这是个多行输入框，单独按 Enter 应该是换行
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') void submit()
          }}
        />
        <div style={{ marginTop: 9 }}>
          {EXAMPLES.map((example) => (
            <span
              key={example}
              className="prompt-chip"
              onClick={() => !submitting && setRequest(example)}
              title="点击填入"
            >
              {example.length > 34 ? `${example.slice(0, 34)}…` : example}
            </span>
          ))}
        </div>
        <div className="toolbar" style={{ marginTop: 12, marginBottom: 0 }}>
          <button
            className="btn primary"
            disabled={submitting || request.trim().length < 2}
            onClick={() => void submit()}
          >
            {submitting ? '已提交，正在跳转…' : '提交任务'}
          </button>
          <span className="muted" style={{ fontSize: 12.5 }}>
            也可以按 Ctrl/⌘ + Enter 提交
          </span>
        </div>
        {submitError && (
          <div className="error-box" style={{ marginTop: 12, marginBottom: 0 }}>
            {submitError}
          </div>
        )}
      </div>

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
                    还没有任务。在上面填一句话交办一个，或用 <span className="mono">uv run fashionops-seed</span> 灌假数据
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
