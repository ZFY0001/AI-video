import { useEffect, useMemo, useRef, useState } from 'react'

import {
  brandLogoUrl,
  clearBrand,
  deleteBrandLogo,
  deleteRun,
  exportFileUrl,
  fetchBrand,
  fetchExports,
  fetchHistory,
  fetchReview,
  fetchRunDetail,
  fetchSubtitle,
  historyCoverUrl,
  historyLrcUrl,
  historyStillUrl,
  historySubtitleUrl,
  historyVideoUrl,
  revealRun,
  saveBrand,
  startBatchExports,
  startExport,
  submitReview,
  undoBrand,
  uploadBrandLogo,
  type BrandConfig,
  type BrandPosition,
  type BrandState,
  type ExportItem,
  type ExportPreset,
  type HistoryItem,
  type ReviewPanel,
  type ReviewShot,
  type RunDetail,
} from '../lib/api'
import { Badge, Button, Panel } from '../components/ui'
import { setEditGuard } from '../lib/editGuard'

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

const REVIEW_MODE: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'idle' }> = {
  vlm: { label: '真实视觉评审', tone: 'ok' },
  simulated: { label: '模拟评审', tone: 'warn' },
  technical_failed: { label: '技术检查失败', tone: 'bad' },
}

const BRAND_POSITIONS: { value: BrandPosition; label: string }[] = [
  { value: 'bottom-right', label: '右下' },
  { value: 'bottom-left', label: '左下' },
  { value: 'top-right', label: '右上' },
  { value: 'top-left', label: '左上' },
]

/** 预设只填入表单，点保存后才真正生效——这样一次误点可以直接撤销。 */
const BRAND_PRESETS: {
  id: string
  label: string
  hint: string
  patch: Partial<BrandConfig>
}[] = [
  {
    id: 'minimal',
    label: '极简水印',
    hint: '品牌名 + logo 放角落，不加片头片尾',
    patch: { name: '一言片场', intro_text: '', outro_text: '' },
  },
  {
    id: 'opening',
    label: '片头片尾',
    hint: '开头与结尾各叠一条字卡，时长不变',
    patch: {
      intro_text: '出品 · 一言片场',
      outro_text: '感谢观看',
      intro_seconds: 2,
      outro_seconds: 2,
    },
  },
  {
    id: 'clean',
    label: '清空文字',
    hint: '清空品牌名与片头片尾；logo 需要单独点「移除 logo」',
    patch: { name: '', intro_text: '', outro_text: '' },
  },
]

type ExportAspect = '16:9' | '9:16' | '1:1'
type ExportMode = 'fit' | 'crop'

const EXPORT_COMBOS: { aspect: ExportAspect; mode: ExportMode; label: string }[] = [
  { aspect: '9:16', mode: 'fit', label: '9:16 适配' },
  { aspect: '9:16', mode: 'crop', label: '9:16 裁切' },
  { aspect: '1:1', mode: 'fit', label: '1:1 适配' },
  { aspect: '1:1', mode: 'crop', label: '1:1 裁切' },
]

function reviewModeOf(shot: ReviewShot) {
  return REVIEW_MODE[shot.review_mode] ?? { label: '评审未记录', tone: 'idle' as const }
}

/** 技术证据一行。没记录就直说没记录，不能拿 0 当作"检查通过"。 */
function techLine(shot: ReviewShot): string {
  const tech = shot.technical
  if (!tech.recorded) return '技术证据未记录（旧版本运行产物）'
  if (!tech.ok) return `技术检查失败：${tech.reason || '图片不可用'}`
  const sha = shot.image_sha256 ? shot.image_sha256.slice(0, 8) : '—'
  return `${tech.width}×${tech.height} · 亮度 ${tech.mean.toFixed(1)} · 对比 ${tech.contrast.toFixed(1)} · sha ${sha}`
}

/** 单镜留存画面 + 可选安全区。
 *
 * 90%/80% 是广播/平台通用的动作安全区与字幕安全区：主体动作超出外框、
 * 文字超出内框，在最常见的裁切与手机全面屏上就可能被切掉。这里只画参考线，
 * 不修改画面。
 */
function ShotStill({ runId, shotId, safeArea, source }: {
  runId: string
  shotId: number
  safeArea: boolean
  /** 出图来源。离线后端重画的是无字占位背景，和评审看的那张不是同一张。 */
  source?: string
}) {
  const [failed, setFailed] = useState(false)
  const offline = source === 'offline'
  if (failed) {
    return <p className="mt-1.5 text-[10.5px] text-ink-mute">这一镜没有留存画面</p>
  }
  return (
    <>
    <div className="relative mt-2 inline-block max-w-full overflow-hidden rounded-md border border-[var(--ring)] bg-black/20">
      <img
        src={historyStillUrl(runId, shotId)}
        alt={`镜 ${shotId} 画面`}
        loading="lazy"
        className="block max-h-[190px] w-auto max-w-full"
        onError={() => setFailed(true)}
      />
      {safeArea && (
        <>
          <div className="pointer-events-none absolute inset-[5%] rounded-[2px] border border-dashed border-warn/80" />
          <div className="pointer-events-none absolute inset-[10%] rounded-[2px] border border-dashed border-white/60" />
          <span className="pointer-events-none absolute bottom-1 left-1 rounded bg-black/55 px-1 py-0.5 font-mono text-[9px] text-white/85">
            外 90% · 内 80%
          </span>
        </>
      )}
    </div>
    {offline && (
      <p className="mt-1 text-[10px] leading-relaxed text-ink-mute">
        离线占位背景（已抹字，非评审证据图）：离线引擎看不到这张候选图，画面以真正出图的版本为准。
      </p>
    )}
    </>
  )
}

function toSeconds(stamp: string): number {
  const [hh, mm, ss] = stamp.split(':').map((part) => Number(part.replace(',', '.')) || 0)
  return hh * 3600 + mm * 60 + ss
}

function parseSrt(raw: string): { t: number; time: string; text: string }[] {
  return raw
    .replace(/\r/g, '')
    // 带 BOM 时第一行序号是 "\uFEFF1"，^\d+$ 认不出来，会被当成正文拼进第一句。
    .replace(/\uFEFF/g, '')
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
  const [review, setReview] = useState<ReviewPanel | null>(null)
  const [reviewNotes, setReviewNotes] = useState<Record<number, string>>({})
  const [reviewBusy, setReviewBusy] = useState(0)
  const [reviewMsg, setReviewMsg] = useState('')
  /** 是否在留存画面上叠加 90%/80% 安全区参考线。 */
  const [showSafe, setShowSafe] = useState(false)
  const [exports, setExports] = useState<ExportItem[]>([])
  const [exportBusy, setExportBusy] = useState('')
  const [exportMsg, setExportMsg] = useState('')
  const exportPoll = useRef(0)
  const [selectedSpecs, setSelectedSpecs] = useState<string[]>([])
  const [exportPreset, setExportPreset] = useState('standard')
  const [presets, setPresets] = useState<ExportPreset[]>([])
  const [brand, setBrand] = useState<BrandState | null>(null)
  const [brandForm, setBrandForm] = useState<BrandConfig | null>(null)
  const [brandBusy, setBrandBusy] = useState('')
  const [brandMsg, setBrandMsg] = useState('')
  const brandLogoInput = useRef<HTMLInputElement>(null)
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
    // 还没拍过的项目没有可复核分镜，这时安静地不显示面板即可。
    fetchReview(runId)
      .then((panel) => {
        setReview(panel)
        setReviewNotes(
          Object.fromEntries(panel.shots.map((shot) => [shot.id, shot.manual?.note ?? ''])),
        )
      })
      .catch(() => setReview(null))
  }, [runId])

  useEffect(() => {
    let alive = true
    fetchExports(runId)
      .then((data) => {
        if (!alive) return
        setExports(data.items)
        setPresets(data.presets ?? [])
        // 另一个标签页起的导出也要能在这里看到进度。
        if (data.items.some((item) => item.status === 'running')) pollExports()
      })
      .catch(() => {
        if (alive) setExports([])
      })
    return () => {
      alive = false
    }
  }, [runId])

  useEffect(() => {
    let alive = true
    fetchBrand(runId)
      .then((state) => {
        if (!alive) return
        setBrand(state)
        setBrandForm(state.brand)
      })
      .catch(() => {
        // 一句话成片没有拆解目录，自然也没有品牌模板；安静地不显示编辑区。
        if (alive) {
          setBrand(null)
          setBrandForm(null)
        }
      })
    return () => {
      alive = false
    }
  }, [runId])

  useEffect(() => () => window.clearTimeout(exportPoll.current), [])

  const decide = async (shot: ReviewShot, verdict: 'approved' | 'rejected' | 'clear') => {
    if (verdict === 'rejected' && !window.confirm(
      '把这一镜标记为人工驳回？它不会自动重拍，成片也不会改变；需要换画面请回项目里重拍该镜。',
    )) {
      return
    }
    setReviewBusy(shot.id)
    setReviewMsg('')
    try {
      const out = await submitReview(runId, shot.id, {
        verdict,
        note: (reviewNotes[shot.id] ?? '').trim(),
        image_sha256: shot.image_sha256,
      })
      setReview((prev) => prev
        ? {
            ...prev,
            shots: prev.shots.map((item) => (item.id === shot.id ? out.shot : item)),
            summary: out.summary,
          }
        : prev)
      setReviewMsg(out.note)
    } catch (err) {
      setReviewMsg(err instanceof Error ? err.message : '保存人工结论失败')
    } finally {
      setReviewBusy(0)
    }
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const total = detail?.shots.reduce((sum, shot) => sum + shot.duration, 0) || detail?.duration || 1

  // 质检汇总按数量展示，不能让“有一镜视觉评审”代表整片都已视觉通过。
  const reviewShots = detail?.shots ?? []
  const reviewTotal = reviewShots.length
  const modeCounts = reviewShots.reduce<Record<string, number>>((acc, shot) => {
    const key = shot.review_mode || 'unrecorded'
    acc[key] = (acc[key] ?? 0) + 1
    return acc
  }, {})
  const modeParts: string[] = []
  if (modeCounts.vlm) modeParts.push(`视觉 ${modeCounts.vlm}/${reviewTotal}`)
  if (modeCounts.simulated) modeParts.push(`模拟 ${modeCounts.simulated}/${reviewTotal}`)
  if (modeCounts.technical_failed) {
    modeParts.push(`技术失败 ${modeCounts.technical_failed}/${reviewTotal}`)
  }
  if (modeCounts.unrecorded) modeParts.push(`未记录 ${modeCounts.unrecorded}/${reviewTotal}`)
  const manualSummary = review?.summary
  const manualPart = manualSummary
    ? `人工通过 ${manualSummary.manual_approved}/${manualSummary.total}`
    : ''
  const reviewNote =
    reviewTotal === 0
      ? ''
      : `质检：${modeParts.join(' · ')}${manualPart ? ` · ${manualPart}` : ''}`
  const vertical = detail?.aspect === '9:16'
  const activeLine = subtitle.reduce((last, line, index) => (line.t <= now + 0.25 ? index : last), -1)

  const seek = (seconds: number) => {
    const video = videoRef.current
    if (!video) return
    video.currentTime = Math.max(0, seconds)
    void video.play().catch(() => undefined)
  }

  const refreshExports = async () => {
    const data = await fetchExports(runId)
    setExports(data.items)
    return data.items
  }

  /** 导出是后台任务：起完轮询到没有 running 为止，不假设一个固定耗时。 */
  const pollExports = (attempt = 0) => {
    window.clearTimeout(exportPoll.current)
    if (attempt > 120) return
    exportPoll.current = window.setTimeout(() => {
      void refreshExports()
        .then((items) => {
          if (items.some((item) => item.status === 'running')) pollExports(attempt + 1)
        })
        .catch(() => pollExports(attempt + 1))
    }, 1500)
  }

  // 品牌是独立版本域：本地改了字段但还没保存时，导出必须拦住并明确提示，
  // 不能默认等待一个尚未发出的保存请求。
  const brandDirty = Boolean(
    brandForm && brand && (
      brandForm.name.trim() !== brand.brand.name
      || brandForm.intro_text.trim() !== brand.brand.intro_text
      || brandForm.outro_text.trim() !== brand.brand.outro_text
      || brandForm.position !== brand.brand.position
      || brandForm.opacity !== brand.brand.opacity
      || brandForm.intro_seconds !== brand.brand.intro_seconds
      || brandForm.outro_seconds !== brand.brand.outro_seconds
    ),
  )

  // 保存/撤销/上传进行中同样不能导出：那一刻母版正在被重写，
  // 拿到的可能是旧品牌层，甚至是一个还没落盘完的文件。
  const brandLocked = brandDirty || Boolean(brandBusy)

  /** 可导出组合。
   *
   * 原片画幅默认不可选（重排是空操作）。但一旦叠上品牌，同画幅就是一次真实
   * 变换——16:9 原片加角标同样是用户要的东西，不能因为"画幅没变"被挡掉。
   */
  /** 当前预设；平台预设固定画幅（抖音/视频号=9:16，B站=16:9）。 */
  const activePreset = presets.find((item) => item.id === exportPreset)
  const presetAspect = activePreset?.aspect ?? ''

  const exportCombos = useMemo(() => {
    const brandActive = Boolean(brand?.applied)
    const sameAspect = detail?.aspect as ExportAspect | undefined
    const extra = brandActive && sameAspect
      ? [{ aspect: sameAspect, mode: 'fit' as ExportMode, label: `${sameAspect} 加品牌` }]
      : []
    const base = brandActive
      ? EXPORT_COMBOS.filter((combo) => combo.aspect !== sameAspect)
      : EXPORT_COMBOS
    const all = [...extra, ...base]
    // 预设固定画幅时不再展示别的画幅：否则最省事的操作是先选、再吃一个 422。
    return presetAspect ? all.filter((combo) => combo.aspect === presetAspect) : all
  }, [brand?.applied, detail?.aspect, presetAspect])

  // 换预设后已选规格可能属于别的画幅，留着只会让批量整批 422。
  useEffect(() => {
    setSelectedSpecs([])
  }, [exportPreset])

  const brandDirtyRef = useRef(false)
  brandDirtyRef.current = brandDirty
  useEffect(() => {
    setEditGuard(() => (brandDirtyRef.current ? '品牌模板有未保存的修改' : ''))
    return () => setEditGuard(null)
  }, [])

  const launchExport = async (aspect: ExportAspect, mode: ExportMode) => {
    if (brandLocked) {
      setExportMsg(
        brandDirty
          ? '品牌模板有未保存的修改，先点「保存模板」再导出。'
          : '品牌模板正在写入，等它落盘后再导出。',
      )
      return
    }
    const preset = exportPreset
    const key = `${aspect}-${mode}-${preset}`
    setExportBusy(key)
    setExportMsg('')
    try {
      const item = await startExport(runId, aspect, mode, preset)
      setExports((prev) => [...prev.filter((old) => old.export_id !== item.export_id), item])
      if (item.status === 'running') {
        setExportMsg('导出进行中，可以继续看别的；完成后这里会出现下载链接。')
        pollExports()
      } else if (item.status === 'failed') {
        setExportMsg(item.error || '导出失败')
      } else {
        setExportMsg('导出已就绪。')
      }
    } catch (err) {
      setExportMsg(err instanceof Error ? err.message : '导出失败')
    } finally {
      setExportBusy('')
    }
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

  const applyBrandState = (state: BrandState) => {
    setBrand(state)
    setBrandForm(state.brand)
  }

  const patchBrand = (patch: Partial<BrandConfig>) => {
    setBrandForm((prev) => (prev ? { ...prev, ...patch } : prev))
  }

  /** 品牌变更会让所有旧导出失效；重新拉一次，让用户看见"需要重导"。 */
  const afterBrandChange = async (note: string) => {
    setBrandMsg(note)
    try {
      const data = await fetchExports(runId)
      setExports(data.items)
    } catch {
      setExports([])
    }
  }

  const saveBrandForm = async () => {
    if (!brandForm) return
    setBrandBusy('save')
    setBrandMsg('')
    try {
      const state = await saveBrand(runId, {
        name: brandForm.name.trim(),
        intro_text: brandForm.intro_text.trim(),
        outro_text: brandForm.outro_text.trim(),
        position: brandForm.position,
        opacity: brandForm.opacity,
        intro_seconds: brandForm.intro_seconds,
        outro_seconds: brandForm.outro_seconds,
        expected_version: brand?.brand.version,
      })
      applyBrandState(state)
      await afterBrandChange('品牌模板已保存。已导出的旧规格需要重新导出。')
    } catch (err) {
      setBrandMsg(err instanceof Error ? err.message : '保存品牌模板失败')
    } finally {
      setBrandBusy('')
    }
  }

  const undoBrandChange = async () => {
    setBrandBusy('undo')
    setBrandMsg('')
    try {
      const state = await undoBrand(runId)
      applyBrandState(state)
      await afterBrandChange(state.note || '已撤销到上一版品牌模板。')
    } catch (err) {
      setBrandMsg(err instanceof Error ? err.message : '撤销失败')
    } finally {
      setBrandBusy('')
    }
  }

  const clearBrandTemplate = async () => {
    if (!window.confirm('清除品牌模板？之后的导出会不带水印/片头片尾；已导出的文件不会自动删除。')) {
      return
    }
    setBrandBusy('clear')
    setBrandMsg('')
    try {
      const state = await clearBrand(runId)
      applyBrandState(state)
      await afterBrandChange(state.note || '品牌模板已清除。')
    } catch (err) {
      setBrandMsg(err instanceof Error ? err.message : '清除失败')
    } finally {
      setBrandBusy('')
    }
  }

  const pickBrandLogo = async (file: File | undefined) => {
    if (!file) return
    setBrandBusy('logo')
    setBrandMsg('')
    try {
      const state = await uploadBrandLogo(runId, file, brand?.brand.version)
      applyBrandState(state)
      await afterBrandChange('logo 已保存。已导出的旧规格需要重新导出。')
    } catch (err) {
      setBrandMsg(err instanceof Error ? err.message : '上传 logo 失败')
    } finally {
      setBrandBusy('')
      if (brandLogoInput.current) brandLogoInput.current.value = ''
    }
  }

  const removeBrandLogo = async () => {
    setBrandBusy('logo')
    setBrandMsg('')
    try {
      const state = await deleteBrandLogo(runId, brand?.brand.version)
      applyBrandState(state)
      await afterBrandChange('logo 已移除。')
    } catch (err) {
      setBrandMsg(err instanceof Error ? err.message : '移除 logo 失败')
    } finally {
      setBrandBusy('')
    }
  }

  const launchBatch = async () => {
    if (selectedSpecs.length === 0) return
    if (brandLocked) {
      setExportMsg(
        brandDirty
          ? '品牌模板有未保存的修改，先点「保存模板」再批量导出。'
          : '品牌模板正在写入，等它落盘后再批量导出。',
      )
      return
    }
    setExportBusy('batch')
    setExportMsg('')
    try {
      const targets = selectedSpecs.map((key) => {
        const [aspect, mode] = key.split('|') as [ExportAspect, ExportMode]
        return { aspect, mode, preset: exportPreset }
      })
      const out = await startBatchExports(runId, targets)
      setExports((prev) => {
        const merged = new Map(prev.map((item) => [item.export_id, item]))
        out.items.forEach((item) => merged.set(item.export_id, item))
        return [...merged.values()]
      })
      setSelectedSpecs([])
      const skipped = out.skipped.length > 0
        ? `；跳过 ${out.skipped.map((item) => `${item.aspect} ${item.mode}（${item.reason}）`).join('、')}`
        : ''
      setExportMsg(
        `已提交 ${out.items.length} 个规格，按顺序渲染${out.branded ? '，已套用品牌层' : ''}${skipped}。`,
      )
      if (out.items.some((item) => item.status === 'running')) pollExports()
    } catch (err) {
      setExportMsg(err instanceof Error ? err.message : '批量导出失败')
    } finally {
      setExportBusy('')
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="drawer" onClick={(event) => event.stopPropagation()}>
        <div className="mb-3 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <p className="min-w-0 truncate text-[17px] font-semibold">{detail?.title ?? runId}</p>
              {detail && detail.status !== 'ok' && (
                <Badge tone={STATUS[detail.status]?.tone ?? 'warn'}>
                  {detail.status === 'canceled' ? '中断 · 部分产出' : STATUS[detail.status]?.label ?? detail.status}
                </Badge>
              )}
            </div>
            <p className="mt-0.5 text-[11.5px] text-ink-mute">{detail?.created}</p>
            {detail?.status === 'canceled' && detail.has_video && (
              <p className="mt-1 text-[11.5px] text-warn">
                这次拍摄中途停了，成片只包含已经拍完的镜头；时长与镜头数按实际产出计。
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            title="关闭（Esc）"
            className="-mr-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-ink-mute transition-colors hover:bg-[var(--ctl)] hover:text-ink"
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
          {reviewNote && (
            <p className="mt-2 text-[11px] leading-relaxed text-ink-mute">{reviewNote}</p>
          )}
        </div>

        {review && (
          <div className="mt-5">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <p className="text-[12px] text-ink-mute">质检与人工复核</p>
              <span className="font-mono text-[10.5px] text-ink-mute">
                {review.summary.total} 镜 · 待复核 {review.summary.pending}
                {review.summary.manual_approved > 0 && ` · 人工通过 ${review.summary.manual_approved}`}
                {review.summary.manual_rejected > 0 && ` · 人工驳回 ${review.summary.manual_rejected}`}
                {review.summary.stale > 0 && ` · 失效 ${review.summary.stale}`}
              </span>
              <label className="flex items-center gap-1 text-[10.5px] text-ink-mute">
                <input
                  type="checkbox"
                  checked={showSafe}
                  onChange={(event) => setShowSafe(event.target.checked)}
                />
                画面安全区 90/80%
              </label>
            </div>
            <div className="glass-sunk space-y-3 px-3.5 py-3">
              {review.shots.map((shot) => {
                const mode = reviewModeOf(shot)
                const manual = shot.manual
                return (
                  <div key={shot.id} className="border-b border-[var(--ring)] pb-3 last:border-0 last:pb-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[12px] font-medium">镜 {shot.id}</span>
                      <Badge tone={mode.tone}>{mode.label}</Badge>
                      {manual && !manual.stale && (
                        <Badge tone={manual.verdict === 'approved' ? 'ok' : 'bad'}>
                          {manual.verdict === 'approved' ? '人工通过' : '人工驳回'}
                        </Badge>
                      )}
                      {manual?.stale && <Badge tone="warn">画面已变化 · 旧结论失效</Badge>}
                      <span className="ml-auto font-mono text-[10.5px] text-ink-mute">
                        {shot.quality != null ? `规则分 ${shot.quality}/4` : '未评分'}
                      </span>
                    </div>
                    <p className="mt-1 font-mono text-[10.5px] text-ink-mute">{techLine(shot)}</p>
                    <p className="mt-0.5 text-[10.5px] text-ink-mute">
                      评审规则 {shot.review_rule || '—'} · 视觉检查 {shot.visual_check || '未记录'}
                    </p>
                    <ShotStill runId={runId} shotId={shot.id} safeArea={showSafe} source={shot.frame_source} />
                    {manual?.reviewed_at && (
                      <p className="mt-0.5 text-[10.5px] text-ink-mute">
                        人工结论 {manual.reviewed_at}
                        {manual.reviewed_by
                          ? ` · ${manual.reviewed_by === 'guest' ? '访客' : '账号'}`
                          : ''}
                      </p>
                    )}
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <input
                        className="field-input min-w-[160px] flex-1 text-[11.5px]"
                        placeholder="备注（可留空）"
                        maxLength={200}
                        value={reviewNotes[shot.id] ?? ''}
                        onChange={(event) => {
                          const value = event.target.value
                          setReviewNotes((prev) => ({ ...prev, [shot.id]: value }))
                        }}
                      />
                      <Button
                        small
                        variant="primary"
                        disabled={reviewBusy === shot.id}
                        onClick={() => void decide(shot, 'approved')}
                      >
                        人工通过
                      </Button>
                      <Button
                        small
                        variant="danger"
                        disabled={reviewBusy === shot.id}
                        onClick={() => void decide(shot, 'rejected')}
                      >
                        人工驳回
                      </Button>
                      {manual && (
                        <Button
                          small
                          disabled={reviewBusy === shot.id}
                          onClick={() => void decide(shot, 'clear')}
                        >
                          撤销
                        </Button>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-ink-mute">
              人工结论只做归档，不改变已合成画面；要换画面请回项目里单镜重拍。
              模拟评审只代表规则打分，不代表画面语义已经过检查。
            </p>
            {reviewMsg && <p className="mt-1.5 text-[11.5px] text-ink-dim">{reviewMsg}</p>}
          </div>
        )}

        {subtitle.length > 0 && (
          <div className="mt-5">
            <div className="mb-2 flex items-center justify-between gap-2">
              <p className="text-[12px] text-ink-mute">字幕</p>
              <span className="flex items-center gap-3 text-[11px] text-ink-mute">
                <a
                  className="transition-colors hover:text-ink"
                  href={historySubtitleUrl(runId)}
                  download={`${detail?.title || runId}.srt`}
                >
                  下载 SRT
                </a>
                <a
                  className="transition-colors hover:text-ink"
                  href={historyLrcUrl(runId)}
                  download={`${detail?.title || runId}.lrc`}
                >
                  下载 LRC
                </a>
              </span>
            </div>
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

        {detail?.has_video && brand && brandForm && (
          <div className="mt-5">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <p className="text-[12px] text-ink-mute">品牌模板</p>
                <Badge tone={brand.applied ? 'ok' : 'idle'}>
                  {brand.applied ? `已启用 v${brand.brand.version}` : '未启用'}
                </Badge>
              </div>
              <span className="text-[10.5px] text-ink-mute">
                只作用于导出层；不改原片，不改变时长与音轨
              </span>
            </div>
            <div className="glass-sunk space-y-2.5 px-3.5 py-3">
              <div className="flex flex-wrap items-center gap-1.5">
                {BRAND_PRESETS.map((preset) => (
                  <Button
                    key={preset.id}
                    small
                    disabled={Boolean(brandBusy)}
                    title={preset.hint}
                    onClick={() => {
                      patchBrand(preset.patch)
                      setBrandMsg('预设已填入表单，点「保存模板」后才生效。')
                    }}
                  >
                    {preset.label}
                  </Button>
                ))}
                <span className="text-[10px] text-ink-mute">预设只填表，保存后才生效</span>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                {brand.brand.logo.asset ? (
                  <img
                    src={brandLogoUrl(runId, brand.brand.logo.sha)}
                    alt="品牌 logo"
                    className="h-9 w-9 rounded object-contain"
                  />
                ) : (
                  <span className="grid h-9 w-9 place-items-center rounded border border-[var(--ring)] text-[9px] text-ink-mute">
                    无 logo
                  </span>
                )}
                <input
                  ref={brandLogoInput}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  onChange={(event) => void pickBrandLogo(event.target.files?.[0])}
                />
                <Button
                  small
                  disabled={Boolean(brandBusy)}
                  onClick={() => brandLogoInput.current?.click()}
                >
                  {brand.brand.logo.asset ? '更换 logo' : '上传 logo'}
                </Button>
                {brand.brand.logo.asset && (
                  <Button small disabled={Boolean(brandBusy)} onClick={() => void removeBrandLogo()}>
                    移除 logo
                  </Button>
                )}
                <span className="text-[10px] text-ink-mute">PNG / JPEG / WebP，至少 32×32</span>
              </div>

              <div className="grid gap-2 sm:grid-cols-2">
                <label className="space-y-1">
                  <span className="block text-[10.5px] text-ink-mute">品牌名（水印文字）</span>
                  <input
                    className="field-input w-full text-[11.5px]"
                    maxLength={40}
                    placeholder="例如：一言片场"
                    value={brandForm.name}
                    onChange={(event) => patchBrand({ name: event.target.value })}
                  />
                </label>
                <label className="space-y-1">
                  <span className="block text-[10.5px] text-ink-mute">水印位置</span>
                  <select
                    className="field-input w-full text-[11.5px]"
                    value={brandForm.position}
                    onChange={(event) =>
                      patchBrand({ position: event.target.value as BrandPosition })
                    }
                  >
                    {BRAND_POSITIONS.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="space-y-1">
                  <span className="block text-[10.5px] text-ink-mute">
                    片头字卡（{brandForm.intro_seconds.toFixed(1)}s）
                  </span>
                  <input
                    className="field-input w-full text-[11.5px]"
                    maxLength={60}
                    placeholder="留空则不叠加片头"
                    value={brandForm.intro_text}
                    onChange={(event) => patchBrand({ intro_text: event.target.value })}
                  />
                </label>
                <label className="space-y-1">
                  <span className="block text-[10.5px] text-ink-mute">
                    片尾字卡（{brandForm.outro_seconds.toFixed(1)}s）
                  </span>
                  <input
                    className="field-input w-full text-[11.5px]"
                    maxLength={60}
                    placeholder="留空则不叠加片尾"
                    value={brandForm.outro_text}
                    onChange={(event) => patchBrand({ outro_text: event.target.value })}
                  />
                </label>
                <label className="space-y-1">
                  <span className="block text-[10.5px] text-ink-mute">
                    片头时长 {brandForm.intro_seconds.toFixed(1)}s
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={8}
                    step={0.5}
                    className="w-full"
                    value={brandForm.intro_seconds}
                    onChange={(event) =>
                      patchBrand({ intro_seconds: Number(event.target.value) })
                    }
                  />
                </label>
                <label className="space-y-1">
                  <span className="block text-[10.5px] text-ink-mute">
                    片尾时长 {brandForm.outro_seconds.toFixed(1)}s
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={8}
                    step={0.5}
                    className="w-full"
                    value={brandForm.outro_seconds}
                    onChange={(event) =>
                      patchBrand({ outro_seconds: Number(event.target.value) })
                    }
                  />
                </label>
                <label className="space-y-1 sm:col-span-2">
                  <span className="block text-[10.5px] text-ink-mute">
                    不透明度 {Math.round(brandForm.opacity * 100)}%
                  </span>
                  <input
                    type="range"
                    min={0.1}
                    max={1}
                    step={0.05}
                    className="w-full"
                    value={brandForm.opacity}
                    onChange={(event) => patchBrand({ opacity: Number(event.target.value) })}
                  />
                </label>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  small
                  variant="primary"
                  disabled={Boolean(brandBusy)}
                  onClick={() => void saveBrandForm()}
                >
                  {brandBusy === 'save' ? '保存中…' : '保存模板'}
                </Button>
                <Button
                  small
                  disabled={Boolean(brandBusy) || !brand.can_undo}
                  title={brand.can_undo ? '恢复到上一版品牌配置' : '没有可撤销的版本'}
                  onClick={() => void undoBrandChange()}
                >
                  {brandBusy === 'undo' ? '撤销中…' : '撤销上一步'}
                </Button>
                <Button
                  small
                  variant="danger"
                  disabled={Boolean(brandBusy) || !brand.applied}
                  onClick={() => void clearBrandTemplate()}
                >
                  清除模板
                </Button>
                <span className="text-[10px] leading-relaxed text-ink-mute">
                  保存、上传或清除都会留档；撤销只回退品牌层，不动分镜。
                </span>
              </div>
              {brandMsg && (
                <p className="text-[10.5px] leading-relaxed text-ink-dim">{brandMsg}</p>
              )}
            </div>
          </div>
        )}

        {detail?.has_video && (
          <div className="mt-5">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <p className="text-[12px] text-ink-mute">多规格导出</p>
              <span className="text-[10.5px] text-ink-mute">
                原片 {detail.aspect}；导出只重排画面，不重新出图
              </span>
            </div>
            <label className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-ink-mute">
              导出规格
              <select
                className="field-input w-auto px-2 py-1 text-[11.5px]"
                value={exportPreset}
                onChange={(event) => setExportPreset(event.target.value)}
              >
                {(presets.length
                  ? presets
                  : [{ id: 'standard', label: '标准', note: '', fps: 0, loudness: null, aspect: '' }]
                ).map((preset) => (
                  <option key={preset.id} value={preset.id}>
                    {preset.label}
                  </option>
                ))}
              </select>
              <span className="min-w-0 flex-1 text-[10.5px]">
                {activePreset?.note ?? '沿用原帧率与画质，不做响度处理'}
              </span>
            </label>
            <div className="flex flex-wrap items-center gap-2">
              {exportCombos.map((combo) => {
                const same = detail.aspect === combo.aspect && !brand?.applied
                const hint = combo.label.endsWith('加品牌')
                  ? `原片画幅叠加品牌层导出 ${combo.aspect}（不重排画面）`
                  : combo.mode === 'fit'
                    ? `完整画面 + 模糊补边导出 ${combo.aspect}`
                    : `中心裁切导出 ${combo.aspect}，靠边内容可能被切`
                return (
                  <Button
                    key={`${combo.aspect}|${combo.mode}`}
                    small
                    disabled={same || Boolean(exportBusy) || brandLocked}
                    title={same ? '原片已经是这个画幅；要叠品牌请先保存品牌模板' : hint}
                    onClick={() => void launchExport(combo.aspect, combo.mode)}
                  >
                    {combo.label}
                  </Button>
                )
              })}
            </div>
            {brandLocked && (
              <p className="mt-1.5 text-[10.5px] leading-relaxed text-warn">
                {brandDirty
                  ? '品牌模板有未保存的修改：先保存或撤销，再用最新母版导出，避免导出旧品牌。'
                  : '品牌模板正在写入：等保存或撤销落盘后再导出。'}
              </p>
            )}
            <p className="mt-1.5 text-[10.5px] leading-relaxed text-ink-mute">
              「适配」保留完整画面，两侧用同一画面的模糊版补边，烧进画面的字幕不会被切；
              「裁切」主体更大，但左右会被切掉，靠边字幕可能损失。同一规格重复导出会命中缓存。
            </p>
            <div className="mt-2 rounded-lg border border-[var(--ring)] px-2.5 py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[10.5px] text-ink-mute">批量选择（按顺序渲染，不并发抢 CPU）</span>
                <Button
                  small
                  variant="primary"
                  disabled={selectedSpecs.length === 0 || Boolean(exportBusy) || brandLocked}
                  onClick={() => void launchBatch()}
                >
                  {exportBusy === 'batch'
                    ? '提交中…'
                    : `批量导出选中 ${selectedSpecs.length > 0 ? `(${selectedSpecs.length})` : ''}`}
                </Button>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
                {exportCombos.map((combo) => {
                  const key = `${combo.aspect}|${combo.mode}`
                  const same = detail.aspect === combo.aspect && !brand?.applied
                  return (
                    <label
                      key={key}
                      className={`flex items-center gap-1 text-[10.5px] ${same ? 'text-ink-mute/60' : 'text-ink-dim'}`}
                      title={same ? '原片已经是这个画幅，批量导出会跳过' : combo.label}
                    >
                      <input
                        type="checkbox"
                        disabled={same || Boolean(exportBusy) || brandLocked}
                        checked={selectedSpecs.includes(key)}
                        onChange={() => {
                          setSelectedSpecs((prev) =>
                            prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key],
                          )
                        }}
                      />
                      {combo.label}
                    </label>
                  )
                })}
              </div>
              {brand?.applied && (
                <p className="mt-1 text-[10px] text-ink-mute">
                  当前品牌模板 v{brand.brand.version} 会叠加到这批导出上；保存新模板后旧导出自动失效。
                </p>
              )}
            </div>
            {exports.length > 0 && (
              <div className="mt-2 space-y-1">
                {exports.map((item) => (
                  <div
                    key={item.export_id}
                    className="flex flex-wrap items-center gap-2 border-b border-[var(--ring)] py-1.5 last:border-b-0"
                  >
                    <span className="font-mono text-[11px]">
                      {item.aspect} · {item.mode === 'fit' ? '适配' : '裁切'}
                    </span>
                    {item.preset && item.preset !== 'standard' && (
                      <Badge tone="idle">
                        {presets.find((preset) => preset.id === item.preset)?.label ?? item.preset}
                      </Badge>
                    )}
                    {item.branded && (
                      <Badge tone="idle">
                        品牌层{item.brand_version ? ` v${item.brand_version}` : ''}
                      </Badge>
                    )}
                    {item.status === 'running' && <Badge tone="warn">导出中…</Badge>}
                    {item.status === 'failed' && (
                      <span className="min-w-0 flex-1 truncate text-[10.5px] text-err">
                        {item.error || '导出失败'}
                      </span>
                    )}
                    {item.status === 'done' && (
                      <a
                        className="text-[11px] transition-colors hover:text-ink"
                        href={exportFileUrl(runId, item.export_id)}
                        download={`${detail.title || runId}-${item.aspect.replace(':', 'x')}-${item.mode}.mp4`}
                      >
                        下载 {item.width}×{item.height} · {item.size_mb} MB
                      </a>
                    )}
                  </div>
                ))}
              </div>
            )}
            {exportMsg && <p className="mt-1.5 text-[11px] leading-relaxed text-ink-dim">{exportMsg}</p>}
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
