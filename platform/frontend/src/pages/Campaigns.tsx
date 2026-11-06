import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type Campaign } from '../api'
import { PageHeader, StatCard, StatusBadge, yuan } from '../ui'

/** 平台中文名。英文枚举直接展示会漏出实现细节（mock 字样更不能外露）。 */
const PLATFORM_LABEL: Record<string, string> = {
  juliang: '巨量引擎',
  tencent: '腾讯广告',
  mock: '演示平台',
  douyin: '抖音',
  taobao: '淘宝',
}

const OBJECTIVE_LABEL: Record<string, string> = {
  gmv: '成交额',
  roi: 'ROI 目标',
  clicks: '点击',
  uv: '访客',
  conversions: '成交额',
}

type SortKey = 'roiAsc' | 'roiDesc' | null

/**
 * 处置动作。**哪些状态能做什么由后端判** —— 前端只是不给明显按不下去的按钮，
 * 真的点下去后端还会再判一次（改了状态就 409），所以这里不用维护一份状态机。
 */
type ActionMode = 'approve' | 'reject' | 'pause' | 'budget'

const ACTION_LABEL: Record<ActionMode, string> = {
  approve: '批准投放',
  reject: '驳回',
  pause: '一键止损',
  budget: '改日预算',
}

/** 每个动作要不要填原因 / 填新预算。驳回必填，其余选填。 */
const NEEDS: Record<ActionMode, { reason: boolean; reasonRequired: boolean; budget: boolean }> = {
  approve: { reason: false, reasonRequired: false, budget: false },
  reject: { reason: true, reasonRequired: true, budget: false },
  pause: { reason: true, reasonRequired: false, budget: false },
  budget: { reason: true, reasonRequired: false, budget: true },
}

function availableActions(status: string): ActionMode[] {
  switch (status) {
    case 'pending_approval':
      return ['approve', 'reject']
    case 'running':
      return ['pause', 'budget']
    case 'paused':
    case 'draft':
    case 'rejected':
      return ['budget']
    default:
      return []
  }
}

export default function Campaigns() {
  const [items, setItems] = useState<Campaign[]>([])
  const [total, setTotal] = useState(0)
  const [losingCount, setLosingCount] = useState(0)
  const [onlyLosing, setOnlyLosing] = useState(false)
  const [sort, setSort] = useState<SortKey>(null)
  const [error, setError] = useState<string | null>(null)

  // ---- 处置面板 ----
  const [target, setTarget] = useState<Campaign | null>(null)
  const [mode, setMode] = useState<ActionMode | null>(null)
  const [reason, setReason] = useState('')
  const [budget, setBudget] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null)

  const load = useCallback(() => {
    Promise.all([
      api.campaigns({ page_size: 50, only_losing: onlyLosing }),
      api.campaigns({ page_size: 1, only_losing: true }),
    ])
      .then(([page, losing]) => {
        setItems(page.items)
        setTotal(page.total)
        setLosingCount(losing.total)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [onlyLosing])

  useEffect(() => {
    load()
  }, [load])

  const open = (campaign: Campaign, next: ActionMode) => {
    setTarget(campaign)
    setMode(next)
    setReason('')
    setBudget(String(campaign.budget_daily ?? ''))
    setResult(null)
  }

  const close = () => {
    setTarget(null)
    setMode(null)
    setResult(null)
  }

  const submit = async () => {
    if (!target || !mode) return
    const need = NEEDS[mode]
    if (need.reasonRequired && !reason.trim()) {
      setResult({ ok: false, text: '驳回必须写原因 —— 申请人要靠它知道该怎么改' })
      return
    }
    const value = Number(budget)
    if (need.budget && (!Number.isFinite(value) || value <= 0)) {
      setResult({ ok: false, text: '日预算必须是一个大于 0 的数字' })
      return
    }

    setBusy(true)
    setResult(null)
    try {
      const note = reason.trim() || undefined
      let res
      if (mode === 'approve') res = await api.approveCampaign(target.campaign_no, note)
      else if (mode === 'reject') res = await api.rejectCampaign(target.campaign_no, reason.trim())
      else if (mode === 'pause') res = await api.pauseCampaign(target.campaign_no, note)
      else res = await api.changeBudget(target.campaign_no, value, note)

      setResult({ ok: true, text: res.message })
      // 成功后刷新：状态/预算是后端落的，前端不自己改这一行 ——
      // 本地改会和"别人也动过这条"冲突，表现为刷新后又变回去。
      load()
    } catch (e) {
      setResult({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const sorted = useMemo(() => {
    if (!sort) return items
    return [...items].sort((a, b) => {
      const ra = a.roi === null ? -1 : Number(a.roi)
      const rb = b.roi === null ? -1 : Number(b.roi)
      return sort === 'roiAsc' ? ra - rb : rb - ra
    })
  }, [items, sort])

  const runningCount = items.filter((c) => c.status === 'running').length
  const pendingCount = items.filter((c) => c.status === 'pending_approval').length
  const runningLosing = items.filter(
    (c) => c.status === 'running' && c.roi !== null && Number(c.roi) < 1,
  ).length

  const need = mode ? NEEDS[mode] : null

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '广告投放']}
        title="广告投放"
        badge={<span className="badge danger">{losingCount} 个亏损</span>}
        desc={
          <>
            共 {total} 个计划，其中 <span style={{ color: 'var(--danger)' }}>{losingCount} 个 ROI 低于 1.0（亏损）</span>。
            广告投放 Agent 的自动停投规则就是抓这个信号；改预算属于高危操作，必须走人工确认闸口。
          </>
        }
      />

      {error && <div className="error-box">{error}</div>}

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <StatCard label="计划总数" value={total} icon="campaign" />
        <StatCard label="运行中" value={runningCount} icon="trend-up" />
        <StatCard
          label="运行中亏损"
          value={runningLosing}
          sub="与列表口径一致"
          tone={runningLosing > 0 ? 'danger' : 'ok'}
          icon="alert"
        />
        <StatCard label="待审批" value={pendingCount} tone="warn" icon="clock" />
      </div>

      <div className="toolbar">
        <label style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <input type="checkbox" checked={onlyLosing} onChange={(e) => setOnlyLosing(e.target.checked)} />
          只看亏损计划
        </label>

        <button
          type="button"
          className={`filter-chip${sort === 'roiAsc' ? ' active' : ''}`}
          onClick={() => setSort(sort === 'roiAsc' ? null : 'roiAsc')}
        >
          ROI 从低到高
        </button>
        <button
          type="button"
          className={`filter-chip${sort === 'roiDesc' ? ' active' : ''}`}
          onClick={() => setSort(sort === 'roiDesc' ? null : 'roiDesc')}
        >
          ROI 从高到低
        </button>
      </div>

      {mode && target && need && (
        <div className="card" style={{ marginBottom: 14, borderColor: 'var(--accent)' }}>
          <h3>
            {ACTION_LABEL[mode]}：{target.name}
            <span className="mono muted" style={{ marginLeft: 8, fontSize: 12 }}>
              {target.campaign_no}
            </span>
          </h3>
          <p className="muted" style={{ margin: '4px 0 10px' }}>
            {mode === 'pause' && '止损会立刻暂停投放。亏损按分钟累计，所以它不需要再走一轮审批。'}
            {mode === 'approve' && '批准后计划立即开始投放，预算按当前设置消耗。'}
            {mode === 'reject' && '驳回后计划停在 rejected，申请人会看到你写的理由。'}
            {mode === 'budget' && '你在界面上的这次点击本身就是审批 —— 改完立即生效并留痕。'}
          </p>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            {need.budget && (
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span className="muted">新日预算（元）</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={budget}
                  onChange={(e) => setBudget(e.target.value)}
                  style={{ width: 160 }}
                />
              </label>
            )}
            {need.reason && (
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4, flex: 1, minWidth: 240 }}>
                <span className="muted">
                  {need.reasonRequired ? '原因（必填）' : '备注（可选）'}
                </span>
                <input
                  className="input"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder={mode === 'reject' ? '例如：ROI 已经低于 1.0，先降预算观察' : '选填'}
                />
              </label>
            )}
            <button type="button" className="button primary" disabled={busy} onClick={() => void submit()}>
              {busy ? '提交中…' : `确认${ACTION_LABEL[mode]}`}
            </button>
            <button type="button" className="button" onClick={close} disabled={busy}>
              取消
            </button>
          </div>

          {result && (
            <div className={result.ok ? 'notice ok' : 'error-box'} style={{ marginTop: 10 }}>
              {result.text}
            </div>
          )}
        </div>
      )}

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>计划号</th>
                <th>名称</th>
                <th>平台</th>
                <th>目标</th>
                <th style={{ width: 180 }}>日预算消耗</th>
                <th>状态</th>
                <th>ROI</th>
                <th style={{ width: 200 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((row) => {
                const roi = row.roi === null ? null : Number(row.roi)
                const losing = roi !== null && roi < 1
                const budgetNum = Number(row.budget_daily)
                const spend = Number(row.spend)
                const ratio = budgetNum > 0 ? spend / budgetNum : 0
                const over80 = ratio > 0.8
                const actions = availableActions(row.status)
                return (
                  <tr key={row.campaign_no} style={losing ? { background: 'rgba(239,95,107,0.06)' } : undefined}>
                    <td className="mono">{row.campaign_no}</td>
                    <td>{row.name}</td>
                    <td>{PLATFORM_LABEL[row.platform] ?? row.platform}</td>
                    <td>{OBJECTIVE_LABEL[row.objective] ?? row.objective}</td>
                    <td>
                      <div className="budget-bar">
                        <div className="budget-track">
                          <span
                            className={over80 ? 'danger' : ''}
                            style={{ width: `${Math.min(100, ratio * 100)}%` }}
                          />
                        </div>
                        <div className="budget-text">
                          {yuan(spend)} / {yuan(budgetNum)}
                          <span className={over80 ? 'danger-text' : 'muted'}>
                            {Math.round(ratio * 100)}%
                          </span>
                        </div>
                      </div>
                    </td>
                    <td>
                      <StatusBadge status={row.status} />
                    </td>
                    <td>
                      {roi === null ? (
                        '—'
                      ) : (
                        <span className={`badge ${losing ? 'danger' : roi > 3 ? 'ok' : 'warn'}`}>
                          {roi.toFixed(2)}
                          {losing ? ' 亏损' : ''}
                        </span>
                      )}
                    </td>
                    <td>
                      {actions.length === 0 ? (
                        <span className="muted">—</span>
                      ) : (
                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                          {actions.map((action) => (
                            <button
                              key={action}
                              type="button"
                              className={`button small${action === 'pause' ? ' danger' : ''}`}
                              onClick={() => open(row, action)}
                            >
                              {ACTION_LABEL[action]}
                            </button>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                )
              })}
              {sorted.length === 0 && (
                <tr>
                  <td colSpan={8} className="empty">
                    没有投放计划
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
