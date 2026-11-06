/**
 * 账号管理 —— 分级授权。
 *
 * ## 这个页面对不同的人看到的东西不一样
 *
 * | 你是谁 | 看到什么 |
 * | --- | --- |
 * | 主管理员 | 全部账号，能重置任何人的密码 |
 * | 管理员 | 全部账号，但**超级管理员那几行是只读的** |
 * | 部门主管 | 只有本部门的账号 |
 *
 * **范围不是前端算的，是后端按访问令牌判的。** 页面只是把后端返回的
 * `can_manage` / `manage_blocked_reason` 显示出来 ——
 * 前端算范围等于让前端自己申报权限，而前端跑在用户机器上。
 *
 * ## 密码为什么只能重置、不能查看
 *
 * 库里存的是 argon2id 哈希，**数学上不可逆**，不是"不给看"。
 * 所以"主管理员掌管所有账号密码"在这个系统里的实现是"能重置任何人的密码"，
 * 而不是"能看到密码"。界面上也不做任何暗示可以查看的设计。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  api,
  ApiError,
  type AdminAccount,
  type AdminAccountsResponse,
  type AdminRole,
  type LoginEvent,
} from '../api'
import { Icon, PageHeader, StatCard } from '../ui'

/** 账号状态 → 中文标签 + 徽标配色。 */
const STATUS_META: Record<string, { label: string; tone: string }> = {
  active: { label: '启用中', tone: 'ok' },
  disabled: { label: '已停用', tone: 'dim' },
  locked: { label: '已锁定', tone: 'danger' },
  pending: { label: '待激活', tone: 'warn' },
}

/** 每页条数。和后端默认值一致，改一处要改两处 —— 所以写在显眼的地方。 */
const PAGE_SIZE = 50

/** 账号资料完整度：缺邮箱/手机/部门/姓名/角色都会扣分，缺什么一目了然。 */
function completeness(a: AdminAccount): { score: number; missing: string[] } {
  const checks: Array<[boolean, string]> = [
    [Boolean(a.email), '邮箱'],
    [Boolean(a.phone), '手机'],
    [a.department_id != null, '部门'],
    [Boolean(a.full_name), '姓名'],
    [a.roles.length > 0, '角色'],
  ]
  const missing = checks.filter(([ok]) => !ok).map(([, label]) => label)
  const score = Math.round(((checks.length - missing.length) / checks.length) * 100)
  return { score, missing }
}

export default function Accounts() {
  const [data, setData] = useState<AdminAccountsResponse | null>(null)
  const [items, setItems] = useState<AdminAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 搜索框单独一个 state：它每次都变，而请求只应该在停手之后发一次
  const [searchInput, setSearchInput] = useState('')
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [deptFilter, setDeptFilter] = useState('')

  const [editing, setEditing] = useState<AdminAccount | 'new' | null>(null)
  const [resetTarget, setResetTarget] = useState<AdminAccount | null>(null)

  // 打字防抖：不停手就每敲一个字发一次请求，既吵又会因为响应乱序而闪
  useEffect(() => {
    const timer = setTimeout(() => setQuery(searchInput.trim()), 350)
    return () => clearTimeout(timer)
  }, [searchInput])

  /**
   * 拉账号列表。
   *
   * `offset > 0` 表示"再翻一页"，结果**追加**；否则是重新拉首页，结果**替换**。
   * 依赖里带上筛选条件，所以改筛选会自动从第 0 页重来。
   */
  const load = useCallback(
    async (offset: number) => {
      setLoading(true)
      setError(null)
      try {
        const res = await api.adminAccounts({
          limit: PAGE_SIZE,
          offset,
          q: query || undefined,
          status: statusFilter || undefined,
          department_id: deptFilter ? Number(deptFilter) : undefined,
        })
        setData(res)
        setItems((prev) => (offset > 0 ? [...prev, ...res.items] : res.items))
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        setLoading(false)
      }
    },
    [query, statusFilter, deptFilter],
  )

  useEffect(() => {
    void load(0)
  }, [load])

  const scope = data?.scope
  const stats = data?.stats

  /** 部门名。取不到就退回 "未分配" —— 不显示裸的 id 数字（那对用户不是信息）。 */
  const deptName = useCallback(
    (id: number | null) => {
      if (id === null) return null
      return data?.departments.find((d) => d.department_id === id)?.name ?? `部门 #${id}`
    },
    [data],
  )

  /** 角色码 → 角色名。取不到就显示码本身，至少能看出是哪个角色。 */
  const roleName = useCallback(
    (code: string) => data?.assignable_roles.find((r) => r.code === code)?.name ?? code,
    [data],
  )

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '账号管理']}
        title="账号管理"
        badge={
          scope && (
            <span className={`tag${scope.global ? ' accent' : ''}`}>
              可管理范围：{scope.label}
            </span>
          )
        }
        desc="主管理员掌管所有账号并可重置任何人的密码；管理员跨部门管理账号；部门主管只管本部门的账号。密码以不可逆哈希存储，任何角色都无法查看，只能重置。"
        actions={
          <button
            type="button"
            className="btn primary"
            disabled={!scope?.can_create}
            title={scope?.can_create ? undefined : '你没有可管理的部门，无法新建账号'}
            onClick={() => setEditing('new')}
          >
            <Icon name="plus" size={15} />
            新建账号
          </button>
        }
      />

      {error && <div className="error-box">{error}</div>}

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <StatCard
          label="账号总数"
          value={stats?.total ?? '—'}
          sub={scope?.global ? '全部部门' : '本部门'}
          icon="users"
        />
        <StatCard
          label="启用中"
          value={stats?.active ?? '—'}
          sub="可以正常登录"
          tone="ok"
          icon="check"
        />
        <StatCard
          label="已停用"
          value={stats?.disabled ?? '—'}
          sub="登不进来，但历史记录保留"
          icon="archive"
        />
        <StatCard
          label="已锁定 / 待激活"
          value={`${stats?.locked ?? 0} / ${stats?.pending ?? 0}`}
          sub="连续登录失败被临时锁定"
          tone="warn"
          icon="alert"
        />
      </div>

      <details className="policy-note">
        <summary>账号安全策略（成文）</summary>
        <ul>
          <li>
            <b>分级授权</b>：主管理员 &gt; 管理员 &gt; 部门主管。可管理范围由后端按令牌判定，
            前端只负责展示 <span className="mono">can_manage</span>，不自己算权限。
          </li>
          <li>
            <b>密码</b>：以 argon2id 不可逆哈希存储，任何角色只能重置、不能查看；重置会同时撤销该账号全部登录会话。
          </li>
          <li>
            <b>账号生命周期</b>：停用保留历史记录；锁定是连续登录失败触发的临时状态，待激活是新账号未设密码。
          </li>
        </ul>
      </details>

      <div className="toolbar">
        <input
          type="search"
          placeholder="搜索用户名 / 姓名 / 邮箱"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          style={{ minWidth: 240 }}
        />
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">全部状态</option>
          {Object.entries(STATUS_META).map(([value, meta]) => (
            <option key={value} value={value}>
              {meta.label}
            </option>
          ))}
        </select>
        {/* 部门筛选只对跨部门的人有意义：部门主管的列表本来就只有一个部门 */}
        {scope?.global && (
          <select value={deptFilter} onChange={(e) => setDeptFilter(e.target.value)}>
            <option value="">全部部门</option>
            {data?.departments.map((d) => (
              <option key={d.department_id} value={String(d.department_id)}>
                {d.name}
                {d.is_active ? '' : '（已停用）'}
              </option>
            ))}
          </select>
        )}
        <button type="button" className="btn" onClick={() => void load(0)} disabled={loading}>
          <Icon name="refresh" size={15} />
          刷新
        </button>
        <span className="muted" style={{ fontSize: 12.5 }}>
          共 {data?.total ?? 0} 个账号
        </span>
      </div>

      <div className="card">
        {loading && items.length === 0 ? (
          <div className="empty">加载中…</div>
        ) : items.length === 0 ? (
          <div className="empty">
            {query || statusFilter || deptFilter ? '没有符合条件的账号' : '还没有任何账号'}
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>账号</th>
                  <th>姓名</th>
                  <th>部门</th>
                  <th>角色</th>
                  <th>状态</th>
                  <th>最近登录</th>
                  <th>完整度</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((a) => {
                  const meta = STATUS_META[a.status] ?? { label: a.status, tone: 'dim' }
                  const dept = deptName(a.department_id)
                  return (
                    <tr key={a.user_id} className={a.status === 'disabled' ? 'row-disabled' : undefined}>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span className="mono">{a.username}</span>
                          {a.is_superuser && <span className="badge danger">主管理员</span>}
                          {a.is_self && <span className="badge accent">我</span>}
                        </div>
                        {a.email && (
                          <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>
                            {a.email}
                          </div>
                        )}
                      </td>
                      <td>{a.full_name || <span className="muted">—</span>}</td>
                      <td>
                        {dept ?? <span className="muted">未分配</span>}
                      </td>
                      <td>
                        {a.roles.length === 0 ? (
                          <span className="badge warn">无角色</span>
                        ) : (
                          <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                            {a.roles.map((code) => (
                              <span className="tag" key={code}>
                                {roleName(code)}
                              </span>
                            ))}
                          </div>
                        )}
                      </td>
                      <td>
                        <span className={`badge ${meta.tone}`}>{meta.label}</span>
                      </td>
                      <td>
                        <span className="muted" style={{ fontSize: 12 }}>
                          {a.last_login_at?.slice(0, 16).replace('T', ' ') ?? '从未登录'}
                        </span>
                      </td>
                      <td>
                        {(() => {
                          const c = completeness(a)
                          return c.missing.length === 0 ? (
                            <span className="badge ok">完整</span>
                          ) : (
                            <span
                              className="badge warn"
                              title={`待补全：${c.missing.join('、')}`}
                              style={{ cursor: 'help' }}
                            >
                              {c.score}%
                            </span>
                          )
                        })()}
                      </td>
                      <td>
                        {a.can_manage ? (
                          <div style={{ display: 'flex', gap: 6 }}>
                            <button
                              type="button"
                              className="btn small"
                              onClick={() => setEditing(a)}
                            >
                              编辑
                            </button>
                            <button
                              type="button"
                              className="btn small"
                              onClick={() => setResetTarget(a)}
                            >
                              重置密码
                            </button>
                            <button
                              type="button"
                              className={`btn small${a.status === 'active' ? ' danger' : ''}`}
                              onClick={() => void toggleStatus(a)}
                            >
                              {a.status === 'active' ? '停用' : '启用'}
                            </button>
                          </div>
                        ) : (
                          /* 灰掉而不是隐藏：隐藏会让"为什么这个人没有按钮"变成疑问，
                             而原因（权限高于我 / 是我自己）正是用户需要知道的信息 */
                          <span className="muted" style={{ fontSize: 11.5 }}>
                            {a.manage_blocked_reason ?? '不可管理'}
                          </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {data && items.length < data.total && (
          <div style={{ marginTop: 14, textAlign: 'center' }}>
            <button
              type="button"
              className="btn"
              disabled={loading}
              onClick={() => void load(items.length)}
            >
              {loading ? '加载中…' : `加载更多（还有 ${data.total - items.length} 个）`}
            </button>
          </div>
        )}
      </div>

      {editing && (
        <AccountDrawer
          account={editing === 'new' ? null : editing}
          departments={data?.departments ?? []}
          roles={data?.assignable_roles ?? []}
          globalScope={Boolean(scope?.global)}
          scopeDepartmentId={scope?.department_id ?? null}
          canCreateSuperuser={Boolean(scope?.is_superuser)}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            void load(0)
          }}
        />
      )}

      {resetTarget && (
        <ResetPasswordDrawer
          account={resetTarget}
          onClose={() => setResetTarget(null)}
          onDone={() => {
            setResetTarget(null)
            void load(0)
          }}
        />
      )}

      <LoginSecurity />
    </>
  )

  /** 启用 / 停用。走的是同一个 PATCH，只是 status 不同。 */
  async function toggleStatus(account: AdminAccount) {
    const next = account.status === 'active' ? 'disabled' : 'active'
    setError(null)
    try {
      await api.adminUpdateAccount(account.user_id, { status: next })
      await load(0)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }
}

// ---------------------------------------------------------------------------
/**
 * 登录安全审计。
 *
 * **没有 `admin:audit` 权限就整块不渲染** —— 靠后端 403 决定，不在前端猜。
 * 前端那份权限快照可能过期，猜错的结果是给出一块点了必然报错的界面。
 */
function LoginSecurity() {
  const [events, setEvents] = useState<LoginEvent[]>([])
  const [total, setTotal] = useState(0)
  const [onlyAnomalous, setOnlyAnomalous] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    api
      .loginEvents({ limit: 30, only_anomalous: onlyAnomalous })
      .then((r) => {
        setEvents(r.items)
        setTotal(r.total)
        setError(null)
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 403) setForbidden(true)
        else setError(e instanceof Error ? e.message : String(e))
      })
  }, [onlyAnomalous])

  useEffect(() => {
    load()
  }, [load])

  if (forbidden) return null

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3>
        登录安全
        <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
          成功失败都记。共 {total} 条，显示最近 {events.length} 条
        </span>
      </h3>
      <p className="muted" style={{ margin: '4px 0 10px', fontSize: 12.5 }}>
        异常判定不做 IP 归属地：真正有用的信号只是「IP 变了没有」。
        接一个会过期的离线库换来的只是内网 IP 全判不出来的一批噪音。
      </p>

      <div className="toolbar" style={{ marginBottom: 10 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <input
            type="checkbox"
            checked={onlyAnomalous}
            onChange={(e) => setOnlyAnomalous(e.target.checked)}
          />
          只看异常
        </label>
        <button type="button" className="button" onClick={load}>
          刷新
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>账号</th>
              <th>结果</th>
              <th>IP</th>
              <th>方式</th>
              <th>异常</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id} style={e.anomalies.length > 0 ? { background: 'rgba(239,95,107,0.06)' } : undefined}>
                <td className="muted" style={{ fontSize: 12 }}>
                  {e.created_at ? new Date(e.created_at).toLocaleString('zh-CN') : '—'}
                </td>
                <td className="mono">{e.username}</td>
                <td>
                  <span className={`badge ${e.success ? 'ok' : 'danger'}`}>
                    {e.success ? '成功' : e.reason ?? '失败'}
                  </span>
                </td>
                <td className="mono" style={{ fontSize: 12 }}>
                  {e.ip ?? '—'}
                </td>
                <td className="muted">{e.auth_method}</td>
                <td>
                  {e.anomalies.length === 0 ? (
                    <span className="muted">—</span>
                  ) : (
                    <span className="badge danger" title={e.anomaly_note}>
                      {e.anomaly_note || e.anomalies.join('/')}
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {events.length === 0 && (
              <tr>
                <td colSpan={6} className="empty">
                  还没有登录记录
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
/** 新建 / 编辑账号的抽屉。 */
function AccountDrawer({
  account,
  departments,
  roles,
  globalScope,
  scopeDepartmentId,
  canCreateSuperuser,
  onClose,
  onSaved,
}: {
  account: AdminAccount | null
  departments: AdminAccountsResponse['departments']
  roles: AdminRole[]
  globalScope: boolean
  scopeDepartmentId: number | null
  canCreateSuperuser: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const isCreate = account === null
  const [username, setUsername] = useState('')
  const [fullName, setFullName] = useState(account?.full_name ?? '')
  const [email, setEmail] = useState(account?.email ?? '')
  const [phone, setPhone] = useState(account?.phone ?? '')
  const [password, setPassword] = useState('')
  const [status, setStatus] = useState(account?.status ?? 'active')
  const [selectedRoles, setSelectedRoles] = useState<string[]>(account?.roles ?? [])
  const [isSuperuser, setIsSuperuser] = useState(false)
  const [departmentId, setDepartmentId] = useState<string>(
    account?.department_id != null
      ? String(account.department_id)
      : globalScope
        ? ''
        : String(scopeDepartmentId ?? ''),
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [generated, setGenerated] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      if (isCreate) {
        const res = await api.adminCreateAccount({
          username: username.trim(),
          password: password.trim() || null,
          full_name: fullName.trim(),
          email: email.trim() || null,
          phone: phone.trim() || null,
          department_id: departmentId ? Number(departmentId) : null,
          role_codes: selectedRoles,
          is_superuser: isSuperuser,
        })
        setWarnings(res.warnings)
        // 自动生成的初始密码**只出现这一次**，所以不能建完就关抽屉 ——
        // 关掉等于把密码弄丢了，只能再重置一遍
        if (res.generated_password) {
          setGenerated(res.generated_password)
          return
        }
        onSaved()
        return
      }

      await api.adminUpdateAccount(account.user_id, {
        full_name: fullName.trim(),
        email: email.trim() || null,
        phone: phone.trim() || null,
        department_id: departmentId ? Number(departmentId) : null,
        status,
        role_codes: selectedRoles,
      })
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="drawer-mask" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3>
            <Icon name="users" size={16} />
            {isCreate ? '新建账号' : `编辑账号 · ${account.username}`}
          </h3>
          <button type="button" className="btn small" onClick={onClose}>
            <Icon name="close" size={14} />
          </button>
        </div>

        <div className="drawer-body">
          {error && <div className="error-box">{error}</div>}

          {generated && (
            <div className="secret-box">
              <div className="secret-label">初始密码 —— 只会显示这一次，请立刻复制发给本人</div>
              <div className="secret-value">{generated}</div>
              <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
                关闭后无法再查看。忘记只能重新重置一次。
                账号已创建完成，无需再点保存。
              </div>
            </div>
          )}

          {warnings.map((w) => (
            <div className="result-note" key={w}>
              {w}
            </div>
          ))}

          <label className="field">
            <span>用户名（登录用，创建后不可修改）</span>
            <input
              value={isCreate ? username : account.username}
              disabled={!isCreate}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="例如 zhangsan"
            />
          </label>

          <div className="field-row">
            <label className="field">
              <span>姓名</span>
              <input value={fullName} onChange={(e) => setFullName(e.target.value)} />
            </label>
            <label className="field">
              <span>手机</span>
              <input value={phone} onChange={(e) => setPhone(e.target.value)} />
            </label>
          </div>

          <label className="field">
            <span>邮箱</span>
            <input value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>

          <label className="field">
            <span>部门</span>
            {globalScope ? (
              <select value={departmentId} onChange={(e) => setDepartmentId(e.target.value)}>
                <option value="">不归属任何部门</option>
                {departments.map((d) => (
                  <option key={d.department_id} value={String(d.department_id)}>
                    {d.name}
                    {d.is_active ? '' : '（已停用）'}
                  </option>
                ))}
              </select>
            ) : (
              /* 部门主管不能选部门，连下拉都不给 —— 给了也会被后端拒掉，
                 那不如直接显示"会落到哪个部门" */
              <input
                value={departments[0]?.name ?? '（本部门）'}
                disabled
                title="部门主管新建的账号一律落在本部门"
              />
            )}
          </label>

          {isCreate ? (
            <label className="field">
              <span>初始密码（留空则自动生成一个强密码）</span>
              <input
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="留空即自动生成"
              />
            </label>
          ) : (
            <label className="field">
              <span>状态</span>
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="active">启用中</option>
                <option value="disabled">已停用</option>
              </select>
            </label>
          )}

          <div className="field">
            <span>角色（决定这个人登录后能做什么）</span>
            <div className="checkbox-list">
              {roles.length === 0 && (
                <div className="muted" style={{ fontSize: 12.5 }}>
                  没有你可以授予的角色
                </div>
              )}
              {roles.map((r) => (
                <label className="checkbox-item" key={r.code}>
                  <input
                    type="checkbox"
                    checked={selectedRoles.includes(r.code)}
                    onChange={(e) =>
                      setSelectedRoles((prev) =>
                        e.target.checked
                          ? [...prev, r.code]
                          : prev.filter((c) => c !== r.code),
                      )
                    }
                  />
                  <span>
                    <span className="ci-name">{r.name}</span>
                    <span className="ci-desc">
                      {r.description || `权限：${r.permissions.join('、') || '（无）'}`}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {isCreate && canCreateSuperuser && (
            <label className="checkbox-item">
              <input
                type="checkbox"
                checked={isSuperuser}
                onChange={(e) => setIsSuperuser(e.target.checked)}
              />
              <span>
                <span className="ci-name">设为主管理员（超级管理员）</span>
                <span className="ci-desc">
                  拥有全部权限，不受任何部门与权限范围限制，可以管理其他管理员并重置其密码
                </span>
              </span>
            </label>
          )}
        </div>

        <div className="drawer-foot">
          <button type="button" className="btn" onClick={onClose}>
            {generated ? '我已保存密码' : '取消'}
          </button>
          {!generated && (
            <button
              type="button"
              className="btn primary"
              disabled={busy || (isCreate && !username.trim())}
              onClick={() => void submit()}
            >
              {busy ? '提交中…' : isCreate ? '创建' : '保存'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
/** 重置密码抽屉。结果只显示一次。 */
function ResetPasswordDrawer({
  account,
  onClose,
  onDone,
}: {
  account: AdminAccount
  onClose: () => void
  onDone: () => void
}) {
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ generated: string | null; message: string } | null>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await api.adminResetPassword(account.user_id, password.trim() || undefined)
      setResult({ generated: res.generated_password, message: res.message })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="drawer-mask" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3>
            <Icon name="alert" size={16} />
            重置密码 · {account.username}
          </h3>
          <button type="button" className="btn small" onClick={onClose}>
            <Icon name="close" size={14} />
          </button>
        </div>

        <div className="drawer-body">
          {error && <div className="error-box">{error}</div>}

          {result ? (
            <>
              {result.generated ? (
                <div className="secret-box">
                  <div className="secret-label">新密码 —— 只会显示这一次，请立刻复制发给本人</div>
                  <div className="secret-value">{result.generated}</div>
                </div>
              ) : (
                <div className="result-note">{result.message}</div>
              )}
              <div className="result-note">
                该账号的**全部登录会话已被撤销**。这是必须的：重置密码的动机通常是
                "怀疑账号被盗"，旧会话还有效的话，攻击者手里的令牌照样能换新的访问令牌。
              </div>
            </>
          ) : (
            <>
              <div className="result-note">
                原密码存储在系统里的只有不可逆哈希，**任何人都无法查看**。
                所以这里能做的只有"设置一个新密码"——这正是"掌管账号密码"的实际含义。
              </div>
              <label className="field">
                <span>新密码（留空则自动生成一个强密码）</span>
                <input
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="留空即自动生成"
                />
              </label>
            </>
          )}
        </div>

        <div className="drawer-foot">
          <button type="button" className="btn" onClick={result ? onDone : onClose}>
            {result ? '完成' : '取消'}
          </button>
          {!result && (
            <button type="button" className="btn primary" disabled={busy} onClick={() => void submit()}>
              {busy ? '重置中…' : '确认重置'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
