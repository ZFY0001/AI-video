/** 登录态：token 存取、会话恢复、资料更新。
 *
 * 为什么用模块级 store + useSyncExternalStore，而不是 Context：
 * 页面上只有导航栏和两个页面关心登录态，套一层 Provider 只为了传三个字段
 * 不值当。这里维护一份快照对象，emit 时整体替换引用，React 就能正确比对。
 *
 * **依赖方向是单向的**：`api.ts` 从这里取 token，这里不 import api.ts，
 * 避免循环依赖。所以登录/注册的 fetch 直接写在本文件里。
 */

import { useSyncExternalStore } from 'react'

export interface User {
  id: number
  email: string
  nickname: string
  avatar: string
  bio: string
  created_at: string
}

export interface ProfileStats {
  runs: number
  total_duration: number
  total_size_mb: number
  week_runs: number
  storage_mb: number
}

const TOKEN_KEY = 'yiyan.token'
const GUEST_KEY = 'yiyan.guest'

/** 访客凭据：随机 32 位十六进制，只存在这台浏览器里。
 *
 * 游客创建的项目和任务必须有归属，否则任何能访问本机服务的人都能读、能取消。
 * 它不是账号，也不换 token——登录后服务端只认账号，不会把游客项目带过去。
 */
function readGuestId(): string {
  try {
    const saved = localStorage.getItem(GUEST_KEY)
    if (saved && /^[0-9a-f]{32}$/.test(saved)) return saved
  } catch {
    /* 隐私模式：退到内存值，本次会话内仍可用 */
  }
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  const id = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  try {
    localStorage.setItem(GUEST_KEY, id)
  } catch {
    /* ignore */
  }
  return id
}

let guestId = readGuestId()

export function getGuestId(): string {
  return guestId
}

export function guestHeaders(): Record<string, string> {
  return { 'X-Guest-Id': guestId }
}

/** 媒体地址凭据后缀：`<img>` / `<video>` 带不了请求头，只能进 URL。 */
export function mediaCredentialQuery(sep: '?' | '&' = '?'): string {
  if (token) return `${sep}token=${encodeURIComponent(token)}`
  return `${sep}guest=${encodeURIComponent(guestId)}`
}

interface Snapshot {
  user: User | null
  token: string
  /** 是否已经尝试过恢复会话。false 时界面不该急着显示"未登录" */
  ready: boolean
}

function readToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

let token = readToken()
let user: User | null = null
let ready = false
let snapshot: Snapshot = { user, token, ready }
const listeners = new Set<() => void>()

function emit() {
  snapshot = { user, token, ready }
  listeners.forEach((fn) => fn())
}

export function getToken(): string {
  return token
}

export function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function setSession(nextToken: string, nextUser: User | null) {
  token = nextToken
  user = nextUser
  try {
    if (nextToken) localStorage.setItem(TOKEN_KEY, nextToken)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 隐私模式写不进去，本次会话内仍可用 */
  }
  emit()
}

export function subscribeAuth(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** 订阅登录态。
 *
 * **必须走 useSyncExternalStore**：直接 return snapshot 只是读了一次值，
 * 之后 emit 改的是模块变量，组件不会重渲染——登录成功后导航栏还是显示"未登录"。
 */
export function useAuth(): Snapshot {
  return useSyncExternalStore(
    subscribeAuth,
    () => snapshot,
    () => snapshot,
  )
}

/* ---------------------------------------------------------------- 请求 */

async function authFetch<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const res = await fetch(url, { ...init, headers })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body)
    } catch {
      /* 非 JSON 响应，保留状态码 */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

/* ---------------------------------------------------------------- 动作 */

/** 用本地 token 换回用户资料。失败（过期/被删）就静默清掉，不打扰用户。 */
export async function restoreSession(): Promise<void> {
  if (!token) {
    ready = true
    emit()
    return
  }
  try {
    const data = await authFetch<{ user: User }>('/api/auth/me')
    user = data.user
  } catch {
    token = ''
    user = null
    try {
      localStorage.removeItem(TOKEN_KEY)
    } catch {
      /* ignore */
    }
  } finally {
    ready = true
    emit()
  }
}

export async function login(email: string, password: string): Promise<void> {
  const data = await authFetch<{ token: string; user: User }>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  ready = true
  setSession(data.token, data.user)
}

export async function register(email: string, password: string, nickname: string): Promise<void> {
  const data = await authFetch<{ token: string; user: User }>('/api/auth/register', {
    method: 'POST',
    body: JSON.stringify({ email, password, nickname }),
  })
  ready = true
  setSession(data.token, data.user)
}

export function logout(): void {
  ready = true
  setSession('', null)
}

export async function saveProfile(patch: {
  nickname?: string
  bio?: string
  avatar?: string
}): Promise<User> {
  const data = await authFetch<{ user: User }>('/api/auth/me', {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
  user = data.user
  emit()
  return data.user
}

export async function fetchStats(): Promise<ProfileStats> {
  return authFetch<ProfileStats>('/api/auth/stats')
}

/* ---------------------------------------------------------------- 头像 */

/** 头像候选：一个中文字 + 一组渐变色。存的是 "字|色号"，避免为了头像去搞上传。 */
export const AVATAR_GLYPHS = ['言', '影', '光', '帧', '墨', '白', '川', '山', '火', '风']
export const AVATAR_TONES = [
  'linear-gradient(140deg,#6e77f2,#4a54e1 60%,#3b41b8)',
  'linear-gradient(140deg,#4fc7c0,#0fa3a3 60%,#0b7f80)',
  'linear-gradient(140deg,#f0a35e,#d9772f 60%,#b85f1d)',
  'linear-gradient(140deg,#e2779b,#c94a76 60%,#a3335b)',
  'linear-gradient(140deg,#8f8bd6,#635ec0 60%,#4a45a0)',
]

/** 把头像字段拆成「字」和「渐变」。空值给一个按 id 派生的稳定默认。 */
export function avatarOf(user: User | null): { glyph: string; tone: string } {
  const raw = (user?.avatar || '').trim()
  if (raw) {
    const [glyph, idx] = raw.split('|')
    const tone = AVATAR_TONES[Number(idx) % AVATAR_TONES.length] || AVATAR_TONES[0]
    return { glyph: (glyph || user?.nickname || '言').slice(0, 1), tone }
  }
  const seed = user?.id ?? 0
  return {
    glyph: (user?.nickname || '言').slice(0, 1),
    tone: AVATAR_TONES[seed % AVATAR_TONES.length],
  }
}
