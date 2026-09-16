import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { saveSession, type TokenPair } from '../auth'

/**
 * 登录页。
 *
 * ## 几个刻意的处理
 *
 * 1. **错误信息原样展示后端那句话**：后端已经把"用户名或密码不正确"
 *    和"账号已被锁定，请 12 分钟后再试"分开了。前端再包一层
 *    "登录失败" 会把最有用的信息（该等还是该找管理员）盖掉。
 *
 * 2. **提交中禁用按钮**：不禁用的话手快点两下会发两次登录请求，
 *    而第二次如果恰好赶上失败计数阈值，会在用户看来"第一次就锁了"。
 *
 * 3. **不在前端做密码强度校验**：策略在后端（`passwords.validate_strength`），
 *    前端再实现一遍必然和后端漂移 —— 而漂移的表现是
 *    "前端说可以，后端拒了"，用户完全不知道为什么。
 *
 * 4. **扫码登录按钮置灰 + 说明**：后端 `/auth/providers` 会告诉我们
 *    哪些 provider 只留了适配位（`implemented=false`）。
 *    让用户点了才发现 501，比一开始就说"还没接"要差。
 */
export default function Login() {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [providers, setProviders] = useState<
    { provider: string; configured: boolean; implemented: boolean; hint: string }[]
  >([])

  useEffect(() => {
    // 扫码登录目前只有适配位，拿回来是为了显示"已规划但未接入"，
    // 而不是让用户点了才发现。接口本身不需要登录。
    api
      .providers()
      .then((data) => setProviders(data.providers))
      .catch(() => setProviders([]))
  }, [])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const pair: TokenPair = await api.login(username.trim(), password)
      saveSession(pair)
      // 用 replace：登录后按浏览器后退不该回到登录页
      //
      // 落点是**对话页**而不是看板：用户登录后想干的第一件事通常是
      // 说一句话（"最近退货是不是变多了？"），而不是先看一堆图表。
      navigate('/chat', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <h1>服装电商多 Agent 运营中台</h1>
        <p className="login-sub">FashionOps · 请登录后使用</p>

        <label className="field">
          <span>用户名</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>

        <label className="field">
          <span>密码</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {error && <div className="error-box">{error}</div>}

        <button className="button primary" type="submit" disabled={busy}>
          {busy ? '登录中…' : '登录'}
        </button>

        {providers.length > 0 && (
          <div className="login-oauth">
            {providers.map((p) => (
              <button
                key={p.provider}
                type="button"
                className="button"
                disabled
                title={p.hint || '已规划，尚未接入'}
              >
                {p.provider === 'dingtalk' ? '钉钉扫码登录' : '企业微信扫码登录'}
                <span className="muted"> · 未接入</span>
              </button>
            ))}
            <p className="hint-text">
              扫码登录已留好适配位（数据模型、配置项、流程抽象均就绪），
              真实 HTTP 调用待接入。见 <code>security/oauth.py</code>。
            </p>
          </div>
        )}

        <p className="login-help">
          没有账号？在服务器上执行：
          <br />
          <code>.venv\Scripts\fashionops-user.exe create-admin &lt;用户名&gt;</code>
          <br />
          密码只显示一次。
        </p>
      </form>
    </div>
  )
}
