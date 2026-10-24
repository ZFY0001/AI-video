import { useEffect, useRef, useState } from 'react'

import { TopNav } from './components/TopNav'
import { useRun } from './hooks/useRun'
import { fetchMeta, type Meta } from './lib/api'
import { restoreSession } from './lib/auth'
import { CreatePage } from './pages/CreatePage'
import { PlanPage } from './pages/PlanPage'
import { HistoryPage } from './pages/HistoryPage'
import { SettingsPage } from './pages/SettingsPage'
import { ProfilePage } from './pages/ProfilePage'
import { AuthPage } from './pages/AuthPage'

type Theme = 'light' | 'dark'
type Route = 'create' | 'history' | 'settings' | 'plan' | 'profile' | 'login'

const THEME_KEY = 'yiyan.theme'

function initialTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

/** 解析 hash。`#plan/<run_id>` 带参数，所以不能只按"整串是不是已知路由"判断。 */
function parseLocation(): { route: Route; planId: string } {
  const raw = window.location.hash.replace(/^#\/?/, '')
  const [head, ...rest] = raw.split('/')
  if (head === 'plan') return { route: 'plan', planId: decodeURIComponent(rest.join('/')) }
  if (head === 'history') return { route: 'history', planId: '' }
  if (head === 'settings') return { route: 'settings', planId: '' }
  if (head === 'profile') return { route: 'profile', planId: '' }
  if (head === 'login') return { route: 'login', planId: '' }
  return { route: 'create', planId: '' }
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [bootError, setBootError] = useState('')
  const [theme, setTheme] = useState<Theme>(initialTheme)
  const [loc, setLoc] = useState(parseLocation)
  const scroller = useRef<HTMLElement>(null)
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
    const onHash = () => setLoc(parseLocation())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 })
  }, [loc.route])

  const navigate = (next: string) => {
    window.location.hash = next === 'create' ? '' : `#${next}`
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

      <main ref={scroller} className="smooth-scroll scroll-thin no-rail relative z-10 h-full overflow-y-auto">
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

        {loc.route === 'create' && (
          <div id="workspace" className="relative">
            <CreatePage meta={meta} run={run} onPlan={(id) => navigate(`plan/${id}`)} />
          </div>
        )}
        {loc.route === 'history' && <HistoryPage onNavigate={navigate} />}
        {loc.route === 'plan' && (
          <PlanPage
            runId={loc.planId}
            onNavigate={navigate}
            // 按拆解拍摄：起的是与「开始生成」同一条事件流，只是入口不同。
            // 起完立刻回创作台看进度，用户不用在两个页面之间来回切。
            onRender={(id) => {
              void run.startPlan(id)
              navigate('create')
            }}
            // 单镜重拍：同样起一条事件流，只是后端只重拍这一镜再整片重新合成。
            // 起完也回创作台看进度——重合成同样要等，用户得看见它在动。
            onReshoot={(id, shotId) => {
              void run.startShot(id, shotId)
              navigate('create')
            }}
            // 只重合成：改过镜长或顺序之后重拼。同样回创作台看进度——
            // 它虽然不重画，但重编码整片仍要等一会儿。
            onRecompose={(id) => {
              void run.startRecompose(id)
              navigate('create')
            }}
          />
        )}
        {loc.route === 'settings' && <SettingsPage />}
        {loc.route === 'profile' && <ProfilePage onNavigate={navigate} />}
        {loc.route === 'login' && (
          <AuthPage onDone={() => navigate('profile')} onSkip={() => navigate('create')} />
        )}
      </main>
    </div>
  )
}
