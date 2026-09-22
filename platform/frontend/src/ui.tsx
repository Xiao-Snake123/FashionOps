/** 几个跨页面复用的小组件与格式化函数。 */

import type { ReactNode } from 'react'

export function yuan(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return '—'
  return `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`
}

export function pct(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return '—'
  // 后端存的是 0~1 的小数，展示时乘 100
  return `${(n * 100).toFixed(digits)}%`
}

export function num(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return Number(value).toLocaleString('zh-CN')
}

// ---------------------------------------------------------------------------
// 图标
// ---------------------------------------------------------------------------
/*
 * 一套线性图标（24×24，stroke=currentColor）。
 *
 * 为什么手写 SVG 而不是引 UI 库：整个前端只有十来处要图标，
 * 引一个图标库会带进来几百 KB 和一个额外的构建配置；而这里每个图标
 * 就是一条 path，复制成本比引入依赖低。
 */
export type IconName =
  | 'chat'
  | 'dashboard'
  | 'tasks'
  | 'ticket'
  | 'campaign'
  | 'mail'
  | 'product'
  | 'memory'
  | 'link'
  | 'book'
  | 'logout'
  | 'refresh'
  | 'close'
  | 'plus'
  | 'send'
  | 'sparkle'
  | 'trend-up'
  | 'trend-down'
  | 'alert'
  | 'check'
  | 'clock'
  | 'archive'
  | 'tag'
  | 'users'

const ICON_PATHS: Record<IconName, ReactNode> = {
  chat: <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.9 9.9 0 0 1-3.8-.7L3 21l1.9-4.6A8.2 8.2 0 0 1 4 11.5 8.4 8.4 0 0 1 12.5 3 8.4 8.4 0 0 1 21 11.5Z" />,
  dashboard: (
    <>
      <path d="M4 20V10M10 20V4M16 20v-7M4 20h16" />
    </>
  ),
  tasks: (
    <>
      <rect x="3" y="4" width="6.5" height="16" rx="1.6" />
      <rect x="12.5" y="4" width="8.5" height="7" rx="1.6" />
      <rect x="12.5" y="13.5" width="8.5" height="6.5" rx="1.6" />
    </>
  ),
  ticket: (
    <>
      <path d="M3 9a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v1.5a2 2 0 0 0 0 3.8V16a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-1.7a2 2 0 0 0 0-3.8Z" />
      <path d="M12 8v8" strokeDasharray="2 2" />
    </>
  ),
  campaign: (
    <>
      <path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1Z" />
      <path d="M15 8.5a5 5 0 0 1 0 7M18 6a8.5 8.5 0 0 1 0 12" />
    </>
  ),
  mail: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="m3.5 7 8.5 6 8.5-6" />
    </>
  ),
  product: (
    <>
      <path d="M12 3 4 7v10l8 4 8-4V7Z" />
      <path d="m4 7 8 4 8-4M12 11v10" />
    </>
  ),
  memory: (
    <>
      <path d="M12 4v3M12 17v3M4.9 7.5l2.4 2.4M16.7 14.1l2.4 2.4M4 12h3M17 12h3M4.9 16.5l2.4-2.4M16.7 9.9l2.4-2.4" />
      <circle cx="12" cy="12" r="3.4" />
    </>
  ),
  link: (
    <>
      <path d="M9.5 14.5 14.5 9.5" />
      <path d="M11 6.5 12.8 4.7a3.8 3.8 0 0 1 5.4 5.4L16.4 12M7.6 12l-1.8 1.8a3.8 3.8 0 0 0 5.4 5.4L13 17.4" />
    </>
  ),
  book: (
    <>
      <path d="M5 4h9a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3Z" />
      <path d="M17 7h2v13H8" />
    </>
  ),
  logout: (
    <>
      <path d="M15 12H5M8 8l-3 4 3 4" />
      <path d="M12 5h5a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-5" />
    </>
  ),
  refresh: <path d="M20 11a8 8 0 0 0-13.7-4.7L4 8.6M4 13a8 8 0 0 0 13.7 4.7L20 15.4M4 8.6V4M20 15.4V20" />,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  plus: <path d="M12 5v14M5 12h14" />,
  send: <path d="M4.5 12 20 4.5 14 20l-3.2-6.2L4.5 12Z" />,
  sparkle: (
    <>
      <path d="M12 4.5 13.6 9l4.5 1.6L13.6 12l-1.6 4.5L10.4 12 6 10.6l4.4-1.6Z" />
      <path d="M18.5 4v3M20 5.5h-3" />
    </>
  ),
  'trend-up': <path d="M4 16l5-5 3.5 3.5L20 7M20 7h-4.5M20 7v4.5" />,
  'trend-down': <path d="M4 8l5 5 3.5-3.5L20 17M20 17h-4.5M20 17v-4.5" />,
  alert: (
    <>
      <path d="M12 4 3 19h18Z" />
      <path d="M12 10v4M12 16.6v.4" />
    </>
  ),
  check: <path d="M5 12.5 10 17.5 19.5 7" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 8v4.2l2.8 1.8" />
    </>
  ),
  archive: (
    <>
      <rect x="4" y="4" width="16" height="4" rx="1.4" />
      <path d="M5.5 8v10a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8M10 12.5h4" />
    </>
  ),
  tag: (
    <>
      <path d="M11.6 4H5a1 1 0 0 0-1 1v6.6a2 2 0 0 0 .6 1.4l6.4 6.4a2 2 0 0 0 2.8 0l5.2-5.2a2 2 0 0 0 0-2.8L12.9 4.6a2 2 0 0 0-1.3-.6Z" />
      <circle cx="8.2" cy="8.2" r="1.2" />
    </>
  ),
  users: (
    <>
      <path d="M15.5 20v-1.6a4 4 0 0 0-4-4h-5a4 4 0 0 0-4 4V20" />
      <circle cx="9" cy="7.6" r="3.3" />
      <path d="M17 11.3a3.3 3.3 0 0 0 0-6.4M21.5 20v-1.6a4 4 0 0 0-3-3.87" />
    </>
  ),
}

export function Icon({
  name,
  size = 18,
  className,
  strokeWidth = 1.8,
}: {
  name: IconName
  size?: number
  className?: string
  strokeWidth?: number
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICON_PATHS[name]}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// 页面头部
// ---------------------------------------------------------------------------
/** 面包屑 + 标题 + 描述 + 右侧操作。所有列表页统一用它开头。 */
export function PageHeader({
  crumbs,
  title,
  badge,
  desc,
  actions,
}: {
  crumbs?: ReactNode[]
  title: ReactNode
  badge?: ReactNode
  desc?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="page-header">
      <div style={{ minWidth: 0 }}>
        {crumbs && crumbs.length > 0 && (
          <div className="page-crumb">
            {crumbs.map((c, i) => (
              <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                {i > 0 && <span className="sep">/</span>}
                <span className={i === crumbs.length - 1 ? 'current' : undefined}>{c}</span>
              </span>
            ))}
          </div>
        )}
        <h2 className="page-title">
          {title}
          {badge}
        </h2>
        {desc && <p className="page-desc">{desc}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 统计 / KPI 卡片
// ---------------------------------------------------------------------------
export function StatCard({
  label,
  value,
  sub,
  tone = 'normal',
  icon,
  delta,
}: {
  label: string
  value: ReactNode
  sub?: string
  tone?: 'normal' | 'ok' | 'warn' | 'danger'
  icon?: IconName
  /** 环比变化。up=上升，down=下降（颜色按业务语义给，不always红/绿） */
  delta?: { text: string; dir: 'up' | 'down' }
}) {
  const toneClass = tone === 'normal' ? '' : tone
  return (
    <div className={`card stat-card ${toneClass}`}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div className="stat-label">{label}</div>
          <div className="stat-value small">
            {value}
            {delta && <span className={`delta ${delta.dir}`}>{delta.text}</span>}
          </div>
        </div>
        {icon && (
          <div className="stat-icon">
            <Icon name={icon} size={17} />
          </div>
        )}
      </div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

const STATUS_TONE: Record<string, string> = {
  succeeded: 'ok',
  running: 'warn',
  waiting_approval: 'warn',
  failed: 'danger',
  pending: 'dim',
  cancelled: 'dim',
  completed: 'ok',
  paid: 'warn',
  shipped: 'ok',
  refunded: 'danger',
  refunding: 'danger',
  closed: 'ok',
  processing: 'warn',
  open: 'warn',
  waiting: 'warn',
  transferred: 'danger',
  ended: 'dim',
  paused: 'warn',
  draft: 'dim',
  rejected: 'danger',
  on_sale: 'ok',
  ready: 'warn',
  off_shelf: 'dim',
  approved: 'ok',
  sent: 'ok',
  blocked: 'danger',
}

const TONE_LABEL: Record<string, string> = {
  succeeded: '成功',
  running: '运行中',
  waiting_approval: '等待确认',
  failed: '失败',
  pending: '待执行',
  cancelled: '已取消',
  completed: '已完成',
  paid: '已付款',
  shipped: '已发货',
  refunded: '已退款',
  refunding: '退款中',
  closed: '已关闭',
  processing: '处理中',
  open: '待处理',
  waiting: '等待中',
  transferred: '已转人工',
  ended: '已结束',
  paused: '已暂停',
  draft: '草稿',
  rejected: '已驳回',
  on_sale: '在售',
  ready: '待上架',
  off_shelf: '已下架',
  approved: '已通过',
  sent: '已发送',
  blocked: '被拦截',
}

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const tone = STATUS_TONE[status] ?? 'dim'
  return <span className={`badge ${tone}`}>{label ?? TONE_LABEL[status] ?? status}</span>
}

export function ProgressBar({ value, status }: { value: number; status: string }) {
  const cls = status === 'succeeded' ? 'done' : status === 'failed' ? 'failed' : ''
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div className={`progress ${cls}`}>
        <span style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
      </div>
      <span className="muted" style={{ fontSize: 12 }}>
        {value}%
      </span>
    </div>
  )
}
