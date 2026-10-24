import { useCallback, useEffect, useMemo, useState } from 'react'

import { Badge, Button, Field, Panel, SectionTitle } from '../components/ui'
import { fetchHistory, historyCoverUrl, humanDuration, type HistoryItem } from '../lib/api'
import {
  AVATAR_GLYPHS,
  AVATAR_TONES,
  avatarOf,
  fetchStats,
  logout,
  saveProfile,
  useAuth,
  type ProfileStats,
} from '../lib/auth'

/** 作品封面。没有封面时 `run_cover` 返回 404，`<img>` 会渲染成裂图——
 * 必须自己兜底，否则一排作品卡全是破图标。 */
function Cover({ runId }: { runId: string }) {
  const [broken, setBroken] = useState(false)
  if (broken) {
    return (
      <div className="grid h-full place-items-center text-[11.5px] text-ink-mute">
        没有封面
      </div>
    )
  }
  return (
    <img
      src={historyCoverUrl(runId)}
      alt=""
      loading="lazy"
      onError={() => setBroken(true)}
      className="h-full w-full object-cover"
    />
  )
}

/** 个人中心：资料 + 统计 + 我的作品。 */
export function ProfilePage({ onNavigate }: { onNavigate: (route: string) => void }) {
  const { user, ready } = useAuth()
  const [stats, setStats] = useState<ProfileStats | null>(null)
  const [items, setItems] = useState<HistoryItem[]>([])
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const [nickname, setNickname] = useState('')
  const [bio, setBio] = useState('')
  const [glyph, setGlyph] = useState('')
  const [tone, setTone] = useState(0)

  const load = useCallback(async () => {
    if (!user) return
    try {
      const [s, list] = await Promise.all([fetchStats(), fetchHistory(6, true)])
      setStats(s)
      setItems(list)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    }
  }, [user])

  useEffect(() => {
    void load()
  }, [load])

  // 进入编辑时把当前值灌进表单（放在 effect 里，避免 user 更新后表单还是旧值）
  useEffect(() => {
    if (!user) return
    setNickname(user.nickname)
    setBio(user.bio)
    const raw = (user.avatar || '').trim()
    if (raw) {
      const [g, idx] = raw.split('|')
      setGlyph(g || '')
      setTone(Number(idx) || 0)
    }
  }, [user, editing])

  const avatar = useMemo(() => avatarOf(user), [user])

  if (!ready) {
    return <div className="mx-auto max-w-[1340px] px-6 py-16 text-center text-[13px] text-ink-mute">正在读取登录状态…</div>
  }

  if (!user) {
    return (
      <div className="mx-auto flex min-h-[calc(100vh-180px)] max-w-[1340px] items-center justify-center px-6">
        <Panel className="w-full max-w-[400px] p-8 text-center">
          <div
            className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-2xl text-[20px] font-semibold text-white"
            style={{ background: AVATAR_TONES[0], boxShadow: 'inset 0 1px 0 rgba(255,255,255,.35)' }}
          >
            言
          </div>
          <h2 className="font-display text-[17px] font-semibold text-ink">还没登录</h2>
          <p className="mt-2 text-[12.5px] leading-relaxed text-ink-mute">
            登录后才能看到「我的作品」和使用统计。
            <br />
            成片墙里账号体系上线前的作品仍然公开可见。
          </p>
          <button
            type="button"
            onClick={() => onNavigate('login')}
            className="mt-5 h-10 w-full rounded-xl bg-brand text-[13px] font-semibold text-white shadow-[0_10px_24px_-12px_rgba(74,84,225,.75)]"
          >
            去登录 / 注册
          </button>
        </Panel>
      </div>
    )
  }

  const doSave = async () => {
    setBusy(true)
    setError('')
    try {
      await saveProfile({
        nickname: nickname.trim(),
        bio: bio.trim(),
        avatar: glyph ? `${glyph}|${tone}` : '',
      })
      setEditing(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto w-full max-w-[1340px] px-6 pb-16 pt-6">
      {/* ---------------- 资料卡 ---------------- */}
      <Panel className="p-6">
        <div className="flex flex-wrap items-start gap-5">
          <div
            className="grid h-16 w-16 shrink-0 place-items-center rounded-2xl text-[24px] font-semibold text-white"
            style={{ background: avatar.tone, boxShadow: 'inset 0 1px 0 rgba(255,255,255,.35), 0 10px 24px -12px rgba(30,38,90,.5)' }}
          >
            {avatar.glyph}
          </div>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-display text-[19px] font-semibold tracking-tight text-ink">
                {user.nickname}
              </h1>
              <Badge tone="ok">已登录</Badge>
            </div>
            <p className="mt-1 text-[12.5px] text-ink-dim">{user.email}</p>
            {user.bio ? (
              <p className="mt-2 text-[12.5px] leading-relaxed text-ink-mute">{user.bio}</p>
            ) : (
              <p className="mt-2 text-[12.5px] text-ink-mute">还没写签名</p>
            )}
            <p className="mt-2 text-[11.5px] text-ink-mute">注册于 {user.created_at}</p>
          </div>

          <div className="flex shrink-0 gap-2">
            <Button small onClick={() => setEditing((v) => !v)}>
              {editing ? '取消' : '编辑资料'}
            </Button>
            <Button
              small
              variant="danger"
              onClick={() => {
                logout()
                onNavigate('create')
              }}
            >
              退出登录
            </Button>
          </div>
        </div>

        {editing && (
          <div className="mt-6 border-t border-[var(--ring)] pt-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="昵称" hint="最多 24 字">
                <input
                  value={nickname}
                  maxLength={24}
                  onChange={(e) => setNickname(e.target.value)}
                  className="field-input w-full text-[13px]"
                />
              </Field>
              <Field label="签名" hint="最多 120 字">
                <input
                  value={bio}
                  maxLength={120}
                  onChange={(e) => setBio(e.target.value)}
                  placeholder="一句话介绍自己"
                  className="field-input w-full text-[13px]"
                />
              </Field>
            </div>

            <div className="mt-4">
              <p className="mb-2 text-[12px] font-medium text-ink-dim">头像</p>
              <p className="mb-2 text-[11px] text-ink-mute">
                暂不支持上传图片——用一个字 + 一个底色，比灰色占位好看，也不用处理剪裁。
              </p>
              <div className="flex flex-wrap gap-1.5">
                {AVATAR_GLYPHS.map((g) => (
                  <button
                    key={g}
                    type="button"
                    onClick={() => setGlyph(g)}
                    className={`h-8 w-8 rounded-lg text-[13px] transition-colors ${
                      glyph === g
                        ? 'bg-brand text-white'
                        : 'bg-[var(--ctl)] text-ink-dim hover:text-ink'
                    }`}
                  >
                    {g}
                  </button>
                ))}
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                {AVATAR_TONES.map((t, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => setTone(i)}
                    aria-label={`配色 ${i + 1}`}
                    className={`h-7 w-7 rounded-lg ring-offset-2 transition-shadow ${
                      tone === i ? 'ring-2 ring-brand' : ''
                    }`}
                    style={{ background: t }}
                  />
                ))}
              </div>
            </div>

            {error && <p className="mt-3 text-[12px] text-err">{error}</p>}

            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={doSave}
                disabled={busy}
                className="h-8 rounded-lg bg-brand px-4 text-[12.5px] font-semibold text-white disabled:opacity-45"
              >
                {busy ? '保存中…' : '保存'}
              </button>
              <Button onClick={() => setEditing(false)}>取消</Button>
            </div>
          </div>
        )}
      </Panel>

      {/* ---------------- 统计 ---------------- */}
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          { label: '作品数', value: stats ? String(stats.runs) : '—', unit: '条' },
          { label: '总时长', value: stats ? String(Math.round(stats.total_duration)) : '—', unit: '秒' },
          { label: '近 7 天', value: stats ? String(stats.week_runs) : '—', unit: '条' },
          { label: '占用空间', value: stats ? String(Math.round(stats.total_size_mb)) : '—', unit: 'MB' },
        ].map((cell) => (
          <Panel key={cell.label} className="px-5 py-4">
            <p className="text-[11.5px] text-ink-mute">{cell.label}</p>
            <p className="mt-1 font-display text-[22px] font-semibold tracking-tight text-ink">
              {cell.value}
              <span className="ml-1 text-[12px] font-normal text-ink-mute">{cell.unit}</span>
            </p>
          </Panel>
        ))}
      </div>

      {/* ---------------- 我的作品 ---------------- */}
      <div className="mt-7">
        <SectionTitle title="我的作品" hint="只统计登录后创建的成片" />
        {items.length === 0 ? (
          <Panel className="px-5 py-8 text-center">
            <p className="text-[13px] text-ink-mute">还没有归属你的作品</p>
            <button
              type="button"
              onClick={() => onNavigate('create')}
              className="mt-3 h-8 rounded-lg bg-brand px-4 text-[12.5px] font-semibold text-white"
            >
              去创作台
            </button>
          </Panel>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((item) => (
              <Panel key={item.run_id} className="overflow-hidden p-0">
                <button
                  type="button"
                  onClick={() => onNavigate('history')}
                  className="block w-full text-left"
                >
                  <div className="relative aspect-video w-full overflow-hidden bg-[var(--sunk)]">
                    {item.has_video ? (
                      <Cover runId={item.run_id} />
                    ) : (
                      <div className="grid h-full place-items-center text-[11.5px] text-ink-mute">
                        没有封面
                      </div>
                    )}
                  </div>
                  <div className="px-4 py-3">
                    <p className="truncate text-[13px] font-medium text-ink">{item.title}</p>
                    <p className="mt-1 text-[11.5px] text-ink-mute">
                      {humanDuration(item.duration)} · {item.aspect} · {item.shots} 镜
                    </p>
                  </div>
                </button>
              </Panel>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
