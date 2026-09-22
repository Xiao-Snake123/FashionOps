/**
 * 会话（认证）状态。
 *
 * ## 令牌存哪里：一个必须说清楚的取舍
 *
 * | 方案 | 好处 | 代价 |
 * | --- | --- | --- |
 * | `localStorage` | 刷新页面不掉线，代码简单 | **XSS 能读到**，令牌会随任何注入脚本外泄 |
 * | 内存（变量） | XSS 拿不到（要主动调 API 才行） | 刷新页面就掉线 |
 * | httpOnly Cookie | XSS 读不到 | **CSRF** 要额外防；跨域调试麻烦 |
 *
 * 这里选的是 **`localStorage` 存 refresh token + 内存存 access token**。
 * 理由是：
 *
 * 1. access token 只活 30 分钟且在内存里，XSS 要在**同一个页面会话内**才能偷到；
 * 2. refresh token 虽然在 localStorage，但它**服务端可撤销**（`refresh_tokens` 表），
 *    发现异常能立刻踢下线 —— 这是当初把它入库而不是纯 JWT 的原因；
 * 3. refresh 每次使用都轮换（见后端 `service.refresh`），被偷的那份一旦用了，
 *    合法用户下次刷新就会触发重放检测 → **整条链被撤销** → 攻击者也被踢出去。
 *
 * **必须诚实说明**：这不是"XSS 安全"的方案，这只是"XSS 的损失有上限"的方案。
 * 真正要防 XSS 得靠 CSP + 不给用户输入直出的口子（React 默认转义已经挡了大部分）。
 * 上线到公网前应该改成 httpOnly Cookie + CSRF token，那时这个文件的接口不用变。
 *
 * ## 为什么 401 之后要"自动刷新一次再重试"
 *
 * access token 只有 30 分钟。如果 401 就直接跳登录页，用户每半小时被打断一次 ——
 * 而**他本来是有有效 refresh token 的**，完全不需要重新输密码。
 *
 * 关键在于**并发去重**：一个页面同时发 5 个请求，token 过期时 5 个都会 401。
 * 不去重的话会同时发起 5 次 refresh，而**刷新是轮换的** ——
 * 后 4 次拿的是已经被前一次替换掉的旧 token，直接触发服务端的重放检测，
 * 把用户整条会话链撤销、强制下线。
 *
 * 这个 bug 只在"页面同时发多个请求 + token 恰好过期"时出现，
 * 手工点页面几乎测不出来。所以用 `refreshPromise` 做**单飞（single-flight）**。
 */

export interface User {
  user_id: number
  username: string
  full_name: string
  roles: string[]
  permissions: string[]
  is_superuser: boolean
  /** 所属部门。为 null 表示不属于任何部门（超管、对接账号）。 */
  department_id?: number | null
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: User
}

const ACCESS_KEY = 'fashionops.access_token'
const REFRESH_KEY = 'fashionops.refresh_token'
const USER_KEY = 'fashionops.user'

/**
 * access token **同时**放内存和 localStorage。
 *
 * 放内存是为了让 `currentAccessToken()` 的读取零成本（每次请求都要读）；
 * 放 localStorage 是为了刷新页面后不用立刻走一次 refresh（少一次往返）。
 * 两者用同一个 setter 写入，不会不同步。
 */
let accessToken: string | null = localStorage.getItem(ACCESS_KEY)
let refreshToken: string | null = localStorage.getItem(REFRESH_KEY)
let currentUser: User | null = readStoredUser()

/** 正在进行的刷新。用来做单飞，见文件头注释。 */
let refreshPromise: Promise<TokenPair | null> | null = null

/** 会话失效时的回调。App 用它跳登录页 —— 认证模块不直接操作路由。 */
let onSessionExpired: (() => void) | null = null

function readStoredUser(): User | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    // 存坏了就当没有。抛异常会让整个应用起不来，而这只影响一个提示条
    localStorage.removeItem(USER_KEY)
    return null
  }
}

export function setSessionExpiredHandler(handler: (() => void) | null): void {
  onSessionExpired = handler
}

export function saveSession(pair: TokenPair): void {
  accessToken = pair.access_token
  refreshToken = pair.refresh_token
  currentUser = pair.user
  localStorage.setItem(ACCESS_KEY, pair.access_token)
  localStorage.setItem(REFRESH_KEY, pair.refresh_token)
  localStorage.setItem(USER_KEY, JSON.stringify(pair.user))
}

/** 更新用户信息（比如 `GET /auth/me` 拿到最新权限后）。 */
export function saveUser(user: User): void {
  currentUser = user
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearSession(): void {
  accessToken = null
  refreshToken = null
  currentUser = null
  refreshPromise = null
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
  localStorage.removeItem(USER_KEY)
}

export function currentAccessToken(): string | null {
  return accessToken
}

export function currentRefreshToken(): string | null {
  return refreshToken
}

export function currentUserSnapshot(): User | null {
  return currentUser
}

/**
 * 有没有某项权限。**只用于决定界面显示什么，不是安全边界。**
 *
 * 后端每个接口都会独立校验 —— 把"隐藏按钮"当安全措施是最常见的越权来源。
 * 这里的作用是让用户**不用点了才发现没权限**。
 */
export function hasPermission(user: User | null, ...codes: string[]): boolean {
  if (!user) return false
  if (user.is_superuser) return true
  if (codes.length === 0) return true
  return codes.some((code) => user.permissions.includes(code))
}

/** 会话是否可能还有效（有 refresh token 就算"可能"，真伪由服务端判定）。 */
export function hasStoredSession(): boolean {
  return Boolean(refreshToken)
}

/** 仅供测试/登出时重置模块内部状态。 */
export function __resetForTest(): void {
  accessToken = null
  refreshToken = null
  currentUser = null
  refreshPromise = null
  onSessionExpired = null
}

export function __setRefreshPromiseForTest(value: Promise<TokenPair | null> | null): void {
  refreshPromise = value
}

export function getRefreshPromise(): Promise<TokenPair | null> | null {
  return refreshPromise
}

export function setRefreshPromise(value: Promise<TokenPair | null> | null): void {
  refreshPromise = value
}

/** 会话失效：清干净并通知上层（跳登录页）。 */
export function expireSession(): void {
  clearSession()
  onSessionExpired?.()
}
