import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api, ApiError, type Summary } from './api'
import {
  clearSession,
  currentRefreshToken,
  currentUserSnapshot,
  hasPermission,
  hasStoredSession,
  saveUser,
  setSessionExpiredHandler,
  type User,
} from './auth'
import Dashboard from './pages/Dashboard'
import Tasks from './pages/Tasks'
import TaskDetail from './pages/TaskDetail'
import Products from './pages/Products'
import Tickets from './pages/Tickets'
import Campaigns from './pages/Campaigns'
import Emails from './pages/Emails'
import Login from './pages/Login'

type SessionState = 'checking' | 'anonymous' | 'authenticated'

export default function App() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [backendDown, setBackendDown] = useState<string | null>(null)
  const [user, setUser] = useState<User | null>(currentUserSnapshot())
  const [session, setSession] = useState<SessionState>(() =>
    hasStoredSession() ? 'checking' : 'anonymous',
  )
  const location = useLocation()
  const navigate = useNavigate()

  // 会话失效（refresh 用尽 / 被踢下线）时统一跳登录页。
  //
  // 让 auth 模块**回调**而不是让它直接 import 路由：那样会把认证逻辑和
  // react-router 耦在一起，而认证逻辑本身是纯的、可单独测的。
  // 回调也让"跳到哪里"变成调用方的决定。
  useEffect(() => {
    setSessionExpiredHandler(() => {
      setUser(null)
      setSession('anonymous')
      setSummary(null)
      navigate('/login', { replace: true })
    })
    return () => setSessionExpiredHandler(null)
  }, [navigate])

  /**
   * 启动时校验一次会话。
   *
   * **为什么不能"localStorage 里有 token 就当已登录"**：
   * token 可能在服务端已经被撤销（管理员踢下线、改密码、重放检测），
   * 也可能因为服务端换了 `jwt_secret` 而失效。
   * 不校验的话，用户会看到一个空壳界面，每个组件各自报 401，
   * 而不是干脆地跳登录页。
   */
  useEffect(() => {
    if (session !== 'checking') return
    let alive = true
    ;(async () => {
      try {
        // 先试 /me：access token 还有效就不用刷新（省一次往返）
        const me = await api.me()
        if (!alive) return
        saveUser(me)
        setUser(me)
        setSession('authenticated')
      } catch (error) {
        if (!alive) return
        if (error instanceof ApiError && error.status === 401) {
          // access 过期但 refresh 还有效 —— 这是最常见的正常情况（离开一晚回来）。
          // 请求层也会自己续期，但启动时显式试一次能让状态切换更干脆。
          const pair = await api.refresh()
          if (!alive) return
          if (pair) {
            setUser(pair.user)
            setSession('authenticated')
            return
          }
        }
        clearSession()
        setUser(null)
        setSession('anonymous')
      }
    })()
    return () => {
      alive = false
    }
  }, [session])

  // 概览数字。只在已登录时拉 —— 否则会在登录页打出一串 401 噪音。
  useEffect(() => {
    if (session !== 'authenticated') return
    let alive = true
    api
      .summary()
      .then((data) => {
        if (!alive) return
        setSummary(data)
        setBackendDown(null)
      })
      .catch((error: Error) => {
        // 401 已经被请求层处理过（续期 + 跳登录），这里不用再管
        if (alive && !(error instanceof ApiError && error.status === 401)) {
          setBackendDown(error.message)
        }
      })
    return () => {
      alive = false
    }
  }, [session, location.pathname])

  const logout = useCallback(async () => {
    try {
      // 通知服务端撤销 refresh token。**失败也要继续清本地** ——
      // 断网时登不出去会让用户以为"退出没生效"，反而更危险。
      await api.logout(currentRefreshToken())
    } catch {
      /* 忽略：本地清干净才是重点 */
    }
    clearSession()
    setUser(null)
    setSession('anonymous')
    setSummary(null)
    navigate('/login', { replace: true })
  }, [navigate])

  if (location.pathname === '/login') {
    // 已登录还去登录页就直接送回去，免得"登录后按后退又回到登录页"
    return session === 'authenticated' ? <Navigate to="/dashboard" replace /> : <Login />
  }

  if (session === 'checking') {
    return (
      <div className="login-page">
        <div className="login-card">
          <h1>正在校验登录状态…</h1>
          <p className="login-sub">如果这一步一直不动，多半是后端没启动</p>
        </div>
      </div>
    )
  }

  if (session !== 'authenticated') {
    // 用**路由跳转**而不是直接渲染 `<Login />`：
    // 后者会让地址栏停在 /tasks，用户刷新页面还是看不到"我没登录"的线索。
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <h1>服装电商多 Agent 运营中台</h1>
          <small>FashionOps · 阶段 0</small>
        </div>

        <NavLink to="/dashboard" className="nav-link">
          <span>数据看板</span>
        </NavLink>
        <NavLink to="/tasks" className="nav-link">
          <span>任务工作台</span>
          {summary && summary.running_tasks > 0 && (
            <span className="nav-badge">{summary.running_tasks}</span>
          )}
        </NavLink>
        <NavLink to="/tickets" className="nav-link">
          <span>客服工单</span>
          {summary && summary.escalated_tickets > 0 && (
            <span className="nav-badge">{summary.escalated_tickets}</span>
          )}
        </NavLink>
        <NavLink to="/campaigns" className="nav-link">
          <span>广告投放</span>
        </NavLink>

        {/* 邮件记录里有收件人地址与正文，需要 mail:send 权限。
            没权限就不显示 —— 比让用户点了收到 403 好。
            **这只是体验，后端照样会拦。** */}
        {hasPermission(user, 'mail:send') && (
          <NavLink to="/emails" className="nav-link">
            <span>邮件闸门</span>
          </NavLink>
        )}

        <NavLink to="/products" className="nav-link">
          <span>商品库</span>
        </NavLink>

        <div className="sidebar-footer">
          <div className="user-chip">
            <div className="user-name">{user?.full_name || user?.username}</div>
            <div className="user-roles">
              {user?.is_superuser ? '超级管理员' : user?.roles.join(' / ') || '（无角色）'}
            </div>
          </div>
          <button className="button small" type="button" onClick={logout}>
            退出登录
          </button>
          <div className="sidebar-meta">
            <div>
              后端{' '}
              <span className={backendDown ? 'muted' : ''}>
                {backendDown ? '未连接' : '已连接'}
              </span>
            </div>
            <div>
              <a href="/docs" target="_blank" rel="noreferrer">
                API 文档
              </a>
            </div>
          </div>
        </div>
      </aside>

      <main className="content">
        {backendDown && (
          <div className="error-box">
            连不上后端（{backendDown}）。请确认后端已启动：
            <span className="mono">
              {' '}
              uv run uvicorn fashionops.api.main:app --reload --port 8000
            </span>
          </div>
        )}
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/:taskNo" element={<TaskDetail />} />
          <Route path="/tickets" element={<Tickets />} />
          <Route path="/campaigns" element={<Campaigns />} />
          <Route path="/emails" element={<Emails />} />
          <Route path="/products" element={<Products />} />
          {/* 兜底：未知路径回看板，而不是显示一片空白 */}
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  )
}
