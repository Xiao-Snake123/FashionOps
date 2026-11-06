/**
 * 后端 API 客户端。
 *
 * 约定：
 *  - 一律走相对路径（`/api/...`），由 Vite 的 proxy 转到后端。
 *    这样浏览器同源、完全不涉及 CORS，后端换端口也只改 vite.config.ts 一处。
 *  - 统一错误处理：非 2xx 抛 ApiError，页面层只关心 catch。
 *  - **认证与 401 续期在这里统一做掉**，页面层不需要知道令牌的存在。
 */

import {
  currentAccessToken,
  currentRefreshToken,
  expireSession,
  saveSession,
  type TokenPair,
  type User,
} from './auth'

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

/**
 * 任务里的一步。**它同时就是计划和进度** ——
 *
 * 旧架构里这两个是分开的：计划来自 `tasks.dag`（静态的图），进度要靠
 * 从事件流反推节点状态（间接，还得处理"最后一条 node_start 未必是当前
 * 节点"这种猜测）。新架构的 `task_todos` 既有目标又有状态和结果，
 * 所以前端只需要这一份数据，不必再猜。
 *
 * `kind` 是"这一步需要哪类工具能力"（selection / analytics / notification /
 * generic），**不是**"谁在执行" —— 执行者只有一个 Agent。
 */
export interface Todo {
  seq: number
  goal: string
  kind: string
  /** pending / running / done / waiting_approval / failed / skipped */
  status: string
  /** 这一步的结论（模型写的，也给下一步当输入） */
  result_summary: string | null
  artifact_nos: string[] | null
  tool_names: string[] | null
  error: string | null
  /** 卡在闸口时指向 approvals.approval_no */
  approval_no: string | null
  llm_calls: number
  total_tokens: number
  total_cost: string
  duration_ms: number | null
  started_at: string | null
  finished_at: string | null
}

/**
 * 关联单据：这个任务在别的模块里留下了什么。
 *
 * `href` 由后端拼好 —— "哪些模块之间有关联"是业务知识，
 * 放在前端等于复制一份，而它本来就在后端那几张外键里。
 */
export interface RelatedDoc {
  kind: string
  label: string
  ref: string
  href: string
}

export interface TaskDetail extends Task {
  /** 非空 = 停在人工闸口，前端据此弹确认框 */
  approval_payload: ApprovalPayload | null
  /** 进程内是否有线程在跑（status 落库有延迟，光看它会误判"还在跑"） */
  is_running: boolean
  /** 还在迁移态，即"值得继续等" */
  is_pending: boolean
  started_at: string | null
  /** 有序的 todo 列表。旧任务没有 todo 行，会是空数组 */
  todos: Todo[]
  /** 关联单据（广告计划 / 邮件 / 商品 / 工单），可直接跳转 */
  related_docs: RelatedDoc[]
}

/**
 * 需要人工确认的载荷（见 `recorder.mark_waiting_approval`）。
 *
 * 闸口挂在**某一次工具调用**上：人批准的正是"这次调用、这些参数"，
 * 所以 `payload` 里带着工具名、完整参数和人话摘要 `preview` ——
 * 界面必须把它们显示出来，否则就是在让人盲批。
 */
export interface ApprovalPayload {
  /** 人话描述“这一步要干什么”。审批横幅的主标题就用它 */
  action?: string
  /** 为什么需要人来点确认（风险说明） */
  reason?: string
  risk?: 'low' | 'medium' | 'high'
  /**
   * 载荷是**平铺**的（`recorder.mark_waiting_approval` 把 payload 展开到顶层）。
   * 不要写成 `payload.xxx` —— 那样读到的全是 undefined，
   * 表现为"审批卡片上没有内容"，而接口明明给了。
   */
  todo_seq?: number
  tool?: string
  arguments?: Record<string, unknown>
  /** 由 `describe_preview` 生成的人话摘要 —— 不是模型写的 */
  preview?: string
  approval_no?: string
  /** 以下字段旧架构用过，保留兼容：接少了不报错，只是少显示一行 */
  goal?: string
  agent?: string
  step_id?: string
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

/** 类目退货率。**基准区间由后端给**，前端不写死 25%~40% ——
 *  否则图表上的基准线和表格里的判定迟早对不上。 */
export interface CategoryReturnRate {
  category: string
  return_rate: number | null
  gmv: number
  refund_amount: number
  over_baseline: boolean
}

export interface ReturnRateByCategory {
  days: number
  baseline: { low: number; high: number }
  items: CategoryReturnRate[]
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
  /** 当前可售库存（由库存流水推出，不是余额字段） */
  stock: number
  /** 近 30 天销量（动销），已排除退款/取消的订单 */
  sold_count: number
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
  // ---- 客服 Agent 的产出 ----
  content?: string | null
  resolution?: string | null
  confidence?: number | null
  cited_article_nos?: string[] | null
  agent_handled_at?: string | null
  agent_lane?: string | null
  // ---- 人工侧 ----
  assignee?: string | null
  assigned_at?: string | null
  human_status?: string | null
  human_note?: string | null
  human_updated_at?: string | null
}

/**
 * 工单**看板**统计。和下面的 `TicketStats`（批量处理的那一轮结果）不是一回事，
 * 所以名字要分开 —— 两个同名类型在一个文件里会让调用方悄悄用错那个。
 *
 * 只看 status 得不到"有没有人在跟"，那是 assignee 的事。
 */
export interface TicketBoardStats {
  total: number
  unassigned: number
  by_status: Record<string, number>
  by_human_status: Record<string, number>
  by_assignee: Record<string, number>
}

export interface HumanStatusOption {
  value: string
  label: string
}

/** 一条工单的处理结果（SSE 事件里带的 `outcome`）。 */
export interface TicketOutcome {
  ticket_no: string
  lane: string
  lane_reason: string
  intent: string
  sentiment: string
  /** 意图/情绪是谁定的：`rule` 或 `model`。**这个必须显示** ——
   *  看不到来源就分不清该调规则还是该调 prompt */
  intent_source: string
  sentiment_source: string
  escalated: boolean
  escalate_reason: string
  reply_draft: string
  cited_article_nos: string[]
  confidence: number
  steps: number
  tool_calls: number
  tokens: number
  cost: number
  duration_ms: number
  error: string
}

export interface TicketStats {
  total: number
  fast_lane: number
  agent_lane: number
  escalated: number
  failed: number
  tokens: number
}

export type TicketEvent =
  | { type: 'start'; total: number; tickets?: string[] }
  | { type: 'ticket'; index?: number; total?: number; outcome: TicketOutcome }
  | { type: 'done'; stats: TicketStats }
  | { type: 'error'; detail: string }

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

/** 广告处置结果。**后端是唯一裁判**：状态对不对、能不能改，都由它说了算，
 *  前端只负责把 message 显示出来。 */
export interface CampaignActionResult {
  campaign_no: string
  status: string
  budget_daily: string | null
  approved_by: string | null
  message: string
}

/** 邮件策略。**`overrides` 说明哪些值不是 .env 来的** ——
 *  否则运维看到的白名单和配置里写的不一致，会以为是同步出了 bug。 */
export interface MailPolicy {
  enabled: boolean
  dry_run: boolean
  from: string
  allowlist: string[]
  denylist: string[]
  max_recipients: number
  rate_limit_per_minute: number
  max_attachment_bytes: number
  attachment_exts: string[]
  smtp_host: string
  smtp_configured: boolean
  overrides: Record<string, unknown>
  override_source: string
  overridable_keys?: string[]
  accepted?: Record<string, unknown>
  errors?: string[]
}

export interface MailRetryResult {
  message_no: string
  status: string
  sent: boolean
  decision: string
  reason: string
  hint: string
  backend: string | null
  retry_count: number
}

export interface LoginEvent {
  id: number
  username: string
  user_id: number | null
  success: boolean
  reason: string | null
  auth_method: string
  ip: string | null
  user_agent: string | null
  created_at: string | null
  anomalies: string[]
  anomaly_note: string
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

/**
 * 从响应体里取"给人看的那句话"。
 *
 * 后端三种错误形状都要照顾到：
 *  - `{detail: "..."}` —— 大多数业务错误（含 HTTPException）
 *  - `{detail: [{msg, loc}]}` —— pydantic 校验失败，是**数组**
 *  - `{hint: "..."}` —— 503 那类，hint 比 detail 有用得多
 *
 * 只处理第一种的话，参数校验失败会显示成 `[object Object]`。
 */
function extractMessage(payload: unknown, fallback: string): string {
  if (typeof payload === 'string' && payload.trim()) return payload
  if (typeof payload !== 'object' || payload === null) return fallback

  const body = payload as Record<string, unknown>

  if (typeof body.hint === 'string' && body.hint) return body.hint

  const detail = body.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    const first = detail[0] as Record<string, unknown> | undefined
    if (first && typeof first.msg === 'string') return first.msg
  }
  if (typeof body.message === 'string' && body.message) return body.message
  return fallback
}

/** 正在进行的刷新请求。单飞：并发的 401 只会触发**一次** refresh。 */
let refreshInFlight: Promise<boolean> | null = null

async function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight

  refreshInFlight = (async () => {
    const token = currentRefreshToken()
    if (!token) return false
    try {
      // 用**裸 fetch**，不走 request() —— 否则刷新失败会再触发一次刷新，无限递归
      const response = await fetch('/api/v1/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: token }),
      })
      if (!response.ok) return false
      saveSession((await response.json()) as TokenPair)
      return true
    } catch {
      // 网络错误也算刷新失败。**不要**在这里清会话 ——
      // 断网一秒就把用户登出，体验极差；让调用方决定（它会返回错误，用户可以重试）
      return false
    } finally {
      refreshInFlight = null
    }
  })()

  return refreshInFlight
}

async function rawRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const token = currentAccessToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(path, { ...init, headers })

  if (!response.ok) {
    let payload: unknown
    const text = await response.text()
    try {
      payload = text ? JSON.parse(text) : undefined
    } catch {
      payload = text
    }
    throw new ApiError(
      extractMessage(payload, `请求失败：${response.status} ${response.statusText}`),
      response.status,
      payload,
    )
  }

  if (response.status === 204) return undefined as T
  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

/**
 * 带认证与自动续期的请求。
 *
 * ## 401 的处理顺序（顺序很重要）
 *
 * 1. 收到 401 → 如果**有 refresh token**，刷新一次 access token 然后重试原请求
 * 2. 刷新成功 → 用户完全无感（他只多等了一次往返）
 * 3. 刷新失败 → 清会话 + 通知上层跳登录页
 *
 * 为什么不直接跳登录页：access token 只活 30 分钟，用户每半小时被打断一次，
 * 而他手上的 refresh token 明明还有效 —— 这是纯粹的体验损失。
 *
 * ## 为什么只重试一次
 *
 * 刷新后的新 token 如果还是 401，说明问题不在"过期"（而是被禁用、密钥变了、
 * 权限被撤）。再试下去就会变成**无限循环**，而表现是浏览器一直转圈。
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    return await rawRequest<T>(path, init)
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) throw error

    // 登录/刷新这两个接口的 401 是**业务结果**（密码错、refresh 失效），
    // 不该触发"刷新后重试" —— 那会让输错密码变成一次多余的 refresh 请求。
    if (path.includes('/auth/login') || path.includes('/auth/refresh')) throw error

    const refreshed = await refreshSession()
    if (!refreshed) {
      expireSession()
      throw error
    }
    try {
      return await rawRequest<T>(path, init)
    } catch (retryError) {
      if (retryError instanceof ApiError && retryError.status === 401) {
        expireSession()
      }
      throw retryError
    }
  }
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
 * 代价：它只能发 GET、不能带自定义头。
 *
 * **所以 token 只能走查询串 `?token=`。** 这是原生 EventSource 的硬限制，
 * 不是偷懒。代价是这个 token 会进浏览器历史、Referer、nginx access log ——
 * 所以：
 *  - 只传 **access token**（30 分钟过期），绝不传 refresh token
 *  - 后端只在 SSE 这一个端点上接受查询串令牌（见 `security/deps.py`）
 *
 * 记得调用返回的函数关闭连接，否则页面切走后连接还挂着，
 * 服务端那条 SSE 协程要一直等到超时才结束。
 */
export function openTaskStream(taskNo: string, handlers: TaskStreamHandlers): () => void {
  const { sinceSeq = 0, onEvent, onDone, onError } = handlers
  const source = new EventSource(
    `/api/v1/tasks/${encodeURIComponent(taskNo)}/stream${qs({
      since_seq: sinceSeq,
      token: currentAccessToken() ?? undefined,
    })}`,
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

// ===========================================================================
// 对话
// ===========================================================================
export interface Conversation {
  conversation_no: string
  title: string
  status: string
  username: string
  model: string
  message_count: number
  llm_calls: number
  tool_calls: number
  total_tokens: number
  total_cost: number
  last_message_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ChatMessage {
  id: number
  seq: number
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  tool_call_id: string | null
  tool_name: string | null
  tool_arguments: Record<string, unknown> | null
  tool_result: Record<string, unknown> | null
  tool_ok: boolean | null
  tool_duration_ms: number | null
  task_no: string | null
  model: string | null
  prompt_tokens: number
  completion_tokens: number
  cost: number
  latency_ms: number | null
  finish_reason: string | null
  interrupted: boolean
  created_at: string | null
}

export interface ChatApproval {
  id: number
  approval_no: string
  conversation_id: number
  message_id: number | null
  tool_call_id: string
  tool_name: string
  tool_arguments: Record<string, unknown> | null
  preview: string
  reason: string
  status: string
  decided_by: string | null
  decided_at: string | null
  decision_reason: string | null
  execution_result: Record<string, unknown> | null
  created_at: string | null
}

/**
 * 对话事件。**用同一个 `type` 字段区分**，不给每种类型注册一个监听器 ——
 * 后端新增一种事件时前端不会静默漏掉（它至少会进 default 分支）。
 */
export type ChatEvent =
  | { type: 'message'; message: ChatMessage }
  | { type: 'token'; text: string }
  | { type: 'tool_call'; name: string; arguments: Record<string, unknown> }
  | { type: 'tool_result'; name: string; ok: boolean; message: ChatMessage }
  | { type: 'task_created'; task_no: string }
  | { type: 'approval_required'; approval: ChatApproval }
  | { type: 'approval_decided'; approval_no: string; approved: boolean; tool: string; ok: boolean }
  | { type: 'done'; status: string; steps?: number }
  | { type: 'ping' }
  | { type: 'error'; detail: string }

/**
 * 走 fetch 读 SSE 流 —— **通用版**，对话和工单处理共用。
 *
 * ## 为什么不用原生 EventSource
 *
 * `EventSource` 只能发 GET，也不能带自定义请求头 —— 为了传令牌只能塞进
 * 查询串，而查询串会进访问日志、浏览器历史和 Referer。用 fetch 就能正常带
 * `Authorization`，代价只是自己解析 SSE 帧（几行 split）。
 *
 * ## 401 要自己处理
 *
 * 因为它没走 `request()` 那条带自动续期的路径（那条会 `await` 完整响应体，
 * 而这里要的是流）。所以这里手工做一次"刷新后重试"，行为保持一致。
 */
async function postSse<T>(
  path: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const send = async (): Promise<Response> => {
    const headers = new Headers({ 'Content-Type': 'application/json' })
    const token = currentAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    return fetch(path, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal,
    })
  }

  let response = await send()
  if (response.status === 401) {
    const refreshed = await refreshSession()
    if (!refreshed) {
      expireSession()
      throw new ApiError('登录已过期，请重新登录', 401, null)
    }
    response = await send()
  }

  if (!response.ok) {
    let payload: unknown
    const text = await response.text()
    try {
      payload = text ? JSON.parse(text) : undefined
    } catch {
      payload = text
    }
    if (response.status === 401) expireSession()
    throw new ApiError(
      extractMessage(payload, `请求失败：${response.status} ${response.statusText}`),
      response.status,
      payload,
    )
  }
  if (!response.body) throw new ApiError('响应没有可读的流', response.status, null)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  // SSE 用空行分隔帧。中间件可能把 `\n` 改写成 `\r\n`，所以先归一化 ——
  // 不归一化的话 `indexOf('\n\n')` 永远不命中，表现是"一个字都不显示"。
  const flush = (raw: string): void => {
    for (const line of raw.split('\n')) {
      if (!line.startsWith('data: ')) continue
      try {
        onEvent(JSON.parse(line.slice(6)) as T)
      } catch {
        /* 半截 JSON：丢掉，不要因为一个坏帧把整条流打断 */
      }
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let index = buffer.indexOf('\n\n')
    while (index >= 0) {
      flush(buffer.slice(0, index))
      buffer = buffer.slice(index + 2)
      index = buffer.indexOf('\n\n')
    }
  }
  if (buffer.trim()) flush(buffer)
}

export interface AgentMemory {
  id: number
  topic: string
  content: string
  kind: string
  username: string
  source_conversation_no: string | null
  created_at: string | null
  updated_at: string | null
}

// ---------------------------------------------------------------------------
// 账号管理（分级授权）
// ---------------------------------------------------------------------------
export interface AdminAccount {
  user_id: number
  username: string
  full_name: string
  email: string | null
  phone: string | null
  status: string
  is_superuser: boolean
  department_id: number | null
  roles: string[]
  permissions: string[]
  last_login_at: string | null
  created_at: string | null
  /** 我能不能管这个账号。**不是安全边界** —— 后端每次写操作都会重判 */
  can_manage: boolean
  /** 不能管的原因（给人看的一句话）。`can_manage` 为真时为 null */
  manage_blocked_reason: string | null
  is_self: boolean
}

export interface AdminDepartment {
  department_id: number
  code: string
  name: string
  description: string
  is_active: boolean
}

export interface AdminRole {
  code: string
  name: string
  description: string
  permissions: string[]
}

export interface AdminScopeInfo {
  global: boolean
  department_id: number | null
  label: string
  is_superuser: boolean
  can_create: boolean
}

export interface AdminStats {
  total: number
  active: number
  disabled: number
  locked: number
  pending: number
  superusers: number
}

export interface AdminAccountsResponse {
  scope: AdminScopeInfo
  total: number
  /** 整个作用域的状态统计（不受 status 筛选影响）。前端算不出来 —— 只有当前页 */
  stats: AdminStats
  items: AdminAccount[]
  departments: AdminDepartment[]
  /** 我**能授予**的角色。超出自己权限范围的角色不会出现在这里 */
  assignable_roles: AdminRole[]
}

export const api = {
  health: () => request<Health>('/health'),
  summary: () => request<Summary>('/api/v1/summary'),
  meta: () => request<Record<string, unknown>>('/api/v1/meta'),

  // ---- 认证 ----
  /** 登录。**不经过 request() 的 401 重试** —— 密码错的 401 是业务结果，
   *  不该触发一次多余的刷新。 */
  login: (username: string, password: string) =>
    rawRequest<TokenPair>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: (refreshToken: string | null) =>
    request<{ revoked: boolean; message: string }>('/api/v1/auth/logout', {
      method: 'POST',
      body: JSON.stringify({ refresh_token: refreshToken }),
    }),
  me: () => request<User>('/api/v1/auth/me'),
  /** 直接刷新并落盘。给“应用启动时校验会话”用。 */
  refresh: async (): Promise<TokenPair | null> => {
    const token = currentRefreshToken()
    if (!token) return null
    try {
      const pair = await rawRequest<TokenPair>('/api/v1/auth/refresh', {
        method: 'POST',
        body: JSON.stringify({ refresh_token: token }),
      })
      saveSession(pair)
      return pair
    } catch {
      return null
    }
  },
  providers: () =>
    request<{
      providers: { provider: string; configured: boolean; implemented: boolean; hint: string }[]
      note: string
      enabled: boolean
    }>('/api/v1/auth/providers'),

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

  /** 提交新任务。返回后它在后台线程跑，用 `openTaskStream` 跟进度。
   *
   *  **不再传 created_by** —— 提交人由后端从访问令牌里取。
   *  让前端声明"我是谁"等于把审计变成自证。
   */
  submitTask: (body: { user_request: string; task_type?: string; task_no?: string }) =>
    request<TaskActionResponse>('/api/v1/tasks', { method: 'POST', body: JSON.stringify(body) }),

  /** 人工闸口的批复。`approved=false` 会把任务置为 cancelled。
   *
   *  **同样不传 `by`** —— 审批人由后端从令牌取。
   */
  approveTask: (taskNo: string, body: { approved: boolean; reason?: string }) =>
    request<TaskActionResponse>(`/api/v1/tasks/${encodeURIComponent(taskNo)}/approve`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /**
   * 工单列表。
   *
   * `only_mine` **不接收用户名** —— 后端按访问令牌里的身份过滤。
   * 让前端传"我是谁"等于允许任何人改一个参数就看别人的待办。
   */
  tickets: (
    p: {
      page?: number
      page_size?: number
      only_escalated?: boolean
      assignee?: string
      only_mine?: boolean
      only_unassigned?: boolean
    } = {},
  ) => request<Page<Ticket>>(`/api/v1/tickets${qs(p)}`),

  campaigns: (p: { page?: number; page_size?: number; only_losing?: boolean } = {}) =>
    request<Page<Campaign>>(`/api/v1/campaigns${qs(p)}`),

  /**
   * 就地批准。只接受 `pending_approval` 的计划 ——
   * 别的后端会给 409，前端把那条 message 显示出来即可，不要自己判状态。
   */
  approveCampaign: (campaignNo: string, reason?: string) =>
    request<CampaignActionResult>(`/api/v1/campaigns/${encodeURIComponent(campaignNo)}/approve`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason ?? null }),
    }),
  rejectCampaign: (campaignNo: string, reason: string) =>
    request<CampaignActionResult>(`/api/v1/campaigns/${encodeURIComponent(campaignNo)}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
  /** 一键止损。亏损按分钟累计，所以它不需要再走一轮审批。 */
  pauseCampaign: (campaignNo: string, reason?: string) =>
    request<CampaignActionResult>(`/api/v1/campaigns/${encodeURIComponent(campaignNo)}/pause`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason ?? null }),
    }),
  changeBudget: (campaignNo: string, budgetDaily: number, reason?: string) =>
    request<CampaignActionResult>(`/api/v1/campaigns/${encodeURIComponent(campaignNo)}/budget`, {
      method: 'POST',
      body: JSON.stringify({ budget_daily: budgetDaily, reason: reason ?? null }),
    }),

  // ---- 邮件闸门运维 ----
  mailPolicy: () => request<MailPolicy>('/api/v1/mail/policy'),
  /** 改策略。传 null 的字段 = 恢复 .env 的值（不是清空）。 */
  updateMailPolicy: (patch: {
    mail_enabled?: boolean | null
    mail_dry_run?: boolean | null
    mail_send_allowlist?: string | null
    mail_send_denylist?: string | null
    mail_max_recipients?: number | null
    mail_rate_limit_per_minute?: number | null
  }) => request<MailPolicy>('/api/v1/mail/policy', { method: 'PATCH', body: JSON.stringify(patch) }),
  resetMailPolicy: () =>
    request<{ ok: boolean; cleared: Record<string, unknown>; policy: MailPolicy }>(
      '/api/v1/mail/policy/overrides',
      { method: 'DELETE' },
    ),
  /** 重发。**会重新过一遍闸门** —— 上次被白名单拦下、加完白名单再重发，走的正是这条路。 */
  retryEmail: (messageNo: string) =>
    request<MailRetryResult>(`/api/v1/emails/${encodeURIComponent(messageNo)}/retry`, {
      method: 'POST',
    }),

  // ---- 工单人工侧 ----
  ticketBoardStats: () => request<TicketBoardStats>('/api/v1/tickets/stats'),
  ticketStatuses: () =>
    request<{ human_statuses: HumanStatusOption[] }>('/api/v1/service/tickets/statuses'),
  assignTicket: (ticketNo: string, assignee: string | null, note?: string) =>
    request<{ ticket_no: string; assignee: string | null; changed: boolean; message: string }>(
      `/api/v1/service/tickets/${encodeURIComponent(ticketNo)}/assign`,
      { method: 'POST', body: JSON.stringify({ assignee, note: note ?? null }) },
    ),
  updateTicketProgress: (ticketNo: string, humanStatus: string, note?: string) =>
    request<{
      ticket_no: string
      human_status: string
      human_status_label: string
      assignee: string | null
      human_note: string | null
      message: string
    }>(`/api/v1/service/tickets/${encodeURIComponent(ticketNo)}/progress`, {
      method: 'POST',
      body: JSON.stringify({ human_status: humanStatus, note: note ?? null }),
    }),

  // ---- 登录安全审计 ----
  loginEvents: (p: { username?: string; only_anomalous?: boolean; limit?: number } = {}) =>
    request<{
      items: LoginEvent[]
      total: number
      stats: { returned: number; anomalous_in_page: number; failed_in_page: number }
    }>(`/api/v1/admin/security/logins${qs(p)}`),

  returnRateByCategory: (days = 30) =>
    request<ReturnRateByCategory>(`/api/v1/metrics/return-rate-by-category${qs({ days })}`),

  emails: (p: { page?: number; page_size?: number; status?: string; only_blocked?: boolean } = {}) =>
    request<Page<EmailMessage>>(`/api/v1/emails${qs(p)}`),
  emailStats: () => request<EmailGateStats>('/api/v1/emails/stats'),

  dailyMetrics: (p: { category?: string; days?: number } = {}) =>
    request<DailyMetric[]>(`/api/v1/metrics/daily${qs(p)}`),

  lowStock: (threshold = 5) => request<LowStockItem[]>(`/api/v1/inventory/low-stock${qs({ threshold })}`),

  // ---- 对话 ----
  /**
   * 我的会话列表。`total` 是**真实总数**（不是本页条数），
   * 配合 `offset` 就能翻页 —— 以前 total 恒等于返回条数，第 51 个会话永远取不到。
   */
  conversations: (p: { limit?: number; status?: string; offset?: number } = {}) =>
    request<{ items: Conversation[]; total: number }>(`/api/v1/conversations${qs(p)}`),
  createConversation: (title?: string) =>
    request<{ conversation: Conversation }>('/api/v1/conversations', {
      method: 'POST',
      body: JSON.stringify({ title: title ?? null }),
    }),
  /**
   * 会话详情 + 历史消息。
   *
   * 默认只回**最近**的一批（后端 `DEFAULT_HISTORY_LIMIT`），`before_seq`
   * 用来往回翻更早的消息 —— 一场长对话不该为了看最新几句就把整段历史
   * （含工具返回的大段 JSON）全量拉下来。
   */
  conversation: (conversationNo: string, p: { limit?: number; before_seq?: number } = {}) =>
    request<{
      conversation: Conversation
      messages: ChatMessage[]
      pending_approvals: ChatApproval[]
      /** 还有更早的消息可以翻（用最后一条的 seq 当 before_seq 再请求一次）。 */
      has_more: boolean
    }>(`/api/v1/conversations/${encodeURIComponent(conversationNo)}${qs(p)}`),

  /** 归档 / 取消归档：把会话从默认列表收起或放回，不删数据。 */
  archiveConversation: (conversationNo: string) =>
    request<{ ok: boolean; status: string }>(
      `/api/v1/conversations/${encodeURIComponent(conversationNo)}/archive`,
      { method: 'POST' },
    ),
  unarchiveConversation: (conversationNo: string) =>
    request<{ ok: boolean; status: string }>(
      `/api/v1/conversations/${encodeURIComponent(conversationNo)}/unarchive`,
      { method: 'POST' },
    ),

  /** 我记住了关于你的哪些事（长期记忆）。 */
  memories: () => request<{ items: AgentMemory[] }>('/api/v1/memories'),
  deleteMemory: (id: number) =>
    request<{ ok: boolean; id: number }>(`/api/v1/memories/${id}`, { method: 'DELETE' }),

  // ---- 账号管理 ----
  /**
   * 账号列表。**不需要传"我能管哪些部门"** —— 范围由后端按访问令牌里的
   * 身份判定（部门主管自动只见本部门）。前端传范围参数等于让它自己申报权限。
   *
   * 响应里顺带带上部门与可授予角色，避免那次请求拿到的是过期的角色列表。
   */
  adminAccounts: (
    p: { q?: string; status?: string; department_id?: number; limit?: number; offset?: number } = {},
  ) => request<AdminAccountsResponse>(`/api/v1/admin/accounts${qs(p)}`),

  adminCreateAccount: (body: {
    username: string
    password?: string | null
    full_name?: string
    email?: string | null
    phone?: string | null
    department_id?: number | null
    role_codes?: string[]
    is_superuser?: boolean
  }) =>
    request<{
      account: AdminAccount
      /** 自动生成的初始密码，**只在这一次响应里出现** */
      generated_password: string | null
      warnings: string[]
    }>('/api/v1/admin/accounts', { method: 'POST', body: JSON.stringify(body) }),

  /** 部分更新。只有传了的字段会被改 —— 传 `null` 是"清空"，不传是"不动"。 */
  adminUpdateAccount: (
    userId: number,
    body: {
      full_name?: string
      email?: string | null
      phone?: string | null
      department_id?: number | null
      status?: string
      role_codes?: string[]
    },
  ) =>
    request<{ account: AdminAccount; notes: string[] }>(`/api/v1/admin/accounts/${userId}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  /** 重置密码。不传密码就生成一个随机强密码，**只返回一次**。 */
  adminResetPassword: (userId: number, password?: string) =>
    request<{ username: string; generated_password: string | null; message: string }>(
      `/api/v1/admin/accounts/${userId}/reset-password`,
      { method: 'POST', body: JSON.stringify({ password: password ?? null }) },
    ),

  adminCreateDepartment: (body: { code: string; name: string; description?: string }) =>
    request<{ department: AdminDepartment }>('/api/v1/admin/departments', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  adminUpdateDepartment: (
    departmentId: number,
    body: { name?: string; description?: string; is_active?: boolean },
  ) =>
    request<{ department: AdminDepartment }>(`/api/v1/admin/departments/${departmentId}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  /** 发一条消息，流式接收 Agent 的回答。 */
  sendChatMessage: (
    conversationNo: string,
    content: string,
    onEvent: (event: ChatEvent) => void,
    signal?: AbortSignal,
  ) =>
    postSse<ChatEvent>(
      `/api/v1/conversations/${encodeURIComponent(conversationNo)}/messages`,
      { content },
      onEvent,
      signal,
    ),

  /** 确认/驳回对话里的写操作，并流式接收后续回答。 */
  decideChatApproval: (
    conversationNo: string,
    approvalNo: string,
    approved: boolean,
    reason: string,
    onEvent: (event: ChatEvent) => void,
    signal?: AbortSignal,
  ) =>
    postSse<ChatEvent>(
      `/api/v1/conversations/${encodeURIComponent(conversationNo)}/approvals/${encodeURIComponent(approvalNo)}`,
      { approved, reason },
      onEvent,
      signal,
    ),

  // ---- 客服处理 ----
  /** 处理一条工单（两层：代码快车道 + Agent 兜底），流式推进度。 */
  handleTicket: (ticketNo: string, onEvent: (event: TicketEvent) => void, signal?: AbortSignal) =>
    postSse<TicketEvent>(
      `/api/v1/service/tickets/${encodeURIComponent(ticketNo)}/handle`,
      {},
      onEvent,
      signal,
    ),

  /** 批量处理。`all=false` 时只碰没处理过的 —— 重跑会覆盖上一轮判断。 */
  handleTicketsBatch: (
    body: { limit: number; all: boolean },
    onEvent: (event: TicketEvent) => void,
    signal?: AbortSignal,
  ) => postSse<TicketEvent>('/api/v1/service/tickets/handle-pending', body, onEvent, signal),
}
