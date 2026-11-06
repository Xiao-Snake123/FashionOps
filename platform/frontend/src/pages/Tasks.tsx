import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { api, type Task } from '../api'
import { PageHeader, ProgressBar, StatusBadge } from '../ui'

/**
 * 示例需求。点一下填进输入框 —— 这是**发现功能的主要入口**：
 * 用户不知道该问什么的时候，光给一个空文本框等于没告诉他这个系统能干什么。
 */
const EXAMPLES = [
  '帮我选 3 款秋季女装，再出一份近 30 天的经营分析报表发给 ops@example.com',
  '看一下最近 30 天的经营情况，把异常指标汇总后发邮件通知我',
  '挑 5 款适合上新的秋季连衣裙',
]

/** 任务类型的中文名。后端存的是英文枚举，直接展示会漏出实现细节。 */
const TYPE_LABELS: Record<string, string> = {
  new_product_launch: '选品分析',
  daily_report: '经营日报',
  after_sale: '工单处理',
  ad_optimize: '广告巡检',
  generic: '通用任务',
}

type FilterKey = 'all' | 'active' | 'failed' | 'done' | 'cancelled'

/** 逻辑筛选 → 后端状态集合。后端一次只能按一个 status 过滤，这里合并。 */
const STATUS_GROUPS: Record<FilterKey, string[]> = {
  all: [],
  active: ['running', 'waiting_approval', 'pending'],
  failed: ['failed'],
  done: ['succeeded'],
  cancelled: ['cancelled'],
}

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'active', label: '进行中' },
  { key: 'failed', label: '失败' },
  { key: 'done', label: '已完成' },
  { key: 'cancelled', label: '已取消' },
]

/** 凭证失效类错误不该盲目重跑 —— 重跑也还是 401。 */
function isCredentialFailure(message: string | null): boolean {
  return /凭证|授权|401|认证|api.?key/i.test(message ?? '')
}

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [total, setTotal] = useState(0)
  const [filter, setFilter] = useState<FilterKey>('all')
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [error, setError] = useState<string | null>(null)

  const [request, setRequest] = useState('')
  const location = useLocation()
  /**
   * 从别的模块跳过来时带的需求草稿（比如商品库的「建补货任务」）。
   *
   * 为什么不走查询串：需求是**一整句话**，塞进 URL 会被截断、还要编码，
   * 而且它会留在浏览器历史里。用 router state 只在内存里传一次。
   */
  const draft = (location.state as { draft?: string } | null)?.draft ?? ''
  const [draftApplied, setDraftApplied] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState<string | null>(null)
  const [batchRetrying, setBatchRetrying] = useState(false)
  const navigate = useNavigate()
  // 提交后要立刻打开一个「还没写进列表」的任务详情页，
  // 所以这里不能靠刷新列表，直接用后端返回的任务号跳走
  const submittingRef = useRef(false)

  // 带草稿进来时填进输入框。**只填一次**：否则用户清空输入框后，
  // 任何一次重渲染都会把草稿再塞回来，表现为"删不掉"。
  useEffect(() => {
    if (draft && !draftApplied) {
      setRequest(draft)
      setDraftApplied(true)
    }
  }, [draft, draftApplied])

  /** 按逻辑筛选拉列表。多个状态时合并结果再按时间倒序。 */
  const fetchList = useCallback(async (key: FilterKey) => {
    const statuses = STATUS_GROUPS[key]
    if (statuses.length === 0) {
      const page = await api.tasks({ page_size: 50 })
      return { items: page.items, total: page.total }
    }
    const pages = await Promise.all(statuses.map((s) => api.tasks({ page_size: 50, status: s })))
    const items = pages.flatMap((p) => p.items)
    items.sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
    return { items: items.slice(0, 50), total: pages.reduce((s, p) => s + p.total, 0) }
  }, [])

  const refresh = useCallback(
    () =>
      fetchList(filter)
        .then(({ items, total: t }) => {
          setTasks(items)
          setTotal(t)
          setError(null)
        })
        .catch((e: Error) => setError(e.message)),
    [fetchList, filter],
  )

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 各筛选 chip 的计数。分别只取第一页的 total，不拉明细。
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const entries = await Promise.all(
          FILTERS.map(async (f) => {
            const statuses = STATUS_GROUPS[f.key]
            if (statuses.length === 0) {
              return [f.key, (await api.tasks({ page_size: 1 })).total] as const
            }
            const pages = await Promise.all(
              statuses.map((s) => api.tasks({ page_size: 1, status: s })),
            )
            return [f.key, pages.reduce((sum, p) => sum + p.total, 0)] as const
          }),
        )
        if (!alive) return
        setCounts(Object.fromEntries(entries))
      } catch {
        /* 计数失败不打扰主列表 */
      }
    })()
    return () => {
      alive = false
    }
  }, [filter])

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

  const retryTask = useCallback(
    async (t: Task) => {
      setRetrying(t.task_no)
      try {
        // 后端约定：传已存在的 task_no 即重跑该任务
        await api.submitTask({ user_request: t.user_request, task_no: t.task_no })
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setRetrying(null)
      }
    },
    [refresh],
  )

  const retryAllFailed = useCallback(async () => {
    setBatchRetrying(true)
    try {
      const failed = tasks.filter((t) => t.status === 'failed' && !isCredentialFailure(t.error_message))
      for (const t of failed) {
        await api.submitTask({ user_request: t.user_request, task_no: t.task_no })
      }
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBatchRetrying(false)
    }
  }, [refresh, tasks])

  const costTotal = tasks.reduce((s, t) => s + Number(t.total_cost || 0), 0)

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
        <div className="filter-chips">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`filter-chip${filter === f.key ? ' active' : ''}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
              <span className="filter-chip-count">{counts[f.key] ?? '…'}</span>
            </button>
          ))}
        </div>
        {tasks.some((t) => t.status === 'failed') && (
          <button
            type="button"
            className="btn small"
            disabled={batchRetrying}
            onClick={() => void retryAllFailed()}
          >
            {batchRetrying ? '重跑中…' : '批量重跑失败任务'}
          </button>
        )}
        <span className="muted" style={{ fontSize: 12.5 }}>
          本页 {tasks.length} 条 · 合计成本 ¥{costTotal.toFixed(2)}
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
                <th style={{ width: 150 }}>进度</th>
                <th>成本</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr
                  key={task.task_no}
                  className="clickable"
                  onClick={() => navigate(`/tasks/${task.task_no}`)}
                >
                  <td className="mono">{task.task_no}</td>
                  <td>{task.title}</td>
                  <td className="muted">{TYPE_LABELS[task.task_type] ?? task.task_type}</td>
                  <td>
                    <StatusBadge status={task.status} />
                  </td>
                  <td>
                    <ProgressBar value={task.progress} status={task.status} />
                    {task.status === 'failed' && (
                      <div className="task-fail-reason" title={task.error_message ?? ''}>
                        失败于 {task.current_node ?? '未知节点'}：{task.error_message ?? '未知原因'}
                      </div>
                    )}
                  </td>
                  <td>¥{Number(task.total_cost).toFixed(2)}</td>
                  <td style={{ textAlign: 'right' }}>
                    {task.status === 'failed' &&
                      (isCredentialFailure(task.error_message) ? (
                        <Link className="button small" to="/accounts">
                          去授权
                        </Link>
                      ) : (
                        <button
                          type="button"
                          className="button small"
                          disabled={retrying === task.task_no}
                          onClick={(e) => {
                            e.stopPropagation()
                            void retryTask(task)
                          }}
                        >
                          {retrying === task.task_no ? '提交中…' : '重跑'}
                        </button>
                      ))}
                  </td>
                </tr>
              ))}
              {tasks.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty">
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
