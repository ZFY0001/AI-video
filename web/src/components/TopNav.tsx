import type { EnvPayload } from '../lib/api'
import { avatarOf, useAuth } from '../lib/auth'

const NAV = [
  { key: 'create', label: '创作台' },
  { key: 'history', label: '成片墙' },
  { key: 'settings', label: '设置' },
  { key: 'profile', label: '个人中心' },
] as const

function Logo() {
  return (
    <span
      className="relative grid h-[22px] w-[22px] shrink-0 place-items-center rounded-[7px]"
      style={{
        background: 'linear-gradient(140deg, #6e77f2, #4a54e1 55%, #3b41b8)',
        boxShadow: 'inset 0 1px 0 rgba(255,255,255,.4), 0 4px 12px -4px rgba(74,84,225,.6)',
      }}
      aria-hidden="true"
    >
      <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
        <path d="M4.1 2.6 L9.4 6 L4.1 9.4 Z" fill="#fff" />
        <path
          d="M2.2 2.2 h1.4 M8.4 9.8 h1.4 M2.2 9.8 h1.4"
          stroke="#fff"
          strokeWidth="1.1"
          strokeLinecap="round"
          opacity="0.55"
        />
      </svg>
    </span>
  )
}

function ThemeToggle({ theme, onToggle }: { theme: 'light' | 'dark'; onToggle: () => void }) {
  const dark = theme === 'dark'
  return (
    <button
      type="button"
      onClick={onToggle}
      title={dark ? '切到浅色' : '切到深色'}
      aria-label={dark ? '切到浅色' : '切到深色'}
      className="grid h-[30px] w-[30px] place-items-center rounded-[9px] border border-transparent text-ink-dim transition-colors hover:border-[var(--ctl-edge)] hover:bg-[var(--ctl)] hover:text-ink"
    >
      {dark ? (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="12" cy="12" r="4.2" stroke="currentColor" strokeWidth="1.5" />
          <path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2M5.4 5.4l1.6 1.6M17 17l1.6 1.6M18.6 5.4L17 7M7 17l-1.6 1.6"
            stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      ) : (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M20 14.2A8.2 8.2 0 0 1 9.8 4a8.4 8.4 0 1 0 10.2 10.2Z"
            stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
        </svg>
      )}
    </button>
  )
}

/** 右上角的账号入口：登录了显示头像，没登录显示"登录"。
 *
 * 把它放在导航项之外，是因为登录态是"状态"不是"页面"——用户扫一眼就知道
 * 自己是谁，不用点进个人中心才知道。
 */
function UserChip({ route, onNavigate }: { route: string; onNavigate: (next: string) => void }) {
  const { user, ready } = useAuth()

  if (!ready) return <span className="h-[30px] w-[30px]" />

  if (!user) {
    return (
      <button
        type="button"
        onClick={() => onNavigate('login')}
        className={`h-[30px] rounded-[9px] border border-[var(--ctl-edge)] bg-[var(--ctl)] px-3 text-[12.5px] font-medium transition-colors hover:text-ink ${
          route === 'login' ? 'text-ink' : 'text-ink-dim'
        }`}
      >
        登录
      </button>
    )
  }

  const avatar = avatarOf(user)
  return (
    <button
      type="button"
      onClick={() => onNavigate('profile')}
      title={`${user.nickname} · 个人中心`}
      aria-label="个人中心"
      className={`grid h-[30px] w-[30px] place-items-center rounded-[9px] text-[12px] font-semibold text-white transition-transform hover:scale-105 ${
        route === 'profile' ? 'ring-2 ring-brand' : ''
      }`}
      style={{ background: avatar.tone, boxShadow: 'inset 0 1px 0 rgba(255,255,255,.35)' }}
    >
      {avatar.glyph}
    </button>
  )
}

export function TopNav({
  env,
  version,
  theme,
  route,
  onNavigate,
  onToggleTheme,
}: {
  env: EnvPayload | null
  version: string
  theme: 'light' | 'dark'
  route: string
  onNavigate: (next: string) => void
  onToggleTheme: () => void
}) {
  const tone = !env ? 'idle' : env.ready ? 'ok' : 'warn'
  const text = !env ? '待体检' : env.ready ? '环境就绪' : '有降级项'

  return (
    <header className="sticky top-0 z-40 border-b border-[var(--ring)] bg-[var(--nav-bg)] backdrop-blur-2xl">
      <div className="mx-auto flex h-[56px] max-w-[1340px] items-center gap-3 px-6">
        <a
          href="#workspace"
          className="flex items-center gap-2.5"
          onClick={(e) => {
            e.preventDefault()
            document.getElementById('workspace')?.scrollIntoView({ behavior: 'smooth' })
          }}
        >
          <Logo />
          <span className="text-[14px] font-semibold tracking-[-0.01em]">一言片场</span>
        </a>

        <nav className="ml-5 hidden items-stretch self-stretch md:flex">
          {NAV.map((item) => {
            const active = route === item.key
            return (
              <button
                key={item.key}
                type="button"
                onClick={() => onNavigate(item.key)}
                className={[
                  'relative flex items-center px-3.5 text-[14px] transition-colors',
                  active ? 'font-semibold text-ink' : 'text-ink-dim hover:text-ink',
                ].join(' ')}
              >
                {item.label}
                {active && (
                  <span
                    className="absolute inset-x-3 bottom-0 h-[2px] rounded-t-sm"
                    style={{ background: 'var(--color-brand)', boxShadow: '0 0 10px rgba(74,84,225,.55)' }}
                  />
                )}
              </button>
            )
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <span className="hidden sm:flex sm:items-center sm:gap-1.5">
            <span
              className="h-[6px] w-[6px] rounded-full"
              style={{
                background: 'currentColor',
                color: tone === 'ok' ? 'var(--color-ok)' : tone === 'warn' ? 'var(--color-warn)' : 'var(--color-ink-mute)',
                boxShadow: '0 0 8px currentColor',
              }}
            />
            <span className="text-[12.5px] text-ink-dim">{text}</span>
          </span>
          <span className="hidden font-mono text-[10.5px] tracking-[0.06em] text-ink-mute md:block">
            v{version}
          </span>
          <ThemeToggle theme={theme} onToggle={onToggleTheme} />
          <UserChip route={route} onNavigate={onNavigate} />
        </div>
      </div>
    </header>
  )
}
