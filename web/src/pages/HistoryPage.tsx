import { useEffect, useMemo, useRef, useState } from 'react'

import {
  deleteRun,
  fetchHistory,
  fetchRunDetail,
  fetchSubtitle,
  historyCoverUrl,
  historyVideoUrl,
  revealRun,
  type HistoryItem,
  type RunDetail,
} from '../lib/api'
import { Badge, Button, Panel } from '../components/ui'

const STATUS: Record<string, { tone: 'ok' | 'warn' | 'bad'; label: string }> = {
  ok: { tone: 'ok', label: '完成' },
  canceled: { tone: 'warn', label: '中断' },
  failed: { tone: 'bad', label: '失败' },
}

function statusOf(item: HistoryItem) {
  return STATUS[item.status] ?? STATUS.ok
}

function segColor(status: string): string {
  if (status === 'approved') return 'var(--color-brand)'
  if (status === 'rejected') return 'color-mix(in srgb, var(--color-err) 55%, transparent)'
  return 'color-mix(in srgb, var(--color-ink-mute) 28%, transparent)'
}

function toSeconds(stamp: string): number {
  const [hh, mm, ss] = stamp.split(':').map((part) => Number(part.replace(',', '.')) || 0)
  return hh * 3600 + mm * 60 + ss
}

function parseSrt(raw: string): { t: number; time: string; text: string }[] {
  return raw
    .replace(/\r/g, '')
    .split(/\n{2,}/)
    .map((block) => block.split('\n').filter(Boolean))
    .filter((lines) => lines.length >= 2)
    .map((lines) => {
      const timeLine = lines.find((line) => line.includes('-->')) ?? ''
      const start = (timeLine.split('-->')[0] ?? '').trim().slice(0, 8)
      const text = lines.filter((line) => !line.includes('-->') && !/^\d+$/.test(line)).join(' ')
      return { t: toSeconds(start), time: start, text }
    })
    .sort((a, b) => a.t - b.t)
}

export function HistoryPage({ onNavigate }: { onNavigate: (next: string) => void }) {
  const [items, setItems] = useState<HistoryItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('全部')
  const [aspect, setAspect] = useState('全部')
  const [openId, setOpenId] = useState('')
  const [previewId, setPreviewId] = useState('')

  const reload = () => {
    setLoading(true)
    fetchHistory(80)
      .then((list) => {
        setItems(list)
        setError('')
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : '读取历史失败'))
      .finally(() => setLoading(false))
  }

  useEffect(reload, [])

  const aspects = useMemo(() => {
    const set = new Set(items.map((item) => item.aspect).filter(Boolean))
    return ['全部', ...Array.from(set)]
  }, [items])

  const counts = useMemo(
    () => ({
      all: items.length,
      ok: items.filter((i) => i.status === 'ok').length,
      canceled: items.filter((i) => i.status === 'canceled').length,
      failed: items.filter((i) => i.status === 'failed').length,
    }),
    [items],
  )

  const visible = items.filter(
    (item) =>
      (status === '全部' || item.status === status) && (aspect === '全部' || item.aspect === aspect),
  )
  const featured = items.slice(0, 5)

  return (
    <div className="mx-auto w-full max-w-[1340px] px-6 pt-7 pb-16">
      <div className="mb-5 flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">成片墙</h2>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">
            每次运行的成片都留在这里。点开可以播放、跟着分镜走、看字幕。
          </p>
        </div>
        <Button small onClick={reload} disabled={loading}>
          刷新
        </Button>
      </div>

      {error && (
        <Panel className="mb-4 px-4 py-3">
          <p className="text-[12.5px] text-err">{error}</p>
        </Panel>
      )}

      {loading && items.length === 0 && (
        <p className="py-10 text-center text-[12.5px] text-ink-mute">正在读取 runs/…</p>
      )}

      {!loading && items.length === 0 && !error && (
        <Panel className="flex flex-col items-center gap-4 px-6 py-16 text-center">
          <svg width="120" height="96" viewBox="0 0 180 140" fill="none" aria-hidden="true" className="text-ink-mute">
            <circle cx="76" cy="62" r="34" stroke="currentColor" strokeWidth="1.4" />
            <circle cx="76" cy="62" r="20" stroke="currentColor" strokeWidth="1.4" />
            <circle cx="76" cy="62" r="5" stroke="currentColor" strokeWidth="1.4" />
            <path d="M104 78c14 10 22 20 22 30" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            <path d="M126 108h22M126 108l-6 10h34l-6-10" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
            <circle cx="120" cy="112" r="2.6" fill="var(--color-brand)" />
          </svg>
          <div>
            <p className="text-[14px] font-medium">还没有成片</p>
            <p className="mt-1 text-[12.5px] text-ink-mute">跑一条试试，跑完就会出现在这里</p>
          </div>
          <Button variant="primary" onClick={() => onNavigate('create')}>
            去创作台
          </Button>
        </Panel>
      )}

      {featured.length >= 3 && (
        <div className="acc mb-4">
          {featured.map((item, index) => (
            <div
              key={item.run_id}
              className="acc-item"
              role="button"
              tabIndex={0}
              onClick={() => setOpenId(item.run_id)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') setOpenId(item.run_id)
              }}
            >
              <img src={historyCoverUrl(item.run_id)} alt="" loading="lazy" />
              <div className="acc-scrim" />
              <span className="acc-idx">{String(index + 1).padStart(2, '0')}</span>
              <div className="acc-cap">
                <p className="text-[14px] font-semibold text-white">{item.title}</p>
                <p className="mt-0.5 text-[11.5px] text-white/70">
                  {item.duration}s · {item.aspect} · {item.shots} 镜
                </p>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        {[
          ['全部', `全部 ${counts.all}`],
          ['ok', `完成 ${counts.ok}`],
          ['canceled', `中断 ${counts.canceled}`],
          ['failed', `失败 ${counts.failed}`],
        ].map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => setStatus(value)}
            className="glass-chip px-3 py-1 text-[12.5px] transition-colors"
            style={
              status === value
                ? {
                    color: 'var(--color-brand)',
                    borderColor: 'color-mix(in srgb, var(--color-brand) 34%, transparent)',
                  }
                : { color: 'var(--color-ink-dim)' }
            }
          >
            {label}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-[var(--ring)]" />
        {aspects.map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setAspect(value)}
            className="glass-chip px-3 py-1 text-[12.5px] transition-colors"
            style={
              aspect === value
                ? {
                    color: 'var(--color-brand)',
                    borderColor: 'color-mix(in srgb, var(--color-brand) 34%, transparent)',
                  }
                : { color: 'var(--color-ink-dim)' }
            }
          >
            {value === '全部' ? '全部画幅' : value}
          </button>
        ))}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {visible.map((item) => (
          <RunCard
            key={item.run_id}
            item={item}
            previewing={previewId === item.run_id}
            onPreview={setPreviewId}
            onOpen={() => setOpenId(item.run_id)}
          />
        ))}
      </div>

      {openId && (
        <RunDrawer
          runId={openId}
          onClose={() => setOpenId('')}
          onDeleted={() => {
            setOpenId('')
            reload()
          }}
        />
      )}
    </div>
  )
}

function RunCard({
  item,
  previewing,
  onPreview,
  onOpen,
}: {
  item: HistoryItem
  previewing: boolean
  onPreview: (id: string) => void
  onOpen: () => void
}) {
  const timer = useRef<number | undefined>(undefined)
  const status = statusOf(item)
  const tone =
    status.tone === 'ok' ? 'var(--color-ok)' : status.tone === 'warn' ? 'var(--color-warn)' : 'var(--color-err)'

  const enter = () => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => onPreview(item.run_id), 600)
  }

  const leave = () => {
    window.clearTimeout(timer.current)
    if (previewing) onPreview('')
  }

  useEffect(() => () => window.clearTimeout(timer.current), [])

  return (
    <div className="rcard" onClick={onOpen} onMouseEnter={enter} onMouseLeave={leave}>
      <div className="rcard-cover">
        <img src={historyCoverUrl(item.run_id)} alt="" loading="lazy" />
        {previewing && item.has_video && (
          <video
            className="rcard-preview"
            data-on={previewing}
            src={historyVideoUrl(item.run_id)}
            autoPlay
            muted
            loop
            playsInline
            preload="none"
          />
        )}
        <span className="corner-badge left-2 text-white/80">{item.aspect || '—'}</span>
        <span className="corner-badge right-2" style={{ color: tone }}>
          {status.label}
        </span>
        <span className="rcard-play">
          <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
            <path d="M0 0l10 6-10 6z" fill="#fff" />
          </svg>
        </span>
      </div>
      <div className="px-3 py-2.5">
        <p className="truncate text-[13.5px] font-semibold">{item.title}</p>
        <p className="mt-0.5 text-[11.5px] text-ink-mute">
          {item.duration}s · {item.size_mb} MB · {item.created}
        </p>
        {item.tags.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {item.tags.map((tag) => (
              <span key={tag} className="tag-chip">
                {tag}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function RunDrawer({
  runId,
  onClose,
  onDeleted,
}: {
  runId: string
  onClose: () => void
  onDeleted: () => void
}) {
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [subtitle, setSubtitle] = useState<{ t: number; time: string; text: string }[]>([])
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [now, setNow] = useState(0)
  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    fetchRunDetail(runId)
      .then(setDetail)
      .catch((err: unknown) => setMessage(err instanceof Error ? err.message : '读取失败'))
    fetchSubtitle(runId)
      .then((raw) => setSubtitle(parseSrt(raw)))
      .catch(() => setSubtitle([]))
  }, [runId])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const total = detail?.shots.reduce((sum, shot) => sum + shot.duration, 0) || detail?.duration || 1
  const vertical = detail?.aspect === '9:16'
  const activeLine = subtitle.reduce((last, line, index) => (line.t <= now + 0.25 ? index : last), -1)

  const seek = (seconds: number) => {
    const video = videoRef.current
    if (!video) return
    video.currentTime = Math.max(0, seconds)
    void video.play().catch(() => undefined)
  }

  const remove = async () => {
    setBusy(true)
    try {
      await deleteRun(runId)
      onDeleted()
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : '删除失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="drawer" onClick={(event) => event.stopPropagation()}>
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <p className="text-[17px] font-semibold">{detail?.title ?? runId}</p>
            <p className="mt-0.5 text-[11.5px] text-ink-mute">{detail?.created}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-ink-mute transition-colors hover:text-ink"
            aria-label="关闭"
          >
            <svg width="13" height="13" viewBox="0 0 12 12" aria-hidden="true">
              <path d="M1 1l10 10M11 1L1 11" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        {detail?.has_video ? (
          <div className={`device ${vertical ? 'phone' : ''}`}>
            {!vertical && (
              <div className="device-bar">
                <i />
                <i />
                <i />
              </div>
            )}
            <div className={`device-vid ${vertical ? 'mx-auto aspect-[9/16] w-[236px]' : 'aspect-video'}`}>
              <video
                ref={videoRef}
                key={runId}
                src={historyVideoUrl(runId)}
                controls
                playsInline
                className="h-full w-full object-contain"
                onTimeUpdate={(event) => setNow(event.currentTarget.currentTime)}
              />
            </div>
          </div>
        ) : (
          <div className="glass-sunk grid h-40 place-items-center text-[12.5px] text-ink-mute">这次运行没有成片</div>
        )}

        <div className="mt-5">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[12px] text-ink-mute">分镜</span>
            <span className="font-mono text-[10.5px] text-ink-mute">
              {detail?.shots.length ?? 0} 镜 · 点一段跳到那里
            </span>
          </div>
          <div className="tl">
            {(detail?.shots ?? []).map((shot) => (
              <div
                key={shot.id}
                className="tl-seg"
                style={{ width: `${(shot.duration / total) * 100}%`, background: segColor(shot.status) }}
                title={`镜 ${shot.id} · ${shot.duration}s · ${shot.status}`}
                onClick={() => seek(shot.start)}
              />
            ))}
            <span className="tl-playhead" style={{ left: `${Math.min(100, (now / total) * 100)}%` }} />
          </div>
          <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
            {(detail?.shots ?? []).map((shot) => (
              <span key={shot.id} className="text-[11px] text-ink-mute">
                镜 {shot.id}
                <span className="ml-1 text-ink-dim">{shot.attempts ? `重试 ${shot.attempts}` : '一次过'}</span>
              </span>
            ))}
          </div>
        </div>

        {subtitle.length > 0 && (
          <div className="mt-5">
            <p className="mb-2 text-[12px] text-ink-mute">字幕</p>
            <div className="space-y-0.5">
              {subtitle.map((line, index) => (
                <div key={`${line.time}-${index}`} className="sub-row" data-now={index === activeLine}>
                  <span className="shrink-0 font-mono text-[11px] text-ink-mute">{line.time}</span>
                  <span className="min-w-0 text-ink-dim">{line.text}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {message && <p className="mt-4 text-[12px] text-err">{message}</p>}

        <div className="mt-6 flex items-center justify-between gap-3 border-t border-[var(--ring)] pt-4">
          <span className="text-[11.5px] text-ink-mute">
            {detail ? `${detail.aspect} · ${detail.duration}s · ${detail.size_mb} MB` : '—'}
          </span>
          <div className="flex gap-2">
            <Button
              small
              onClick={() => {
                revealRun(runId).catch((err: unknown) =>
                  setMessage(err instanceof Error ? err.message : '打不开目录'),
                )
              }}
            >
              打开目录
            </Button>
            <Button small variant="danger" disabled={busy} onClick={() => void remove()}>
              删除
            </Button>
          </div>
        </div>

        <div className="mt-3 flex items-center gap-2">
          <Badge tone="idle">Esc 关闭</Badge>
          <span className="text-[11px] text-ink-mute">删除会移到 runs/_trash，可以捞回来</span>
        </div>
      </div>
    </div>
  )
}
