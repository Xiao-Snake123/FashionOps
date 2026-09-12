import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api, type Summary } from './api'
import Dashboard from './pages/Dashboard'
import Tasks from './pages/Tasks'
import TaskDetail from './pages/TaskDetail'
import Products from './pages/Products'
import Tickets from './pages/Tickets'
import Campaigns from './pages/Campaigns'
import Emails from './pages/Emails'

export default function App() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [backendDown, setBackendDown] = useState<string | null>(null)
  const location = useLocation()

  useEffect(() => {
    let alive = true
    api
      .summary()
      .then((data) => {
        if (alive) {
          setSummary(data)
          setBackendDown(null)
        }
      })
      .catch((error: Error) => {
        if (alive) setBackendDown(error.message)
      })
    return () => {
      alive = false
    }
  }, [location.pathname])

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
          {summary && summary.running_tasks > 0 && <span className="nav-badge">{summary.running_tasks}</span>}
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
        <NavLink to="/emails" className="nav-link">
          <span>邮件闸门</span>
        </NavLink>
        <NavLink to="/products" className="nav-link">
          <span>商品库</span>
        </NavLink>

        <div style={{ marginTop: 'auto', padding: '16px 10px 0', fontSize: 11.5, color: 'var(--text-dim)' }}>
          <div>
            后端 <span className={backendDown ? 'muted' : ''}>{backendDown ? '未连接' : '已连接'}</span>
          </div>
          <div>
            <a href="/docs" target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>
              API 文档
            </a>
          </div>
        </div>
      </aside>

      <main className="content">
        {backendDown && (
          <div className="error-box">
            连不上后端（{backendDown}）。请确认后端已启动：
            <span className="mono"> uv run uvicorn fashionops.api.main:app --reload --port 8000</span>
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
        </Routes>
      </main>
    </div>
  )
}
