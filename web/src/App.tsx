import { Component, useEffect, useRef, useState, type ReactNode } from 'react'

import { TopNav } from './components/TopNav'
import { useRun } from './hooks/useRun'
import { fetchMeta, type Meta } from './lib/api'
import { restoreSession } from './lib/auth'
import { editGuardMessage } from './lib/editGuard'
import { CreatePage } from './pages/CreatePage'
import { PlanPage } from './pages/PlanPage'
import { HistoryPage } from './pages/HistoryPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { TasksPage } from './pages/TasksPage'
import { SettingsPage } from './pages/SettingsPage'
import { ProfilePage } from './pages/ProfilePage'
import { AuthPage } from './pages/AuthPage'

type Theme = 'light' | 'dark'
type Route = 'create' | 'projects' | 'tasks' | 'history' | 'settings' | 'plan' | 'profile' | 'login' | 'invalid'

const THEME_KEY = 'yiyan.theme'

function initialTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

/** 解码失败返回 null，而不是把 URIError 抛到渲染栈上。 */
function safeDecode(text: string): string | null {
  try {
    return decodeURIComponent(text)
  } catch {
    return null
  }
}

/** 解析 hash。`#plan/<run_id>` 带参数，所以不能只按"整串是不是已知路由"判断。
 *
 * 非法百分号编码（`#plan/%`）以前会抛 URIError，而它在 `useState(parseLocation)`
 * 的初始化里执行——整个页面直接白屏，连导航都没有。
 */
function parseLocation(): { route: Route; planId: string } {
  const raw = window.location.hash.replace(/^#\/?/, '')
  const [head, ...rest] = raw.split('/')
  if (head === 'plan') {
    const id = safeDecode(rest.join('/'))
    const invalid =
      id === null ||
      !id ||
      id.length > 200 ||
      id === '.' ||
      id === '..' ||
      id.includes('/') ||
      id.includes('\\') ||
      id.includes('\0')
    if (invalid) return { route: 'invalid', planId: '' }
    return { route: 'plan', planId: id }
  }
  if (head === 'projects') return { route: 'projects', planId: '' }
  if (head === 'tasks') return { route: 'tasks', planId: '' }
  if (head === 'history') return { route: 'history', planId: '' }
  if (head === 'settings') return { route: 'settings', planId: '' }
  if (head === 'profile') return { route: 'profile', planId: '' }
  if (head === 'login') return { route: 'login', planId: '' }
  return { route: 'create', planId: '' }
}

/** 页面级错误边界：单个页面渲染失败不该让整个应用白屏。 */
class ErrorBoundary extends Component<
  { children: ReactNode; onHome: () => void },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: unknown) {
    console.error('页面渲染失败', error, info)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="mx-auto w-full max-w-[720px] px-6 pt-16">
        <div className="rounded-xl border border-err/30 bg-err/[0.06] px-6 py-8 text-center">
          <p className="text-[15px] font-medium">这个页面出错了</p>
          <p className="mt-2 text-[12.5px] leading-relaxed text-ink-mute">
            {this.state.error.message || '未知错误'}。已生成的内容不会因此丢失。
          </p>
          <div className="mt-5 flex justify-center gap-2">
            <button className="btn btn-primary" onClick={this.props.onHome}>
              回创作台
            </button>
            <button className="btn btn-ghost" onClick={() => window.location.reload()}>
              重新加载
            </button>
          </div>
        </div>
      </div>
    )
  }
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [bootError, setBootError] = useState('')
  const [theme, setTheme] = useState<Theme>(initialTheme)
  const [loc, setLoc] = useState(parseLocation)
  const scroller = useRef<HTMLElement>(null)
  /** 最后一次被接受的地址。后退时用它判断"是不是真的要离开"，也用它回退。 */
  const acceptedHash = useRef(window.location.hash)
  const restoring = useRef(false)
  const run = useRun()

  useEffect(() => {
    fetchMeta()
      .then(setMeta)
      .catch((err: unknown) => {
        setBootError(err instanceof Error ? err.message : '无法连接本地服务')
      })
    // 用本地 token 换回用户资料：刷新页面后不该显示成"未登录"
    void restoreSession()
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* 隐私模式下写不进去，不影响显示 */
    }
  }, [theme])

  useEffect(() => {
    const onHash = () => {
      // 浏览器前进/后退绕过了 navigate()，这里补同一道确认；取消就把地址改回去。
      if (restoring.current) {
        restoring.current = false
      } else {
        const warning = editGuardMessage()
        if (warning && window.location.hash !== acceptedHash.current) {
          if (!window.confirm(`${warning}\n\n离开这个页面会丢掉这些改动，确定离开？`)) {
            restoring.current = true
            window.location.hash = acceptedHash.current
            return
          }
        }
      }
      acceptedHash.current = window.location.hash
      setLoc(parseLocation())
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!editGuardMessage()) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [])

  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 })
  }, [loc.route])

  const navigate = (next: string) => {
    const hash = next === 'create' ? '' : `#${next}`
    if (hash !== window.location.hash) {
      const warning = editGuardMessage()
      if (warning && !window.confirm(`${warning}\n\n离开这个页面会丢掉这些改动，确定离开？`)) return
    }
    acceptedHash.current = hash
    window.location.hash = hash
    setLoc(parseLocation())
  }

  // 主题只在 documentElement 上设一次，全局唯一。
  // 曾经在成片墙给外壳强挂 data-theme="dark" 做「放映厅」，结果那个页面上
  // 主题按钮点下去毫无反应——用户看到的就是「切换不统一」。现在取消强制，
  // 所有页面（含成片墙、设置）一律跟随同一个主题。
  return (
    <div className="relative h-full overflow-hidden">
      <div className="aurora" aria-hidden="true">
        <span className="aurora-a" />
        <span className="aurora-b" />
        <span className="aurora-c" />
      </div>
      <div className="mesh" aria-hidden="true" />

      <main ref={scroller} className="smooth-scroll scroll-thin relative z-10 h-full overflow-y-auto">
        <TopNav
          env={run.env}
          version={meta?.version ?? '—'}
          theme={theme}
          route={loc.route}
          onNavigate={navigate}
          onToggleTheme={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
        />

        {bootError && (
          <div className="mx-auto mt-4 mb-1 max-w-[1340px] px-6">
            <div className="rounded-xl border border-err/30 bg-err/[0.07] px-4 py-3">
              <p className="text-[12.5px] text-err">
                连不上本地服务：{bootError}
                <span className="ml-2 text-ink-mute">请确认后端已启动</span>
              </p>
            </div>
          </div>
        )}

        <ErrorBoundary key={`${loc.route}:${loc.planId}`} onHome={() => navigate('create')}>
        {loc.route === 'invalid' && (
          <div className="mx-auto w-full max-w-[720px] px-6 pt-16">
            <div className="rounded-xl border border-[var(--ring)] bg-[var(--ctl)]/40 px-6 py-10 text-center">
              <p className="text-[15px] font-medium">链接无效或已损坏</p>
              <p className="mt-2 text-[12.5px] leading-relaxed text-ink-mute">
                地址里的项目 ID 无法识别（可能被截断，或含有非法字符）。
              </p>
              <div className="mt-5 flex justify-center">
                <button className="btn btn-primary" onClick={() => navigate('create')}>
                  回创作台
                </button>
              </div>
            </div>
          </div>
        )}
        {loc.route === 'create' && (
          <div id="workspace" className="relative">
            <CreatePage meta={meta} run={run} onPlan={(id) => navigate(`plan/${id}`)} />
          </div>
        )}
        {loc.route === 'projects' && <ProjectsPage onNavigate={navigate} />}
        {loc.route === 'history' && <HistoryPage onNavigate={navigate} />}
        {loc.route === 'tasks' && (
          <TasksPage
            onNavigate={navigate}
            // 接回一条正在跑的任务：与刷新页面自动恢复走同一条路，
            // 接上之后回创作台看日志与进度。
            onWatch={(id) => {
              void run.attach(id)
              navigate('create')
            }}
          />
        )}
        {loc.route === 'plan' && (
          <PlanPage
            runId={loc.planId}
            onNavigate={navigate}
            // 按拆解拍摄：起的是与「开始生成」同一条事件流，只是入口不同。
            // 起完立刻回创作台看进度，用户不用在两个页面之间来回切。
            onRender={(id, opts) => {
              void run.startPlan(id, opts)
              navigate('create')
            }}
            // 低清预览：产物在 preview/ 下，只用来核对时间轴/字幕/音轨。
            onPreview={(id, opts) => {
              void run.startPreview(id, opts)
              navigate('create')
            }}
            // 单镜预览：只出这一镜的低清片段，几十秒内看效果，不动正式成片。
            onPreviewShot={(id, shotId, opts) => {
              void run.startShotPreview(id, shotId, opts)
              navigate('create')
            }}
            // 单镜重拍：同样起一条事件流，只是后端只重拍这一镜再整片重新合成。
            // 起完也回创作台看进度——重合成同样要等，用户得看见它在动。
            onReshoot={(id, shotId, opts) => {
              void run.startShot(id, shotId, opts)
              navigate('create')
            }}
            // 只重合成：改过镜长或顺序之后重拼。同样回创作台看进度——
            // 它虽然不重画，但重编码整片仍要等一会儿。
            onRecompose={(id, opts) => {
              void run.startRecompose(id, opts)
              navigate('create')
            }}
          />
        )}
        {loc.route === 'settings' && <SettingsPage onEnvChange={run.publishEnv} />}
        {loc.route === 'profile' && <ProfilePage onNavigate={navigate} />}
        {loc.route === 'login' && (
          <AuthPage onDone={() => navigate('profile')} onSkip={() => navigate('create')} />
        )}
        </ErrorBoundary>
      </main>
    </div>
  )
}
