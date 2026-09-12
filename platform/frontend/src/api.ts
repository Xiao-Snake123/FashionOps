/**
 * 后端 API 客户端。
 *
 * 约定：
 *  - 一律走相对路径（`/api/...`），由 Vite 的 proxy 转到后端。
 *    这样浏览器同源、完全不涉及 CORS，后端换端口也只改 vite.config.ts 一处。
 *  - 统一错误处理：非 2xx 抛 ApiError，页面层只关心 catch。
 */

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface DependencyStatus {
  name: string
  ok: boolean
  detail: string
  latency_ms: number | null
}

export interface Health {
  status: 'ok' | 'degraded' | 'down'
  app: string
  version: string
  env: string
  time: string
  dependencies: DependencyStatus[]
  config: Record<string, string>
}

export interface Summary {
  products: number
  skus: number
  orders: number
  gmv_30d: string
  return_rate_30d: string | null
  ad_spend_30d: string
  ad_roi_30d: string | null
  open_tickets: number
  escalated_tickets: number
  stockout_skus: number
  running_tasks: number
  failed_tasks: number
}

export interface DagStep {
  step_id: string
  agent: string
  goal: string
  depends_on: string[]
  approval_required: boolean
}

/**
 * 编排计划。
 *
 * 库里存过两种形状（seed 的 `{nodes, edges}` 与 W4 起的 `{steps}`），
 * 但后端在 `TaskOut` 的校验器里已经统一归一化了 ——
 * 所以前端只会看到 `steps`，`legacy` 只用来提示"这是演示数据"。
 */
export interface Dag {
  reasoning?: string
  steps: DagStep[]
  legacy?: boolean
}

export interface Task {
  id: number
  task_no: string
  title: string
  user_request: string
  task_type: string
  status: string
  current_node: string | null
  progress: number
  dag: Dag | null
  result: Record<string, unknown> | null
  error_message: string | null
  llm_calls: number
  total_tokens: number
  total_cost: string
  created_at: string
  finished_at: string | null
}

export interface TaskDetail extends Task {
  /** 非空 = 停在人工闸口，前端据此弹确认框 */
  approval_payload: ApprovalPayload | null
  /** 进程内是否有线程在跑（status 落库有延迟，光看它会误判"还在跑"） */
  is_running: boolean
  /** 还在迁移态，即"值得继续等" */
  is_pending: boolean
  started_at: string | null
}

/**
 * 需要人工确认的载荷，由后端 ``interrupt()`` 抛出（见 ``recorder.mark_waiting_approval``）。
 *
 * 字段来自两处拼接：闸口自己的（``action`` / ``reason`` / ``risk``）和计划步
 * 里的（``goal`` / ``agent`` / ``step_id``）。全部可选 —— 接少了不报错，
 * 只是界面上少显示一行。
 */
export interface ApprovalPayload {
  /** 人话描述“这一步要干什么”。审批横幅的主标题就用它 */
  action?: string
  /** 为什么需要人来点确认（风险说明） */
  reason?: string
  risk?: 'low' | 'medium' | 'high'
  goal?: string
  agent?: string
  step_id?: string
  /** 以下两个由通知 Agent 的草稿填，闸口在计划层时可能为空 */
  recipients?: string[]
  subject?: string
  body?: string
  question?: string
  [key: string]: unknown
}

/** 产物。`url` 由后端拼好，前端不该自己拼存储路径。 */
export interface Artifact {
  kind: string
  name: string
  ref: string
  url: string
  size_bytes?: number | null
}

export interface TaskEvent {
  id: number
  seq: number
  event_type: string
  node_name: string | null
  agent_name: string | null
  level: string
  message: string | null
  duration_ms: number | null
  created_at: string
  payload?: Record<string, unknown>
}

export interface EmailMessage {
  id: number
  message_no: string
  direction: string
  to_addrs: string[] | null
  cc_addrs: string[] | null
  bcc_addrs: string[] | null
  from_addr: string | null
  subject: string | null
  body_text: string | null
  attachments: unknown[] | null
  status: string
  gate_decision: string | null
  gate_reason: string | null
  error_message: string | null
  sent_at: string | null
  created_at: string
  /** 后端算好的：gate_decision 以 blocked 开头 */
  blocked: boolean
}

export interface EmailGateStats {
  by_decision: { decision: string; count: number }[]
  blocked_total: number
  total: number
}

export interface Product {
  id: number
  spu_code: string
  title: string
  category: string
  sub_category: string | null
  season: string | null
  style_tags: string[] | null
  cost_price: string | null
  list_price: string | null
  rating: string | null
  status: string
}

export interface Ticket {
  id: number
  ticket_no: string
  channel: string
  customer_nick: string | null
  order_no: string | null
  subject: string
  intent: string | null
  sentiment: string | null
  priority: string
  status: string
  handler: string
  escalated: boolean
  escalate_reason: string | null
  created_at: string
}

export interface Campaign {
  id: number
  campaign_no: string
  platform: string
  name: string
  objective: string
  budget_daily: string
  status: string
  spend: string
  clicks: number
  conversions: number
  revenue: string
  roi: string | null
}

export interface DailyMetric {
  metric_date: string
  category: string
  gmv: string
  order_count: number
  return_rate: string | null
  uv: number
  conversion_rate: string | null
  ad_spend: string
  ad_roi: string | null
}

export interface LowStockItem {
  sku_code: string
  color: string
  size: string
  product_title: string
  available: number
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly payload?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })

  if (!response.ok) {
    let payload: unknown
    try {
      payload = await response.json()
    } catch {
      payload = await response.text()
    }
    // 后端的 503 会带 hint 字段，直接透出来比显示 "Request failed" 有用得多
    const hint =
      typeof payload === 'object' && payload !== null && 'hint' in payload
        ? String((payload as { hint: unknown }).hint)
        : undefined
    throw new ApiError(
      hint ?? `请求失败：${response.status} ${response.statusText}`,
      response.status,
      payload,
    )
  }

  return (await response.json()) as T
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      search.set(key, String(value))
    }
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export interface TaskStreamHandlers {
  /** 从哪条 seq 之后开始收，默认 0（全部） */
  sinceSeq?: number
  onEvent: (event: TaskEvent) => void
  /** 任务跑到终态，服务端主动收尾 */
  onDone?: (info: { status: string; seq: number }) => void
  /** 只表示「连接层」出问题；业务失败走 onDone（status=failed） */
  onError?: (message: string) => void
}

/**
 * 订阅任务事件流，返回一个「取消订阅」函数。
 *
 * 用浏览器原生 `EventSource` 而不是 fetch + ReadableStream，原因是：
 * 断线重连、`Last-Event-ID` 续传、心跳保活都是它内建的 ——
 * 服务端只要按 seq 发 `id:`，重连后浏览器会自己把进度接上，
 * 前端一行重连逻辑都不用写。
 *
 * 代价：它只能发 GET、不能带自定义头（所以将来上鉴权只能靠 cookie）。
 *
 * 记得调用返回的函数关闭连接，否则页面切走后连接还挂着，
 * 服务端那条 SSE 协程要一直等到超时才结束。
 */
export function openTaskStream(taskNo: string, handlers: TaskStreamHandlers): () => void {
  const { sinceSeq = 0, onEvent, onDone, onError } = handlers
  const source = new EventSource(
    `/api/v1/tasks/${encodeURIComponent(taskNo)}/stream${qs({ since_seq: sinceSeq })}`,
  )
  let closed = false

  const close = () => {
    if (closed) return
    closed = true
    source.close()
  }

  source.onmessage = (message) => {
    let data: Record<string, unknown>
    try {
      data = JSON.parse(message.data) as Record<string, unknown>
    } catch {
      return // 半截帧之类的脏数据，丢掉即可，不该让整个页面崩
    }
    // 控制帧用 `type` 判别，数据帧用 `event_type`，两者不重叠
    if (data.type === 'done') {
      onDone?.({ status: String(data.status ?? ''), seq: Number(data.seq ?? 0) })
      close() // 服务端已收尾，不主动关的话浏览器会自动重连、白跑一轮
    } else if (data.type === 'task_error') {
      onError?.(String(data.detail ?? '任务事件流异常'))
      close()
    } else if (typeof data.event_type === 'string') {
      onEvent(data as unknown as TaskEvent)
    }
  }

  source.onerror = () => {
    // readyState=CLOSED 说明重连已被放弃（比如 404），这时才值得打扰用户。
    // 其余情况 EventSource 会自己重连，静默等待 —— 提前报错只会闪一下。
    if (source.readyState === EventSource.CLOSED) {
      onError?.('事件流连接已断开')
      close()
    }
  }

  return close
}

export interface TaskActionResponse {
  task_no: string
  status: string
  message: string
}

export const api = {
  health: () => request<Health>('/health'),
  summary: () => request<Summary>('/api/v1/summary'),
  meta: () => request<Record<string, unknown>>('/api/v1/meta'),

  products: (p: { page?: number; page_size?: number; category?: string; season?: string; q?: string } = {}) =>
    request<Page<Product>>(`/api/v1/products${qs(p)}`),
  productFacets: () =>
    request<{ categories: { value: string; count: number }[]; seasons: { value: string; count: number }[] }>(
      '/api/v1/products/categories',
    ),

  tasks: (p: { page?: number; page_size?: number; status?: string } = {}) =>
    request<Page<Task>>(`/api/v1/tasks${qs(p)}`),
  task: (taskNo: string) => request<TaskDetail>(`/api/v1/tasks/${encodeURIComponent(taskNo)}`),
  taskEvents: (taskNo: string, sinceSeq = 0) =>
    request<TaskEvent[]>(
      `/api/v1/tasks/${encodeURIComponent(taskNo)}/events${qs({ since_seq: sinceSeq })}`,
    ),

  /** 提交新任务。返回后它在后台线程跑，用 `openTaskStream` 跟进度。 */
  submitTask: (body: {
    user_request: string
    task_type?: string
    task_no?: string
    created_by?: string
  }) => request<TaskActionResponse>('/api/v1/tasks', { method: 'POST', body: JSON.stringify(body) }),

  /** 人工闸口的批复。`approved=false` 会把任务置为 cancelled。 */
  approveTask: (taskNo: string, body: { approved: boolean; by?: string; reason?: string }) =>
    request<TaskActionResponse>(`/api/v1/tasks/${encodeURIComponent(taskNo)}/approve`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  tickets: (p: { page?: number; page_size?: number; only_escalated?: boolean } = {}) =>
    request<Page<Ticket>>(`/api/v1/tickets${qs(p)}`),

  campaigns: (p: { page?: number; page_size?: number; only_losing?: boolean } = {}) =>
    request<Page<Campaign>>(`/api/v1/campaigns${qs(p)}`),

  emails: (p: { page?: number; page_size?: number; status?: string; only_blocked?: boolean } = {}) =>
    request<Page<EmailMessage>>(`/api/v1/emails${qs(p)}`),
  emailStats: () => request<EmailGateStats>('/api/v1/emails/stats'),

  dailyMetrics: (p: { category?: string; days?: number } = {}) =>
    request<DailyMetric[]>(`/api/v1/metrics/daily${qs(p)}`),

  lowStock: (threshold = 5) => request<LowStockItem[]>(`/api/v1/inventory/low-stock${qs({ threshold })}`),
}
