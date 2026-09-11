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

export interface DagNode {
  id: string
  name: string
  agent: string
  interrupt?: boolean
}

export interface Dag {
  nodes: DagNode[]
  edges: [string, string][]
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
  task: (taskNo: string) => request<Task>(`/api/v1/tasks/${taskNo}`),
  taskEvents: (taskNo: string, sinceSeq = 0) =>
    request<TaskEvent[]>(`/api/v1/tasks/${taskNo}/events${qs({ since_seq: sinceSeq })}`),

  tickets: (p: { page?: number; page_size?: number; only_escalated?: boolean } = {}) =>
    request<Page<Ticket>>(`/api/v1/tickets${qs(p)}`),

  campaigns: (p: { page?: number; page_size?: number; only_losing?: boolean } = {}) =>
    request<Page<Campaign>>(`/api/v1/campaigns${qs(p)}`),

  dailyMetrics: (p: { category?: string; days?: number } = {}) =>
    request<DailyMetric[]>(`/api/v1/metrics/daily${qs(p)}`),

  lowStock: (threshold = 5) => request<LowStockItem[]>(`/api/v1/inventory/low-stock${qs({ threshold })}`),
}
