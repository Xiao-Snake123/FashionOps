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

export function StatCard({
  label,
  value,
  sub,
  tone = 'normal',
}: {
  label: string
  value: ReactNode
  sub?: string
  tone?: 'normal' | 'warn' | 'danger'
}) {
  const color =
    tone === 'danger' ? 'var(--danger)' : tone === 'warn' ? 'var(--warn)' : 'var(--text)'
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className="stat-value small" style={{ color }}>
        {value}
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
