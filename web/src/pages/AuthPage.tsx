import { useState } from 'react'

import { Field, Panel } from '../components/ui'
import { login, register } from '../lib/auth'

/** 登录 / 注册。两个模式共用一个表单，少写一半状态。 */
export function AuthPage({
  onDone,
  onSkip,
  initialMode = 'login',
}: {
  onDone: () => void
  onSkip: () => void
  initialMode?: 'login' | 'register'
}) {
  const [mode, setMode] = useState<'login' | 'register'>(initialMode)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const isRegister = mode === 'register'

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    setError('')
    setBusy(true)
    try {
      if (isRegister) await register(email.trim(), password, nickname.trim())
      else await login(email.trim(), password)
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : '请求失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto flex min-h-[calc(100vh-160px)] w-full max-w-[1340px] items-center justify-center px-6 py-10">
      <Panel className="w-full max-w-[420px] p-7">
        <div className="mb-6 flex items-baseline gap-3">
          <h1 className="font-display text-[20px] font-semibold tracking-tight text-ink">
            {isRegister ? '创建账号' : '欢迎回来'}
          </h1>
          <span className="text-[12px] text-ink-mute">一言片场</span>
        </div>

        {/* 模式切换：视觉上是两个 tab，实际是两个按钮改 state */}
        <div className="mb-6 grid grid-cols-2 gap-1 rounded-xl border border-[var(--ctl-edge)] bg-[var(--sunk)] p-1">
          {(['login', 'register'] as const).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => {
                setMode(key)
                setError('')
              }}
              className={`h-8 rounded-lg text-[12.5px] font-medium transition-colors ${
                mode === key
                  ? 'bg-[var(--ctl)] text-ink shadow-sm'
                  : 'text-ink-mute hover:text-ink-dim'
              }`}
            >
              {key === 'login' ? '登录' : '注册'}
            </button>
          ))}
        </div>

        <form onSubmit={submit} className="space-y-4">
          <Field label="邮箱" hint="用来登录">
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              className="field-input w-full text-[13px]"
            />
          </Field>

          {isRegister && (
            <Field label="昵称" hint="可留空，默认取邮箱前缀">
              <input
                type="text"
                maxLength={24}
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
                placeholder="比如：风云"
                className="field-input w-full text-[13px]"
              />
            </Field>
          )}

          <Field label="密码" hint={isRegister ? '至少 8 位' : undefined}>
            <input
              type="password"
              required
              minLength={isRegister ? 8 : 1}
              autoComplete={isRegister ? 'new-password' : 'current-password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              className="field-input w-full text-[13px]"
            />
          </Field>

          {error && (
            <div className="rounded-xl border border-err/30 bg-err/[0.07] px-3 py-2">
              <p className="text-[12px] text-err">{error}</p>
            </div>
          )}

          {/* 不用组件库的 primary：它靠两层 mix-blend-mode 做流动光效，
              某些相位下文字会被 blend 吃掉、对比度掉到看不清。
              这是登录的唯一主按钮，可读性优先于效果。 */}
          <button
            type="submit"
            disabled={busy}
            className="h-10 w-full rounded-xl bg-brand text-[13px] font-semibold text-white shadow-[0_10px_24px_-12px_rgba(74,84,225,.75)] transition-opacity hover:opacity-95 disabled:opacity-45"
          >
            {busy ? '处理中…' : isRegister ? '注册并登录' : '登录'}
          </button>
        </form>

        <p className="mt-5 text-center text-[11.5px] leading-relaxed text-ink-mute">
          账号只存在本机的 <code className="rounded bg-[var(--sunk)] px-1">work/yiyan.db</code>，
          不上传任何服务器。
          <br />
          不想注册？
          <button
            type="button"
            onClick={onSkip}
            className="ml-1 text-brand underline-offset-2 hover:underline"
          >
            以游客身份使用
          </button>
        </p>
      </Panel>
    </div>
  )
}
