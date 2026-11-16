import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'

import {
  ApiError,
  fetchPlan,
  fetchSubtitles,
  fetchSubtitleStyle,
  fetchVersions,
  fetchWaveform,
  patchScript,
  patchShot,
  replanAct,
  reorderShots,
  restoreVersion,
  savePalette,
  saveSubtitles,
  saveSubtitleStyle,
  deleteSubjectReference,
  patchSubject,
  subjectReferenceUrl,
  uploadSubjectReference,
  type PlanAct,
  type AudioEdit,
  type PaletteId,
  type PlanMeta,
  type PlanResult,
  type PlanShot,
  type PlanSubject,
  type PlanVersion,
  type RenderOptions,
  type RenderProfile,
  type ShotPatch,
  type SubtitleLine,
  type SubtitleStyleCoverage,
  type SubtitleStylePreset,
  type SubtitleStyleState,
  type SubtitleTrack,
  type TransitionId,
  type Waveform,
} from '../lib/api'
import { PALETTE_OPTIONS } from '../lib/palettes'
import { setEditGuard } from '../lib/editGuard'
import { Badge, Button, Panel } from '../components/ui'

/** 与 `pipeline/models.py` 的 MIN/MAX_SHOT_SECONDS 一致。改这里也要改那边。 */
const MIN_SHOT = 2
const MAX_SHOT = 10

/** 与 `pipeline/compose.py` 的 TRANSITIONS 一致：只做三种，不做自由时间线。 */
const TRANSITION_OPTIONS: { id: TransitionId; label: string; hint: string }[] = [
  { id: 'none', label: '硬切', hint: '默认：直接拼接，不重编码' },
  { id: 'fade', label: '叠化', hint: '前后画面交叉淡化' },
  { id: 'slide', label: '左滑', hint: '画面向左滑出、下一镜从左滑入' },
  { id: 'black', label: '黑场', hint: '先淡出到黑，再从黑淡入' },
]

function fmtSeconds(value: number): string {
  return (Math.round(value * 10) / 10).toFixed(1)
}

/** 拍摄配置的一句话说明。界面必须显示"实际会用哪套参数"，而不是让用户猜。 */
function profileLabel(profile: RenderProfile | undefined): string {
  if (!profile) return ''
  const quality = { fast: '快', medium: '均衡', high: '精细' }[profile.painted_quality] || '均衡'
  const mode = profile.painted
    ? `代码绘制 ${profile.painted_fps}fps · ${quality}`
    : profile.live
      ? '真实模型出图'
      : '离线出图'
  return `${profile.live ? '真实模型' : '离线'} · ${mode}`
}

const SUBTITLE_SOURCE: Record<string, string> = {
  edited: '人工保存',
  rendered: '读自上次成片',
  plan: '按分镜推导',
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** 焦点是不是落在带 `data-field` 的那一格。
 *
 * 为什么不靠 onFocus 记标志：自动填充、输入法、脚本聚焦未必触发 React 的
 * 合成 focus 事件，标志可能没置上，服务端回写就会把用户正在打的字抹掉。
 * 直接看 activeElement 是确定的。
 */
function isEditingField(field: string): boolean {
  const active = document.activeElement as HTMLElement | null
  return Boolean(active?.dataset?.field === field)
}

/** 字幕轨的"已落盘"指纹。和面板当前值不一致就说明有改动还没保存。 */
function subtitleSignature(lines: SubtitleLine[], mode: string): string {
  return `${mode}|${JSON.stringify(lines)}`
}

/** 重拆的三种结局。`need-force` 不是失败，是"要你再确认一次"。 */
type ReplanOutcome =
  | { kind: 'done' }
  | { kind: 'need-force'; detail: string }
  | { kind: 'error'; detail: string }

/** 一场的文字。失焦即存，右栏的预估随之变。 */
function ActCard({
  act,
  index,
  shotCount,
  staleCount,
  busy,
  locked,
  onSave,
  onReplan,
}: {
  act: PlanAct
  index: number
  shotCount: number
  staleCount: number
  busy: boolean
  /** 重拆/恢复版本正在整表重写项目：这时候的失焦保存会撞上旧版本号。 */
  locked: boolean
  onSave: (next: PlanAct) => Promise<boolean>
  onReplan: (actName: string, force: boolean) => Promise<ReplanOutcome>
}) {
  const text = String(act.content ?? act.summary ?? '')
  const [draft, setDraft] = useState(text)
  const [confirming, setConfirming] = useState('')
  const [error, setError] = useState('')
  const actKey = `act:${index}`

  useEffect(() => {
    // 正在写的那一场不被服务端回写冲掉（409 会整表读回）。
    if (!isEditingField(actKey)) setDraft(String(act.content ?? act.summary ?? ''))
  }, [act.content, act.summary])

  const commit = () => {
    if (draft === text) return
    // 保留原对象里的其他键（time_range 等），只换文字——
    // 整个对象重造会让后端的"哪一场被改了"判定失效。
    void onSave({ ...act, content: draft })
  }

  const run = async (force: boolean) => {
    setError('')
    const res = await onReplan(String(act.name ?? ''), force)
    if (res.kind === 'need-force') setConfirming(res.detail)
    else if (res.kind === 'error') setError(res.detail)
    else setConfirming('')
  }

  // 只在"这一场有未处理的改动"或"这一场根本没拆到镜头"时才给重拆入口：
  // 平时不出现，免得手滑把好好的分镜冲掉。
  const showReplan = staleCount > 0 || shotCount === 0

  return (
    <div className="rounded-lg border border-[var(--ring)] px-3 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[12.5px] font-medium">{act.name || `第 ${index + 1} 场`}</span>
        <span className="flex items-baseline gap-2">
          {/* 「这一拍会变成几镜」：0 镜不是错误，但必须让人看见，
              否则剧本里写着的一场戏会静悄悄地从成片里消失。 */}
          <span
            className={[
              'font-mono text-[10px]',
              shotCount === 0 ? 'text-warn' : 'text-ink-mute',
            ].join(' ')}
          >
            {shotCount ? `${shotCount} 镜` : '未拆到镜头'}
          </span>
          {act.time_range && (
            <span className="font-mono text-[10.5px] text-ink-mute">{act.time_range}</span>
          )}
        </span>
      </div>
      <textarea
        value={draft}
        rows={2}
        disabled={locked}
        className="field-input mt-1.5 min-h-[46px] w-full resize-y text-[11.5px] leading-relaxed"
        onChange={(event) => setDraft(event.target.value)}
        data-field={actKey}
        onBlur={commit}
      />

      {showReplan && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <Button small disabled={busy || locked} onClick={() => void run(false)}>
            {busy ? '正在重拆…' : '重拆这一场'}
          </Button>
          {staleCount > 0 && (
            <span className="font-mono text-[10px] text-warn">{staleCount} 镜待确认</span>
          )}
        </div>
      )}

      {confirming && (
        <div className="mt-1.5 rounded-md border border-warn/40 bg-warn/8 px-2 py-1.5">
          <p className="text-[11px] leading-relaxed text-warn">{confirming}</p>
          <div className="mt-1.5 flex gap-2">
            <Button small variant="primary" disabled={busy || locked} onClick={() => void run(true)}>
              仍然重拆
            </Button>
            <Button small disabled={busy || locked} onClick={() => setConfirming('')}>
              取消
            </Button>
          </div>
        </div>
      )}

      {error && <p className="mt-1.5 text-[11px] leading-relaxed text-err">{error}</p>}
    </div>
  )
}

/** 一个角色的卡片：参考图 + 名称 + 视觉锁定。
 *
 * 参考图是**项目素材**，不喂给出图后端；能用的一致性手段是 visual_lock 文本。
 * 界面必须把这件事写明，否则用户会以为传了图画面就会一致。
 */
function SubjectCard({
  runId,
  subject,
  shotIds,
  busy,
  rewriteBusy,
  onSave,
  onUpload,
  onRemove,
}: {
  runId: string
  subject: PlanSubject
  shotIds: number[]
  busy: boolean
  /** 重拆/恢复版本正在重写项目，此时的失焦保存会带着旧版本号撞上去。 */
  rewriteBusy: boolean
  onSave: (patch: { name?: string; visual_lock?: string }) => Promise<boolean>
  onUpload: (file: File) => Promise<boolean>
  onRemove: () => Promise<boolean>
}) {
  const [name, setName] = useState(subject.name)
  const [lock, setLock] = useState(subject.visual_lock ?? '')
  const [fileBusy, setFileBusy] = useState(false)
  const [error, setError] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const fieldKey = (name: string) => `${subject.id}:${name}`

  useEffect(() => {
    // 正在输入的那一格不被服务端回写冲掉（409 会整表读回）。
    if (!isEditingField(fieldKey('name'))) setName(subject.name)
    if (!isEditingField(fieldKey('lock'))) setLock(subject.visual_lock ?? '')
  }, [subject.id, subject.name, subject.visual_lock])

  const commit = async (patch: { name?: string; visual_lock?: string }) => {
    setError('')
    const ok = await onSave(patch)
    if (!ok) {
      setName(subject.name)
      setLock(subject.visual_lock ?? '')
    }
  }

  const pick = () => fileRef.current?.click()

  const choose = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    // 清掉 value：同一张图再选一次也要触发 onChange。
    event.target.value = ''
    if (!file) return
    setFileBusy(true)
    setError('')
    try {
      const ok = await onUpload(file)
      if (!ok) setError('参考图上传失败')
    } finally {
      setFileBusy(false)
    }
  }

  const remove = async () => {
    if (!window.confirm(`移除「${subject.name}」的参考图？画面不会因此改变。`)) return
    setFileBusy(true)
    setError('')
    try {
      const ok = await onRemove()
      if (!ok) setError('移除失败')
    } finally {
      setFileBusy(false)
    }
  }

  const ref = subject.reference
  const locked = fileBusy || busy || rewriteBusy

  return (
    <div className="rounded-lg border border-[var(--ring)] px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-[10.5px] text-ink-mute">{subject.id}</span>
        <span className="text-[10.5px] text-ink-mute">
          {shotIds.length ? `引用 ${shotIds.length} 镜` : '没有镜头引用'}
        </span>
      </div>

      <div className="mt-2 flex gap-3">
        <div className="flex h-[84px] w-[84px] shrink-0 items-center justify-center overflow-hidden rounded-md border border-[var(--ring)] bg-black/20">
          {ref ? (
            <img
              src={subjectReferenceUrl(runId, subject.id, ref.sha256.slice(0, 12))}
              alt={`${subject.name} 参考图`}
              className="h-full w-full object-cover"
            />
          ) : (
            <span className="px-1 text-center text-[10px] leading-4 text-ink-mute">无参考图</span>
          )}
        </div>

        <div className="min-w-0 flex-1 space-y-1.5">
          <input
            value={name}
            maxLength={40}
            disabled={locked}
            placeholder="角色名"
            className="field-input w-full px-2 py-1 text-[12px]"
            onChange={(event) => setName(event.target.value)}
            data-field={fieldKey('name')}
            onBlur={() => {
              if (name.trim() && name !== subject.name) void commit({ name: name.trim() })
              else setName(subject.name)
            }}
          />
          <textarea
            value={lock}
            rows={2}
            maxLength={400}
            disabled={locked}
            placeholder="视觉锁定：外形、配色、服装、材质……出图时拼进这一镜的提示词"
            className="field-input min-h-[44px] w-full resize-y px-2 py-1 text-[11px] leading-relaxed"
            onChange={(event) => setLock(event.target.value)}
            data-field={fieldKey('lock')}
            onBlur={() => {
              if (lock !== (subject.visual_lock ?? '')) void commit({ visual_lock: lock })
            }}
          />
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          className="hidden"
          onChange={(event) => void choose(event)}
        />
        <Button small disabled={locked} onClick={pick}>
          {fileBusy ? '上传中…' : ref ? '更换参考图' : '上传参考图'}
        </Button>
        {ref && (
          <Button small variant="danger" disabled={locked} onClick={() => void remove()}>
            移除
          </Button>
        )}
        {ref && (
          <span className="font-mono text-[10px] text-ink-mute">
            {ref.width}×{ref.height} · {Math.max(1, Math.round(ref.bytes / 1024))}KB
          </span>
        )}
        <span className="text-[10px] text-ink-mute">
          {shotIds.length ? `镜 ${shotIds.join('、')}` : ''}
        </span>
      </div>

      {error && <p className="mt-1 text-[10.5px] text-err">{error}</p>}
    </div>
  )
}

/** 音轨波形 + 字幕块 + 镜头起点。
 *
 * 只读的校准视图：波形让人看清鼓点和句读，字幕块显示当前（含未保存）断句。
 * 时间仍然在字幕轨的数字输入里改——从波形拖拽改时间是下一刀，不在这层假装能做。
 */
type DragMode = 'move' | 'start' | 'end'

function WaveformStrip({
  wave,
  lines,
  shots,
  onAdjust,
}: {
  wave: Waveform
  lines: SubtitleLine[]
  shots: PlanShot[]
  /** 拖动字幕块时回写字幕轨。只改本地状态，保存仍由用户点「保存字幕」。 */
  onAdjust: (index: number, patch: Partial<SubtitleLine>) => void
}) {
  const W = 1000
  const H = 120
  const svgRef = useRef<SVGSVGElement>(null)
  const dragRef = useRef<{
    index: number
    mode: DragMode
    origin: number
    start: number
    end: number
  } | null>(null)
  const [activeLine, setActiveLine] = useState<number | null>(null)
  // 配乐模式会把一首短曲循环铺满解说：时间轴必须按"画面/字幕"的长度算，
  // 否则音轨结束后那半段字幕会全部挤在波形右边、对不上真实秒数。
  const audioEnd = Math.max(0.001, wave.duration || 0)
  const lastLine = lines.reduce((max, line) => Math.max(max, line.end), 0)
  const lastShot = shots.reduce((max, shot) => Math.max(max, shot.start + shot.duration), 0)
  const duration = Math.max(0.001, audioEnd, lastLine, lastShot)
  const waveWidth = (audioEnd / duration) * W

  const path = useMemo(() => {
    const peaks = wave.peaks
    if (!peaks.length) return ''
    const step = waveWidth / peaks.length
    const top: string[] = []
    const bottom: string[] = []
    peaks.forEach((peak, index) => {
      const x = (index + 0.5) * step
      const amp = Math.max(1, Math.min(1, peak) * (H / 2 - 8))
      top.push(`${x.toFixed(1)},${(H / 2 - amp).toFixed(1)}`)
      bottom.push(`${x.toFixed(1)},${(H / 2 + amp).toFixed(1)}`)
    })
    return `M ${top.join(' L ')} L ${bottom.reverse().join(' L ')} Z`
  }, [wave.peaks, waveWidth])

  const tickStep = duration <= 30 ? 5 : duration <= 90 ? 10 : duration <= 180 ? 20 : 30
  const ticks: number[] = []
  for (let t = 0; t <= duration + 0.001; t += tickStep) ticks.push(t)

  const timeAt = (clientX: number) => {
    const box = svgRef.current?.getBoundingClientRect()
    if (!box || box.width <= 0) return 0
    const ratio = (clientX - box.left) / box.width
    return Math.max(0, Math.min(duration, ratio * duration))
  }

  // 拖拽按 0.05 秒对齐：像素换算出来的 0.017 这种时间既难读也难再调。
  const snap = (value: number) => Math.round(value * 20) / 20
  const round3 = (value: number) => Math.round(value * 1000) / 1000

  const beginDrag = (event: ReactPointerEvent<SVGRectElement>, index: number, mode: DragMode) => {
    const line = lines[index]
    if (!line) return
    event.preventDefault()
    event.stopPropagation()
    svgRef.current?.setPointerCapture(event.pointerId)
    dragRef.current = {
      index,
      mode,
      origin: timeAt(event.clientX),
      start: line.start,
      end: line.end,
    }
    setActiveLine(index)
  }

  /** 拖动时实时改本地字幕轨。**限位到相邻行**：宁可拖不动，也不让保存时
   *  才被"字幕重叠"整条驳回——那时用户已经在波形上白调了几分钟。 */
  const moveDrag = (event: ReactPointerEvent<SVGSVGElement>) => {
    const state = dragRef.current
    if (!state) return
    const line = lines[state.index]
    if (!line) return
    const delta = timeAt(event.clientX) - state.origin
    const lower = Math.max(0, state.index > 0 ? lines[state.index - 1].end : 0)
    const upper = Math.min(duration, state.index < lines.length - 1
      ? lines[state.index + 1].start
      : duration)

    if (state.mode === 'move') {
      const width = state.end - state.start
      const high = Math.max(lower, upper - width)
      const start = Math.min(high, Math.max(lower, snap(state.start + delta)))
      onAdjust(state.index, {
        start: round3(start),
        end: Math.min(upper, round3(start + width)),
      })
    } else if (state.mode === 'start') {
      const high = Math.max(lower, state.end - 0.001)
      const start = Math.min(high, Math.max(lower, snap(state.start + delta)))
      onAdjust(state.index, { start: round3(start) })
    } else {
      const low = state.start + 0.001
      const high = Math.max(low, upper)
      const end = Math.max(low, Math.min(high, snap(state.end + delta)))
      onAdjust(state.index, { end: round3(end) })
    }
  }

  const endDrag = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!dragRef.current) return
    dragRef.current = null
    setActiveLine(null)
    try {
      svgRef.current?.releasePointerCapture(event.pointerId)
    } catch {
      /* 浏览器可能已经取消了拖动 */
    }
  }

  return (
    <svg
      ref={svgRef}
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className="h-[120px] w-full touch-none select-none"
      role="img"
      aria-label="音轨波形与字幕时间轴，可拖动字幕块调整时间"
      onPointerMove={moveDrag}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
    >
      <rect width={W} height={H} fill="currentColor" className="text-ink-mute" opacity={0.05} pointerEvents="none" />
      <line x1={0} y1={H / 2} x2={W} y2={H / 2} stroke="currentColor" className="text-ink-mute" opacity={0.18} pointerEvents="none" />

      {/* 字幕块先画，波形压在上层会出现"块里看不见波峰"的错觉；反过来更清楚。 */}
      {lines.map((line, index) => {
        if (line.end <= 0 || line.start >= duration) return null
        const left = (Math.max(0, line.start) / duration) * W
        const width = Math.max(2, ((Math.min(duration, line.end) - Math.max(0, line.start)) / duration) * W)
        const handle = Math.min(10, width / 3)
        const active = activeLine === index
        return (
          <g key={index}>
            <rect
              x={left}
              y={6}
              width={width}
              height={H - 12}
              fill="currentColor"
              className="text-accent cursor-move"
              opacity={active ? 0.28 : 0.16}
              onPointerDown={(event) => beginDrag(event, index, 'move')}
            >
              <title>{`${line.start.toFixed(2)}s → ${line.end.toFixed(2)}s：${line.text}`}</title>
            </rect>
            {width >= 14 && (
              <>
                <rect
                  x={left}
                  y={6}
                  width={handle}
                  height={H - 12}
                  fill="currentColor"
                  className="text-accent cursor-ew-resize"
                  opacity={0.5}
                  onPointerDown={(event) => beginDrag(event, index, 'start')}
                />
                <rect
                  x={left + width - handle}
                  y={6}
                  width={handle}
                  height={H - 12}
                  fill="currentColor"
                  className="text-accent cursor-ew-resize"
                  opacity={0.5}
                  onPointerDown={(event) => beginDrag(event, index, 'end')}
                />
              </>
            )}
          </g>
        )
      })}

      <path d={path} fill="currentColor" className="text-accent" opacity={0.55} pointerEvents="none" />

      {/* 音轨比画面短（配乐循环模式）：后半段没有波形，必须画出来，
          否则用户会以为"波形是空的"而不是"音乐在这里结束、后面是解说"。 */}
      {audioEnd < duration - 0.05 && (
        <>
          <rect
            x={waveWidth}
            y={0}
            width={W - waveWidth}
            height={H}
            fill="currentColor"
            className="text-ink-mute"
            opacity={0.08}
            pointerEvents="none"
          />
          <text
            x={Math.min(W - 10, waveWidth + 4)}
            y={12}
            fontSize={9}
            fill="currentColor"
            className="text-ink-mute"
            pointerEvents="none"
          >
            音轨结束
          </text>
        </>
      )}

      {shots.map((shot) => {
        if (shot.start <= 0 || shot.start >= duration) return null
        return (
          <line
            key={shot.id}
            x1={(shot.start / duration) * W}
            y1={0}
            x2={(shot.start / duration) * W}
            y2={H}
            stroke="currentColor"
            className="text-ink-mute"
            opacity={0.5}
            strokeWidth={1}
            strokeDasharray="4 6"
            pointerEvents="none"
          />
        )
      })}

      {ticks.map((tick) => {
        const x = (tick / duration) * W
        return (
          <text
            key={tick}
            x={Math.min(W - 24, x + 3)}
            y={H - 3}
            fontSize={9}
            fill="currentColor"
            className="text-ink-mute"
            pointerEvents="none"
          >
            {`${Math.floor(tick / 60)}:${String(Math.floor(tick % 60)).padStart(2, '0')}`}
          </text>
        )
      })}
    </svg>
  )
}

/** 一镜。四个可改字段各自失焦保存，改哪格存哪格。 */
function ShotEditor({
  shot,
  edited,
  stale,
  cached,
  locked,
  onSave,
  onReshoot,
  canReshoot,
  onPreviewShot,
  canPreview,
  onMove,
  canMoveUp,
  canMoveDown,
}: {
  shot: PlanShot
  edited: boolean
  stale: boolean
  /** 正式片段已存在：只重合成会复用它，画面不会变。 */
  cached: boolean
  /** 重拆/恢复版本正在整表替换分镜：这时候改字段会带着旧镜号与旧版本号发出去。 */
  locked: boolean
  onSave: (id: number, patch: ShotPatch) => Promise<boolean>
  onReshoot: (id: number) => void
  canReshoot: boolean
  onPreviewShot: (id: number) => void
  canPreview: boolean
  /** 上移 / 下移一位。只改顺序，镜号与时长都不动。 */
  onMove: (id: number, dir: -1 | 1) => void
  canMoveUp: boolean
  canMoveDown: boolean
}) {
  const [duration, setDuration] = useState(fmtSeconds(shot.duration))
  const [visual, setVisual] = useState(shot.visual)
  const [camera, setCamera] = useState(shot.camera)
  const [narration, setNarration] = useState(shot.narration)
  const fieldKey = (name: string) => `${shot.id}:${name}`

  useEffect(() => {
    // 正在输入的那一格不被服务端回写冲掉：409 会整表读回服务端版本，
    // 但用户手上还没失焦的草稿不该被顺手抹掉——那是一次无声的丢字。
    // 用 activeElement 判断而不是 onFocus 记标志：自动填充/输入法/脚本聚焦
    // 未必触发 React 的合成 focus 事件，标志可能没置上。
    if (!isEditingField(fieldKey('duration'))) setDuration(fmtSeconds(shot.duration))
    if (!isEditingField(fieldKey('visual'))) setVisual(shot.visual)
    if (!isEditingField(fieldKey('camera'))) setCamera(shot.camera)
    if (!isEditingField(fieldKey('narration'))) setNarration(shot.narration)
  }, [shot.id, shot.duration, shot.visual, shot.camera, shot.narration])

  const commitDuration = async () => {
    const next = Number(duration)
    if (!Number.isFinite(next) || Math.abs(next - shot.duration) < 1e-6) {
      setDuration(fmtSeconds(shot.duration))
      return
    }
    if (next < MIN_SHOT || next > MAX_SHOT) {
      // 前端先拦一次，用户不必等一次往返才知道填错了
      setDuration(fmtSeconds(shot.duration))
      return
    }
    const ok = await onSave(shot.id, { duration: next })
    if (!ok) setDuration(fmtSeconds(shot.duration))
  }

  const commitText = async (field: 'visual' | 'camera' | 'narration', value: string) => {
    if (value === shot[field]) return
    const ok = await onSave(shot.id, { [field]: value })
    if (!ok) {
      if (field === 'visual') setVisual(shot.visual)
      if (field === 'camera') setCamera(shot.camera)
      if (field === 'narration') setNarration(shot.narration)
    }
  }

  return (
    <div
      className={[
        'relative flex gap-3 border-b border-[var(--ring)] py-3 pl-3 last:border-b-0',
        edited
          ? 'before:absolute before:top-2 before:bottom-2 before:left-0 before:w-[2px] before:rounded-full before:bg-accent'
          : '',
      ].join(' ')}
    >
      <div className="w-14 shrink-0 pt-1">
        <span className="font-mono text-[11px] text-ink-mute">
          {String(shot.id).padStart(2, '0')}
        </span>
        <p className="mt-0.5 font-mono text-[10px] text-ink-mute/70">{fmtSeconds(shot.start)}s</p>
        {/* 挪顺序。不用拖拽：列表里同时还有输入框和 textarea，拖拽一碰就
            误触选中文字，而"上移一位"能覆盖绝大多数实际需求。
            镜号跟着镜头走，不随位置变——它同时是片段文件名和"改过"的记录。 */}
        <div className="mt-1 flex gap-0.5">
          <button
            type="button"
            aria-label={`镜 ${shot.id} 上移`}
            title={canMoveUp ? '与上一镜交换位置' : '已经是第一镜'}
            disabled={!canMoveUp}
            onClick={() => onMove(shot.id, -1)}
            className="rounded px-1 text-[10px] leading-4 text-ink-mute transition hover:bg-[var(--ring)] hover:text-ink disabled:cursor-default disabled:opacity-25 disabled:hover:bg-transparent"
          >
            ▲
          </button>
          <button
            type="button"
            aria-label={`镜 ${shot.id} 下移`}
            title={canMoveDown ? '与下一镜交换位置' : '已经是最后一镜'}
            disabled={!canMoveDown}
            onClick={() => onMove(shot.id, 1)}
            className="rounded px-1 text-[10px] leading-4 text-ink-mute transition hover:bg-[var(--ring)] hover:text-ink disabled:cursor-default disabled:opacity-25 disabled:hover:bg-transparent"
          >
            ▼
          </button>
        </div>
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="tag-chip">{shot.act}</span>
          {/* 这一镜引用了哪些角色。引用关系直接可见，出问题时才能对上号。 */}
          {(shot.subject_refs ?? []).map((id) => (
            <span key={id} className="tag-chip" title="引用角色，出图时带上其视觉锁定">
              {id}
            </span>
          ))}
          <span className="inline-flex items-center gap-1">
            <span className="inline-block w-[52px]">
              <input
                value={duration}
                inputMode="decimal"
                disabled={locked}
                className="field-input w-full px-1 py-0.5 text-center font-mono text-[11px] tabular-nums"
                onChange={(event) => setDuration(event.target.value)}
                data-field={fieldKey('duration')}
                onBlur={() => void commitDuration()}
              />
            </span>
            <span className="text-[10.5px] text-ink-mute">秒</span>
          </span>
          {edited && <Badge tone="live">改过</Badge>}
          {stale && <Badge tone="warn">剧本已改，待确认</Badge>}
          {cached && (
            <span title="正式片段已存在；只重合成会复用它，画面不会变">
              <Badge tone="idle">片段已缓存</Badge>
            </span>
          )}
          {/* 预览只出这一镜的低清片段，不动正式成片；重拍会重合成整片。 */}
          <Button
            small
            className="ml-auto"
            disabled={!canPreview}
            title="只生成这一镜的低清片段，不重合成整片，也不改动正式片段缓存"
            onClick={() => onPreviewShot(shot.id)}
          >
            预览这一镜
          </Button>
          <Button
            small
            disabled={!canReshoot}
            title={
              canReshoot
                ? `只重拍镜 ${shot.id}，其余镜头沿用上次的片段，然后整片重新合成`
                : '这条拆解还没拍过，先整片拍一次'
            }
            onClick={() => onReshoot(shot.id)}
          >
            重拍这一镜
          </Button>
        </div>

        <textarea
          value={visual}
          rows={2}
          disabled={locked}
          placeholder="画面"
          className="field-input mt-1.5 min-h-[40px] w-full resize-y text-[12.5px] leading-relaxed"
          onChange={(event) => setVisual(event.target.value)}
          data-field={fieldKey('visual')}
          onBlur={() => void commitText('visual', visual)}
        />

        <div className="mt-1.5 flex gap-2">
          {/* 定宽要包在外层：`.field-input` 自带 width:100%，直接写 w-[132px]
              会被它盖掉，结果把台词框挤成一条缝。 */}
          <div className="w-[136px] shrink-0">
            <input
              value={camera}
              placeholder="机位"
              disabled={locked}
              className="field-input w-full px-2 py-1 text-[11.5px]"
              onChange={(event) => setCamera(event.target.value)}
              data-field={fieldKey('camera')}
              onBlur={() => void commitText('camera', camera)}
            />
          </div>
          <div className="min-w-0 flex-1">
            <input
              value={narration}
              placeholder="台词"
              disabled={locked}
              className="field-input w-full px-2 py-1 text-[11.5px]"
              onChange={(event) => setNarration(event.target.value)}
              data-field={fieldKey('narration')}
              onBlur={() => void commitText('narration', narration)}
            />
          </div>
        </div>
      </div>
    </div>
  )
}

export function PlanPage({
  runId,
  onNavigate,
  onRender,
  onPreview,
  onPreviewShot,
  onReshoot,
  onRecompose,
}: {
  runId: string
  onNavigate: (next: string) => void
  onRender: (runId: string, opts?: RenderOptions) => void
  onPreview: (runId: string, opts?: RenderOptions) => void
  onPreviewShot: (runId: string, shotId: number, opts?: RenderOptions) => void
  onReshoot: (runId: string, shotId: number, opts?: RenderOptions) => void
  onRecompose: (runId: string, opts?: RenderOptions) => void
}) {
  const [plan, setPlan] = useState<PlanResult | null>(null)
  const [meta, setMeta] = useState<PlanMeta>({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  /** 正在重拆的那一场。重拆期间锁住拍摄按钮——否则会拿旧分镜出片。 */
  const [busyAct, setBusyAct] = useState('')
  /** 还没落盘的保存数。拍摄前要等它归零。 */
  const [pendingSaves, setPendingSaves] = useState(0)
  /** 编辑版本列表。恢复入口不应该藏在命令行里。 */
  const [versions, setVersions] = useState<PlanVersion[]>([])
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [busyVersion, setBusyVersion] = useState(0)
  const [paletteBusy, setPaletteBusy] = useState<PaletteId | ''>('')
  const [paletteMsg, setPaletteMsg] = useState('')
  /** 项目配置默认折叠：先让用户看到分镜；色板/角色/音轨/字幕一次切换可达。 */
  const [configOpen, setConfigOpen] = useState(false)

  /** 同一项目的写操作串行执行：并发 PATCH 会让两个镜头互相覆盖。 */
  const chainRef = useRef<Promise<void>>(Promise.resolve())
  /** 服务端版本。每次保存成功都跟着响应更新。 */
  const revisionRef = useRef(0)
  /** 还没解决的保存失败，按域记。镜头存成功不该把字幕的失败一笔勾销——
   * 那会让拍摄闸门放行，烧进画面的仍是没存进去的旧字幕。 */
  const failedSavesRef = useRef<Set<string>>(new Set())
  /** 队列里要用"最新"数据，不能依赖触发那一刻的闭包快照。 */
  const planRef = useRef<PlanResult | null>(null)
  useEffect(() => {
    planRef.current = plan
  }, [plan])

  /** 混音与字幕设置。只改声音/字幕轨，拍摄或重合成时随请求保存。 */
  const [mix, setMix] = useState<AudioEdit>({
    voice_gain: 1,
    music_gain: 0.28,
    voice_duck: false,
    subtitle_offset: 0,
    transition: 'none',
    text_anim: false,
  })
  const mixInitRef = useRef(false)
  useEffect(() => {
    if (!plan || mixInitRef.current) return
    setMix(meta.audio_edit ?? {
      voice_gain: 1,
      music_gain: 0.28,
      voice_duck: false,
      subtitle_offset: 0,
      transition: 'none',
      text_anim: false,
    })
    mixInitRef.current = true
  }, [plan, meta.audio_edit])

  /** 每次动作都带上当前面板值；服务端只在显式传入时回写项目。 */
  const mixOpts = () => ({
    voice_gain: mix.voice_gain,
    music_gain: mix.music_gain,
    voice_duck: mix.voice_duck,
    subtitle_offset: mix.subtitle_offset,
    transition: mix.transition,
    text_anim: mix.text_anim,
  })

  /** 项目音轨的峰值包络。只在有音轨时请求，避免拆解页一打开就报 404。 */
  const [wave, setWave] = useState<Waveform | null>(null)
  const [waveError, setWaveError] = useState('')
  const musicToken = plan?.music?.token ?? ''
  useEffect(() => {
    if (!runId || !musicToken) {
      setWave(null)
      setWaveError('')
      return
    }
    let alive = true
    fetchWaveform(runId, 1200)
      .then((data) => {
        if (!alive) return
        setWave(data)
        setWaveError('')
      })
      .catch((err: unknown) => {
        if (!alive) return
        setWave(null)
        setWaveError(errText(err) || '读不到音轨波形')
      })
    return () => {
      alive = false
    }
  }, [runId, musicToken])

  /** 字幕轨：可拆分/合并/改时间。保存后由「只重合成」生效。 */
  const [subtitles, setSubtitles] = useState<SubtitleTrack | null>(null)
  const [subLines, setSubLines] = useState<SubtitleLine[]>([])
  const [subOpen, setSubOpen] = useState(false)
  const [subBusy, setSubBusy] = useState(false)
  const [subMsg, setSubMsg] = useState('')
  /** 字幕呈现方式：烧录进画面、只出外置文件、完全关闭。 */
  const [subMode, setSubMode] = useState<'burn' | 'sidecar' | 'none'>('burn')
  /** 服务端已落盘的那一份字幕轨。空串表示还没读到，不算"有改动"。 */
  const [subSavedSig, setSubSavedSig] = useState('')
  /** 整句字幕样式：6 套预置 + 当前样式 + 缺字提示。 */
  const [subStyle, setSubStyle] = useState<SubtitleStyleState | null>(null)
  const [subStylePresets, setSubStylePresets] = useState<SubtitleStylePreset[]>([])
  const [subStyleCoverage, setSubStyleCoverage] = useState<SubtitleStyleCoverage | null>(null)
  const [subStyleBusy, setSubStyleBusy] = useState(false)
  const [subStyleMsg, setSubStyleMsg] = useState('')
  const subDirty = subSavedSig !== '' && subtitleSignature(subLines, subMode) !== subSavedSig
  /** 同一件事的同步版本：离开页面的确认发生在点击那一刻，等不到 setState 重渲染。 */
  const subDirtyRef = useRef(false)

  useEffect(() => {
    setEditGuard(() => {
      const parts: string[] = []
      if (subDirtyRef.current) parts.push('字幕轨有未保存的改动')
      if (failedSavesRef.current.size > 0) parts.push('有修改保存失败，还没写进项目')
      return parts.join('；')
    })
    return () => setEditGuard(null)
  }, [])

  const round3 = (value: number) => Math.round(value * 1000) / 1000

  const patchLine = (index: number, patch: Partial<SubtitleLine>) => {
    setSubLines((prev) => {
      const line = prev[index]
      if (!line) return prev
      const next = { ...line, ...patch }
      if (next.start === line.start && next.end === line.end && next.text === line.text) return prev
      subDirtyRef.current = true
      return prev.map((item, i) => (i === index ? next : item))
    })
  }

  const splitLine = (index: number) => {
    setSubLines((prev) => {
      const line = prev[index]
      if (!line) return prev
      const chars = Array.from(line.text)
      if (chars.length < 2 || line.end - line.start < 0.2) return prev
      const mid = round3((line.start + line.end) / 2)
      const cut = Math.max(1, Math.min(chars.length - 1, Math.round(chars.length / 2)))
      const next = [...prev]
      next.splice(
        index,
        1,
        { start: line.start, end: mid, text: chars.slice(0, cut).join('') },
        { start: mid, end: line.end, text: chars.slice(cut).join('') },
      )
      subDirtyRef.current = true
      return next
    })
  }

  const mergeLine = (index: number) => {
    setSubLines((prev) => {
      const first = prev[index]
      const second = prev[index + 1]
      if (!first || !second) return prev
      const next = [...prev]
      next.splice(index, 2, {
        start: first.start,
        end: second.end,
        text: `${first.text}${second.text}`,
      })
      subDirtyRef.current = true
      return next
    })
  }

  const dropLine = (index: number) => {
    setSubLines((prev) => {
      if (!prev[index]) return prev
      subDirtyRef.current = true
      return prev.filter((_, i) => i !== index)
    })
  }

  const addLine = () => {
    setSubLines((prev) => {
      const last = prev[prev.length - 1]
      const start = last ? last.end : 0
      const limit = subtitles?.duration || start + 1
      const end = Math.min(limit, start + 1)
      if (end <= start + 0.05) return prev
      subDirtyRef.current = true
      return [...prev, { start: round3(start), end: round3(end), text: '新字幕' }]
    })
  }

  const persistSubtitles = async (
    lines: SubtitleLine[] | null,
    mode: 'burn' | 'sidecar' | 'none' = subMode,
  ): Promise<boolean> => {
    if (!runId) return false
    setSubBusy(true)
    setSubMsg('')
    try {
      // 字幕也必须进入同一保存队列：否则用户点保存后立刻拍摄，
      // flushSaves() 看不到这条请求，拍摄会带着旧 revision 先发出去。
      const out = await enqueue('subtitle', async () =>
        saveSubtitles(runId, lines, revisionRef.current, mode))
      revisionRef.current = out.revision
      setMeta((prev) => ({ ...prev, revision: out.revision }))
      if (out.subtitle_mode) setSubMode(out.subtitle_mode)
      if (out.lines) {
        setSubLines(out.lines)
        setSubtitles((prev) => (prev ? { ...prev, source: 'edited', lines: out.lines! } : prev))
        setSubSavedSig(subtitleSignature(out.lines, out.subtitle_mode ?? mode))
      } else {
        // 恢复自动生成后，界面显示服务端重新推导出的轨道，而不是留一份空白。
        const fresh = await fetchSubtitles(runId)
        setSubtitles(fresh)
        setSubLines(fresh.lines)
        if (fresh.subtitle_mode) setSubMode(fresh.subtitle_mode)
        setSubSavedSig(subtitleSignature(fresh.lines, fresh.subtitle_mode ?? mode))
      }
      setSubMsg(out.note)
      subDirtyRef.current = false
      // 字幕文字变了，缺字检查结果也要跟着刷新（样式本身不变）。
      void fetchSubtitleStyle(runId)
        .then((info) => {
          setSubStyle(info.current)
          setSubStylePresets(info.presets)
          setSubStyleCoverage(info.coverage)
        })
        .catch(() => {})
      return true
    } catch (err) {
      await noteSaveFailure(err)
      setSubMsg(err instanceof Error ? err.message : '保存字幕失败')
      return false
    } finally {
      setSubBusy(false)
    }
  }

  const pickSubtitleStyle = async (preset: string) => {
    if (!runId || subStyleBusy || subStyle?.id === preset) return
    setSubStyleBusy(true)
    setSubStyleMsg('')
    try {
      // 与字幕轨共用保存队列：拍摄会先等这类设置落地，再带着新 revision 发请求。
      const out = await enqueue('subtitle-style', async () =>
        saveSubtitleStyle(runId, preset, revisionRef.current))
      revisionRef.current = out.revision
      setMeta((prev) => ({ ...prev, revision: out.revision }))
      setSubStyle(out.current)
      setSubStyleCoverage(out.coverage)
      setSubStyleMsg(out.note)
    } catch (err) {
      await noteSaveFailure(err)
      setSubStyleMsg(err instanceof Error ? err.message : '保存字幕样式失败')
    } finally {
      setSubStyleBusy(false)
    }
  }

  useEffect(() => {
    if (!runId) {
      setLoading(false)
      return
    }
    setLoading(true)
    fetchPlan(runId)
      .then((data) => {
        setPlan(data)
        setMeta(data.meta ?? {})
        revisionRef.current = Number(data.meta?.revision ?? 0)
        setError('')
      })
      .catch((err: unknown) => setError(errText(err) || '读不到这次拆解'))
      .finally(() => setLoading(false))
    fetchVersions(runId)
      .then((data) => setVersions(data.versions))
      .catch(() => setVersions([]))
    fetchSubtitles(runId)
      .then((track) => {
        setSubtitles(track)
        setSubLines(track.lines)
        if (track.subtitle_mode) setSubMode(track.subtitle_mode)
        setSubSavedSig(subtitleSignature(track.lines, track.subtitle_mode ?? 'burn'))
        subDirtyRef.current = false
      })
      .catch(() => setSubtitles(null))
    fetchSubtitleStyle(runId)
      .then((info) => {
        setSubStyle(info.current)
        setSubStylePresets(info.presets)
        setSubStyleCoverage(info.coverage)
      })
      .catch(() => {
        setSubStyle(null)
        setSubStylePresets([])
      })
  }, [runId])

  /** 某个域的保存落地了：只销它自己的失败记录，别的域还欠着就继续拦。 */
  const markSaved = useCallback((domain: string) => {
    failedSavesRef.current.delete(domain)
    if (failedSavesRef.current.size === 0) setError('')
  }, [])

  const enqueue = useCallback(
    <T,>(domain: string, task: () => Promise<T>): Promise<T> => {
      const run = chainRef.current.then(task, task).then(
        (value) => {
          markSaved(domain)
          return value
        },
        (err: unknown) => {
          failedSavesRef.current.add(domain)
          throw err
        },
      )
      chainRef.current = run.then(
        () => undefined,
        () => undefined,
      )
      setPendingSaves((n) => n + 1)
      void run.then(
        () => setPendingSaves((n) => Math.max(0, n - 1)),
        () => setPendingSaves((n) => Math.max(0, n - 1)),
      )
      return run
    },
    [markSaved],
  )

  /** 等所有排队中的保存落地。返回 false 表示有失败，调用方不该继续拍摄。 */
  const flushSaves = useCallback(async (): Promise<boolean> => {
    await chainRef.current
    return failedSavesRef.current.size === 0
  }, [])

  const noteSaveFailure = useCallback(
    async (err: unknown) => {
      setError(errText(err))
      setNotice('')
      // 409 = 别的窗口/标签页先改过。把整份拆解读回来：只更新版本号的话，
      // 屏幕上还留着被拒绝的那份编辑，用户会以为它已经生效。
      if (err instanceof ApiError && err.status === 409 && runId) {
        try {
          const snap = await fetchPlan(runId)
          setPlan(snap)
          setMeta(snap.meta ?? {})
          revisionRef.current = Number(snap.meta?.revision ?? revisionRef.current)
          setNotice('已刷新到服务端的最新版本；刚才那次修改没有写进去，需要重新改一次。')
        } catch {
          /* 读不到就算了，错误信息已经给出 */
        }
      }
    },
    [runId],
  )

  const shots = plan?.shots ?? []
  const acts = plan?.script?.acts ?? []
  const total = useMemo(() => shots.reduce((sum, shot) => sum + shot.duration, 0), [shots])
  const editedShots = meta.edited?.shots ?? []
  const staleShots = meta.stale_shots ?? []
  const cachedShots = plan?.clips?.available ?? []
  const outOfRange = shots.filter((shot) => shot.duration < MIN_SHOT || shot.duration > MAX_SHOT)
  const currentPalette = (meta.palette ?? plan?.meta?.palette ?? 'auto') as PaletteId

  /** 改色板会递增项目版本；保存前先等编辑落地，避免基于旧版本写入。 */
  const changePalette = async (palette: PaletteId) => {
    if (palette === currentPalette || paletteBusy) return
    if (!(await flushSaves())) {
      setPaletteMsg('有修改尚未保存成功，先处理保存失败再换色板')
      return
    }
    setPaletteBusy(palette)
    setPaletteMsg('')
    try {
      // 进同一保存队列，并在真正发请求时读取实时 revision。等待 flush 后再用
      // 闭包里的 meta.revision 会漏掉前一条保存刚更新的版本，造成无谓 409。
      const res = await enqueue('palette', async () => savePalette(runId, palette, revisionRef.current))
      revisionRef.current = res.revision
      setMeta((prev) => ({ ...prev, palette: res.palette, revision: res.revision }))
      setPlan((prev) => (prev
        ? {
            ...prev,
            meta: {
              ...(prev.meta ?? {}),
              palette: res.palette,
              revision: res.revision,
            },
          }
        : prev))
      setPaletteMsg(res.note)
    } catch (err) {
      await noteSaveFailure(err)
      setPaletteMsg(errText(err))
    } finally {
      setPaletteBusy('')
    }
  }

  const saveShot = useCallback(
    async (id: number, patch: ShotPatch): Promise<boolean> => {
      try {
        await enqueue('shot', async () => {
          const res = await patchShot(runId, id, {
            ...patch,
            expected_revision: revisionRef.current,
          })
          setPlan((prev) =>
            prev
              ? {
                  ...prev,
                  // 整表按 moved 更新：改一镜时长会牵动后面所有镜头的 start，
                  // 只改被改那一行会让时间轴显示对不上。
                  shots: prev.shots.map((shot) => {
                    if (shot.id === res.shot.id) return { ...shot, ...res.shot }
                    const moved = res.moved.find((item) => item.id === shot.id)
                    return moved ? { ...shot, start: moved.start } : shot
                  }),
                  project: { ...prev.project, total_duration: res.total_duration },
                }
              : prev,
          )
          if (res.meta) {
            setMeta(res.meta)
            revisionRef.current = Number(res.meta.revision ?? revisionRef.current)
          }
          setNotice(res.note)
        })
        return true
      } catch (err) {
        await noteSaveFailure(err)
        return false
      }
    },
    [enqueue, noteSaveFailure, runId],
  )

  const saveAct = useCallback(
    async (act: PlanAct, index: number): Promise<boolean> => {
      try {
        await enqueue('script', async () => {
          // 排在队里才读当前场次列表与版本：上一次保存可能刚更新过它们。
          const next = [...(planRef.current?.script?.acts ?? [])]
          next[index] = act
          const res = await patchScript(runId, {
            acts: next,
            expected_revision: revisionRef.current,
          })
          setPlan((prev) => (prev ? { ...prev, script: res.script } : prev))
          setMeta(res.meta)
          revisionRef.current = Number(res.meta.revision ?? revisionRef.current)
          setNotice(res.note)
        })
        return true
      } catch (err) {
        await noteSaveFailure(err)
        return false
      }
    },
    [enqueue, noteSaveFailure, runId],
  )

  /** 角色卡保存。和镜头/剧本共用同一条保存队列与版本号。 */
  const saveSubject = useCallback(
    async (subjectId: string, patch: { name?: string; visual_lock?: string }): Promise<boolean> => {
      try {
        await enqueue('subject', async () => {
          const res = await patchSubject(runId, subjectId, {
            ...patch,
            expected_revision: revisionRef.current,
          })
          setPlan((prev) =>
            prev
              ? {
                  ...prev,
                  subjects: prev.subjects.map((subject) =>
                    subject.id === subjectId ? res.subject : subject,
                  ),
                }
              : prev,
          )
          if (res.meta) {
            setMeta(res.meta)
            revisionRef.current = Number(res.meta.revision ?? revisionRef.current)
          }
          setNotice(res.note)
        })
        return true
      } catch (err) {
        await noteSaveFailure(err)
        return false
      }
    },
    [enqueue, noteSaveFailure, runId],
  )

  /** 参考图上传/移除同样走保存队列：它们会递增版本，不能和别的写并行。 */
  const changeReference = useCallback(
    async (
      subjectId: string,
      action: 'upload' | 'remove',
      file?: File,
    ): Promise<boolean> => {
      try {
        await enqueue('reference', async () => {
          const res =
            action === 'upload' && file
              ? await uploadSubjectReference(runId, subjectId, file, revisionRef.current)
              : await deleteSubjectReference(runId, subjectId, revisionRef.current)
          setPlan((prev) =>
            prev
              ? {
                  ...prev,
                  subjects: prev.subjects.map((subject) =>
                    subject.id === subjectId ? res.subject : subject,
                  ),
                }
              : prev,
          )
          if (res.meta) {
            setMeta(res.meta)
            revisionRef.current = Number(res.meta.revision ?? revisionRef.current)
          }
          setNotice(res.note)
        })
        return true
      } catch (err) {
        await noteSaveFailure(err)
        return false
      }
    },
    [enqueue, noteSaveFailure, runId],
  )

  /** 挪一位。发的是整条顺序——后端只接受完整置换，缺项一律拒，
   *  免得"悄悄少了一镜"这种最难查的丢失。 */
  const moveShot = useCallback(
    async (id: number, dir: -1 | 1) => {
      try {
        await enqueue('order', async () => {
          const list = planRef.current?.shots ?? []
          const order = list.map((shot) => shot.id)
          const from = order.indexOf(id)
          const to = from + dir
          if (from < 0 || to < 0 || to >= order.length) return
          order.splice(from, 1)
          order.splice(to, 0, id)
          const res = await reorderShots(runId, order, revisionRef.current)
          // 整表替换：顺序变了之后每一镜的 start 都要重算，逐行 diff 反而容易漏。
          setPlan((prev) =>
            prev
              ? {
                  ...prev,
                  shots: res.shots,
                  project: { ...prev.project, total_duration: res.total_duration },
                }
              : prev,
          )
          if (res.meta) {
            setMeta(res.meta)
            revisionRef.current = Number(res.meta.revision ?? revisionRef.current)
          }
          setNotice(res.note)
        })
      } catch (err) {
        await noteSaveFailure(err)
      }
    },
    [enqueue, noteSaveFailure, runId],
  )

  const replan = useCallback(
    async (actName: string, force: boolean): Promise<ReplanOutcome> => {
      if (!actName) return { kind: 'error', detail: '这一场没有场次名，无法重拆' }
      // 重拆会整表替换分镜：先把未落盘的编辑写完，否则它们会被新分镜覆盖，
      // 用户看到的是"我明明改了，重拆之后没了"。
      if (!(await flushSaves())) {
        return { kind: 'error', detail: '有修改尚未保存成功，先解决保存失败再重拆' }
      }
      setBusyAct(actName)
      try {
        const res = await replanAct(runId, actName, force, revisionRef.current)
        // 镜号会全片重排，所以整表替换而不是只换这一场的几行：
        // 只换几行会让后面场的镜号与显示对不上，改时长时更容易改错镜。
        setPlan((prev) =>
          prev
            ? {
                ...prev,
                project: res.storyboard.project ?? prev.project,
                subjects: res.storyboard.subjects ?? prev.subjects,
                shots: res.storyboard.shots ?? prev.shots,
              }
            : prev,
        )
        setMeta(res.meta)
        revisionRef.current = Number(res.meta.revision ?? revisionRef.current)
        setNotice(res.note)
        setError('')
        return { kind: 'done' }
      } catch (err) {
        // 409 = 这一场有你改过的镜头，后端拒绝自动覆盖。这不是失败，
        // 是要人确认一次，所以交给卡片做内联确认，不占顶部错误条。
        if (err instanceof ApiError && err.status === 409) {
          return { kind: 'need-force', detail: err.message }
        }
        const detail = errText(err)
        setError(detail)
        setNotice('')
        return { kind: 'error', detail }
      } finally {
        setBusyAct('')
      }
    },
    [flushSaves, runId],
  )

  /** 拍摄 / 重拍 / 重合成之前统一过这道关：等保存落地，再带上同一版本。
   *
   * 字幕轨是面板里的本地草稿（改字/拖时间轴都不会自动发请求），所以这里先把它
   * 落盘再拍——否则屏幕上明明是新字幕，烧进画面的还是上一份。 */
  const startAction = async (
    launch: (id: string, opts: RenderOptions) => void,
    extra: RenderOptions = {},
  ) => {
    if (!plan) return
    if (subDirty && !(await persistSubtitles(subLines, subMode))) {
      setError('字幕轨没能保存成功。先处理保存失败再拍摄，否则烧的是旧字幕。')
      setNotice('')
      return
    }
    if (!(await flushSaves())) {
      setError('有修改尚未保存成功。先处理保存失败再拍摄，否则拍的是旧分镜。')
      setNotice('')
      return
    }
    launch(plan.run_id || runId, { expected_revision: revisionRef.current, ...extra })
  }

  /** 恢复到某个编辑版本。先 flush 未落盘的修改，否则恢复完又被旧编辑覆盖。 */
  const restore = async (revision: number) => {
    if (subDirty && !(await persistSubtitles(subLines, subMode))) {
      setError('字幕轨没能保存成功，先处理保存失败再恢复版本')
      setNotice('')
      return
    }
    if (!(await flushSaves())) {
      setError('有修改尚未保存成功，先处理保存失败再恢复版本')
      setNotice('')
      return
    }
    if (!window.confirm(`恢复到版本 ${revision}？当前剧本与分镜会被替换（音轨和拍摄配置不变）。`)) {
      return
    }
    setBusyVersion(revision)
    try {
      const data = await restoreVersion(runId, revision)
      setPlan(data)
      setMeta(data.meta ?? {})
      revisionRef.current = Number(data.meta?.revision ?? 0)
      setNotice(data.note || `已恢复到版本 ${revision}`)
      setError('')
      const list = await fetchVersions(runId)
      setVersions(list.versions)
    } catch (err) {
      setError(errText(err))
      setNotice('')
    } finally {
      setBusyVersion(0)
    }
  }

  // 重拆一场、恢复版本、换色板都会重写项目：这些动作在飞的时候不许再发起拍摄，
  // 也不许把还在排队的保存当成"已经落盘"。
  const editsBusy =
    Boolean(busyAct) || subBusy || Boolean(paletteBusy) || busyVersion > 0 || pendingSaves > 0
  /** 整表重写进行中。字段级失焦保存必须停手：它们带的是重写前的镜号与版本号，
   *  发出去要么 409，要么把刚重拆好的分镜又改回旧值。 */
  const rewriteBusy = Boolean(busyAct) || busyVersion > 0
  const renderDisabled = !plan || outOfRange.length > 0 || editsBusy

  // 只重合成：拍过才有片段可重拼；时长越界也不能拼（会拼出一段不该有的长度）。
  const recomposeDisabled =
    !plan || !meta.rendered || outOfRange.length > 0 || editsBusy

  // 创作页选的那套配置。先拆后拍时拍的必须是它，不能是接口默认值。
  const profile = meta.render_profile
  const profileText = profileLabel(profile)

  // 音轨徽章。用户从创作台跳过来之后，这是唯一能确认"我刚才选的 MV 还在"的地方——
  // 拍摄阶段靠 `meta.music` 回落，界面上看不见就没人知道它还生效。
  const music = plan?.music
  const musicName = (music?.name || '').replace(/\.[^.]+$/, '')
  const musicLabel = musicName.length > 12 ? `${musicName.slice(0, 12)}…` : musicName
  const hasMusic = Boolean(music?.token)
  /** MV 模式音乐就是主音轨，不生成配音，人声电平没有意义。 */
  const isMv = music?.role === 'song'

  return (
    <div className="mx-auto w-full max-w-[1340px] px-6 pt-7 pb-16">
      <div className="mb-4">
        <nav className="flex items-center gap-1.5 text-[11px] text-ink-mute" aria-label="位置">
          <button
            type="button"
            onClick={() => onNavigate('projects')}
            className="transition-colors hover:text-ink-dim"
          >
            项目
          </button>
          <span aria-hidden="true">/</span>
          <span className="text-ink-dim">拆解</span>
        </nav>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">拆解</h2>
            <p className="mt-1.5 max-w-[680px] text-[12.5px] leading-relaxed text-ink-mute">
              {plan
                ? `${acts.length} 场 · ${shots.length} 镜 · ${fmtSeconds(total)} 秒　—　改完再拍，拍的就是你现在看到的`
                : '这次拆解的剧本与分镜'}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {music?.token && (
              <Badge tone="idle">
                {`音轨 ${musicLabel} · ${fmtSeconds(music.duration ?? 0)}s · ${
                  music.role === 'bgm' ? '配乐' : 'MV'
                }`}
              </Badge>
            )}
            <Badge tone={plan?.live ? 'ok' : 'idle'}>{plan?.live ? '真实模型' : '离线引擎'}</Badge>
            {/* 拍摄实际会用的配置。先拆后拍时最容易在这里露馅：以前显示的是
                "离线引擎"，拍的时候却按接口默认值切到了真实模型。 */}
            {profileText && (
              <span title="这份拆解拍摄时会使用的配置">
                <Badge tone="idle">{`拍摄 ${profileText}`}</Badge>
              </span>
            )}
            {meta.last_preview_at && (
              <span title={`上次低清预览 ${meta.last_preview_at}（v${meta.last_preview_revision ?? 0}）`}>
                <Badge tone="idle">{`上次预览 ${meta.last_preview_at.slice(5)}`}</Badge>
              </span>
            )}
            {meta.last_shot_preview && (
              <span
                title={`上次单镜预览：镜 ${meta.last_shot_preview.shot} · ${meta.last_shot_preview.at}（v${meta.last_shot_preview.revision}）`}
              >
                <Badge tone="idle">{`上次单镜预览 镜${meta.last_shot_preview.shot}`}</Badge>
              </span>
            )}
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button
            small
            disabled={versions.length === 0}
            title={versions.length ? '查看并恢复之前的编辑版本' : '还没有可恢复的编辑版本'}
            onClick={() => setVersionsOpen((open) => !open)}
          >
            {`版本 ${versions.length}`}
          </Button>
          <Button small onClick={() => onNavigate('projects')}>
            回项目
          </Button>
          {/* 低清预览：半分辨率 + 绘制帧率封顶，先看时间轴/字幕/音轨对不对。
              产物在 preview/ 下，不覆盖正式成片，也不会把项目标成已拍。 */}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Button
              disabled={renderDisabled}
              title={
                outOfRange.length
                  ? '有镜头时长越界，先改回来'
                  : '半分辨率快速出一版，用于核对时间轴/字幕/音轨；不覆盖正式成片'
              }
              onClick={() => void startAction(onPreview, mixOpts())}
            >
              低清预览
            </Button>
            {/* 只重合成：改过镜长或顺序之后用它。一个像素都不重画，
                比「开始拍摄」快一个数量级——但画面不会变好，只会变长变短。 */}
            <Button
              disabled={recomposeDisabled}
              title={
                !meta.rendered
                  ? '这份拆解还没拍过，先拍一次'
                  : outOfRange.length
                    ? '有镜头时长越界，先改回来'
                    : `${cachedShots.length} 镜片段存在；开始后会逐镜校验是否与当前分镜一致，过期会明确拒绝`
              }
              onClick={() => void startAction(onRecompose, mixOpts())}
            >
              只重合成
            </Button>
            <Button
              variant="primary"
              disabled={renderDisabled}
              title={
                outOfRange.length
                  ? '有镜头时长越界，先改回来'
                  : `按这份分镜拍摄（${profileText || '离线 · 离线出图'}）`
              }
              onClick={() => void startAction(onRender, mixOpts())}
            >
              开始拍摄
            </Button>
          </div>
        </div>
      </div>

      {versionsOpen && versions.length > 0 && (
        <Panel className="mb-3 px-4 py-3">
          <div className="flex items-center justify-between">
            <span className="text-[12px] font-medium">编辑版本</span>
            <span className="font-mono text-[10.5px] text-ink-mute">当前 v{meta.revision ?? 0}</span>
          </div>
          <div className="mt-2 max-h-[220px] space-y-1 overflow-y-auto scroll-thin pr-1">
            {versions.map((version) => (
              <div
                key={version.revision}
                className="flex items-center justify-between gap-3 border-b border-[var(--ring)] py-1.5 last:border-b-0"
              >
                <span className="font-mono text-[11px]">v{version.revision}</span>
                <span className="min-w-0 flex-1 truncate text-[11px] text-ink-mute">
                  {version.at || '—'}
                  {version.restored_from != null ? ` · 恢复自 v${version.restored_from}` : ''}
                </span>
                <Button
                  small
                  disabled={busyVersion > 0}
                  onClick={() => void restore(version.revision)}
                >
                  {busyVersion === version.revision ? '恢复中…' : '恢复'}
                </Button>
              </div>
            ))}
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-ink-mute">
            恢复只替换剧本与分镜；音轨和拍摄配置保持不变，恢复后需要重新拍摄才能出片。
          </p>
        </Panel>
      )}

      {plan && (
        <Panel className="mb-3 px-4 py-3">
          <button
            type="button"
            onClick={() => setConfigOpen((open) => !open)}
            aria-expanded={configOpen}
            className="flex w-full flex-wrap items-center justify-between gap-2 text-left"
          >
            <span>
              <span className="text-[12px] font-medium">项目配置</span>
              <span className="ml-2 text-[10.5px] text-ink-mute">
                色板 · 角色 · 音轨 · 字幕 · 转场
              </span>
            </span>
            <span className="text-[11.5px] text-brand">{configOpen ? '收起' : '展开'}</span>
          </button>
          {!configOpen && (
            <p className="mt-1.5 text-[10.5px] leading-relaxed text-ink-mute">
              默认收起，先看分镜；展开后修改仍随拍摄/重合成保存。
            </p>
          )}
        </Panel>
      )}

      <div className={configOpen ? 'contents' : 'hidden'}>

      {plan && (
        <Panel className="mb-3 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[12px] font-medium">视觉色板</span>
            <span className="text-[10.5px] text-ink-mute">
              只改配色，不改题材与文案；保存后需整片重拍，只重合成不会重新上色
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {PALETTE_OPTIONS.map((item) => {
              const active = currentPalette === item.value
              return (
                <button
                  key={item.value}
                  type="button"
                  disabled={Boolean(paletteBusy)}
                  title={item.hint}
                  onClick={() => void changePalette(item.value)}
                  className="rounded-lg border px-2.5 py-1.5 text-left text-[11px] transition-colors"
                  style={{
                    borderColor: active
                      ? 'color-mix(in srgb, var(--color-brand) 60%, transparent)'
                      : 'var(--ring)',
                    background: active
                      ? 'color-mix(in srgb, var(--color-brand) 12%, transparent)'
                      : 'transparent',
                  }}
                >
                  <span className="flex items-center gap-1.5">
                    <span className="flex gap-0.5">
                      {(item.colors.length > 0 ? item.colors : ['#8A9094']).map((color) => (
                        <i
                          key={color}
                          className="h-2.5 w-2.5 rounded-full border border-white/20"
                          style={{ background: color }}
                        />
                      ))}
                    </span>
                    {paletteBusy === item.value ? '保存中…' : item.label}
                  </span>
                </button>
              )
            })}
          </div>
          <p className="mt-2 text-[10.5px] leading-relaxed text-ink-mute">
            {meta.rendered && (meta.last_render_profile?.palette ?? 'auto') !== currentPalette
              ? `当前成片按「${PALETTE_OPTIONS.find((item) => item.value === (meta.last_render_profile?.palette ?? 'auto'))?.label ?? '旧色板'}」渲染；整片重拍后才会应用「${PALETTE_OPTIONS.find((item) => item.value === currentPalette)?.label ?? currentPalette}」。`
              : '色板只影响画面配色，不改题材、文案与分镜；保存后需要整片重拍才会进入成片。'}
          </p>
          {paletteMsg && (
            <p className="mt-1.5 text-[10.5px] leading-relaxed text-ink-dim">{paletteMsg}</p>
          )}
        </Panel>
      )}

      {plan && (
        <Panel className="mb-3 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[12px] font-medium">镜头衔接与字幕动效</span>
            <span className="text-[10.5px] text-ink-mute">
              只影响合成，不重拍片段；随下一次拍摄/重合成保存
            </span>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-ink-mute">镜头转场</span>
            {TRANSITION_OPTIONS.map((item) => (
              <Button
                key={item.id}
                small
                variant={mix.transition === item.id ? 'primary' : undefined}
                title={item.hint}
                onClick={() => setMix((prev) => ({ ...prev, transition: item.id }))}
              >
                {item.label}
              </Button>
            ))}
            <label
              className="ml-2 flex items-center gap-2 text-[11px] text-ink-mute"
              title="字幕卡在各自起点用 0.2 秒淡入；固定标题层不参与"
            >
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-brand"
                checked={mix.text_anim}
                onChange={(event) => {
                  setMix((prev) => ({ ...prev, text_anim: event.target.checked }))
                }}
              />
              <span>字幕淡入</span>
            </label>
          </div>
          <p className="mt-2 text-[10.5px] leading-relaxed text-ink-mute">
            {mix.transition === 'none'
              ? '硬切直接拼接、不重编码，画面与旧版一致；字幕淡入只叠加图层，不改片段。'
              : `「${TRANSITION_OPTIONS.find((item) => item.id === mix.transition)?.label}」会让相邻镜头各让出约 0.2 秒做淡化，总时长与字幕时间轴不变；极短镜头会自动缩短转场。转场需要重编码，合成会比硬切慢。绘制后端不使用转场。`}
          </p>
        </Panel>
      )}

      {plan && (plan.subjects?.length ?? 0) > 0 && (
        <Panel className="mb-3 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[12px] font-medium">角色卡 · {plan.subjects.length} 个</span>
            <span className="max-w-[560px] text-right text-[10.5px] leading-relaxed text-ink-mute">
              参考图只作存档与人工对照，当前出图后端不读取；一致性靠「视觉锁定」文字注入提示词
            </span>
          </div>
          <div className="mt-3 grid gap-2 md:grid-cols-2">
            {plan.subjects.map((subject) => (
              <SubjectCard
                key={subject.id}
                runId={runId}
                subject={subject}
                shotIds={shots
                  .filter((shot) => (shot.subject_refs ?? []).includes(subject.id))
                  .map((shot) => shot.id)}
                busy={Boolean(busyAct)}
                rewriteBusy={rewriteBusy}
                onSave={(patch) => saveSubject(subject.id, patch)}
                onUpload={(file) => changeReference(subject.id, 'upload', file)}
                onRemove={() => changeReference(subject.id, 'remove')}
              />
            ))}
          </div>
          <p className="mt-2 text-[10.5px] leading-relaxed text-ink-mute">
            多个镜头引用同一角色时，用的是同一段锁定文字；参考图用来人工核对“像不像”。
            只改了锁定而没重拍的画面不会自动更新——要换画面请单镜重拍。
          </p>
        </Panel>
      )}

      {plan && (
        <Panel className="mb-3 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[12px] font-medium">音轨与字幕</span>
            <span className="text-[10.5px] text-ink-mute">
              随下一次拍摄/重合成保存；字幕偏移同时作用于外置 SRT 与最终画面字幕
            </span>
          </div>
          <div className="mt-3 flex flex-wrap items-end gap-5">
            <label className="block text-[11px] text-ink-mute">
              <span className="mb-1 block">人声电平</span>
              <span className="flex items-center gap-1">
                <input
                  type="number"
                  className="field-input w-[88px] text-[12px]"
                  min={0}
                  max={200}
                  step={5}
                  disabled={isMv}
                  value={Math.round(mix.voice_gain * 100)}
                  onChange={(event) => {
                    const pct = Number(event.target.value)
                    setMix((prev) => ({
                      ...prev,
                      voice_gain: Number.isFinite(pct) ? Math.max(0, Math.min(2, pct / 100)) : 0,
                    }))
                  }}
                />
                <span>%</span>
              </span>
            </label>
            <label className="block text-[11px] text-ink-mute">
              <span className="mb-1 block">音乐电平</span>
              <span className="flex items-center gap-1">
                <input
                  type="number"
                  className="field-input w-[88px] text-[12px]"
                  min={0}
                  max={200}
                  step={5}
                  disabled={!hasMusic}
                  value={Math.round(mix.music_gain * 100)}
                  onChange={(event) => {
                    const pct = Number(event.target.value)
                    setMix((prev) => ({
                      ...prev,
                      music_gain: Number.isFinite(pct) ? Math.max(0, Math.min(2, pct / 100)) : 0,
                    }))
                  }}
                />
                <span>%</span>
              </span>
            </label>
            <label
              className="flex items-center gap-2 pb-1 text-[11px] text-ink-mute"
              title="开启后，人声出现时自动把音乐压低约 12dB，人声一停音乐恢复"
            >
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-brand"
                checked={mix.voice_duck}
                disabled={isMv || !hasMusic}
                onChange={(event) => {
                  setMix((prev) => ({ ...prev, voice_duck: event.target.checked }))
                }}
              />
              <span>对白优先（音乐自动闪避）</span>
            </label>
            <label className="block text-[11px] text-ink-mute">
              <span className="flex items-center gap-1">
                <input
                  type="number"
                  className="field-input w-[88px] text-[12px]"
                  min={-10}
                  max={10}
                  step={0.1}
                  value={mix.subtitle_offset}
                  onChange={(event) => {
                    const seconds = Number(event.target.value)
                    setMix((prev) => ({
                      ...prev,
                      subtitle_offset: Number.isFinite(seconds)
                        ? Math.max(-10, Math.min(10, seconds))
                        : 0,
                    }))
                  }}
                />
                <span>秒</span>
              </span>
            </label>
            <span className="pb-1 text-[10.5px] leading-relaxed text-ink-mute">
              {isMv
                ? 'MV 模式没有配音，人声电平与对白优先不生效'
                : hasMusic
                  ? '配乐默认 28%；0% 即静音；对白优先在重新合成后生效'
                  : '当前没有音轨，音乐电平与对白优先不生效'}
            </span>
          </div>
        </Panel>
      )}

      {plan && hasMusic && (
        <Panel className="mb-3 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[12px] font-medium">波形与时间轴</span>
            <span className="font-mono text-[10px] text-ink-mute">
              {wave
                ? `${wave.name.replace(/\.[^.]+$/, '')} · ${fmtSeconds(wave.duration)}s · 峰值 ${Math.round(wave.peak * 100)}%`
                : waveError || '正在读取波形…'}
            </span>
          </div>
          {wave && (
            <div className="mt-2">
              <WaveformStrip
                wave={wave}
                lines={subLines}
                shots={shots}
                onAdjust={(index, patch) => patchLine(index, patch)}
              />
            </div>
          )}
          <p className="mt-1.5 text-[10.5px] leading-relaxed text-ink-mute">
            波形来自项目音轨；半透明块是当前字幕轨（含未保存改动），竖虚线是镜头起点。
            拖字幕块可整体移动，拖两端可改起止；松手后仍未保存，点「保存字幕」生效。
          </p>
        </Panel>
      )}

      {plan && (
        <Panel className="mb-3 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <button
              type="button"
              className="text-[12px] font-medium"
              onClick={() => setSubOpen((open) => !open)}
            >
              字幕轨 · {subLines.length} 行 {subOpen ? '收起' : '展开'}
            </button>
            <span className="min-w-0 text-right text-[10.5px] leading-relaxed text-ink-mute">
              {subDirty && <span className="mr-1.5 font-medium text-warn">有未保存改动</span>}
              {subtitles ? `来源：${SUBTITLE_SOURCE[subtitles.source] ?? subtitles.source}` : '还没有字幕轨'}
              {subtitles?.note ? ` · ${subtitles.note}` : ''}
            </span>
          </div>

          {subOpen && (
            <div className="mt-3">
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[10.5px] text-ink-mute">呈现方式</span>
                {([
                  ['burn', '烧录进画面'],
                  ['sidecar', '只出外置字幕'],
                  ['none', '关闭字幕'],
                ] as const).map(([value, label]) => (
                  <Button
                    key={value}
                    small
                    variant={subMode === value ? 'primary' : undefined}
                    disabled={subBusy}
                    onClick={() => {
                      setSubMode(value)
                      subDirtyRef.current = true
                      // 模式属于同一条字幕轨，立即排队保存；拍摄会等它落地。
                      void persistSubtitles(subLines, value)
                    }}
                  >
                    {label}
                  </Button>
                ))}
                <span className="text-[10.5px] leading-relaxed text-ink-mute">
                  点选立即保存；改字/拖时间轴要点「保存字幕」，再「只重合成」才会出现在成片里。
                </span>
              </div>
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[10.5px] text-ink-mute">整句样式</span>
                {subStylePresets.map((item) => (
                  <Button
                    key={item.id}
                    small
                    variant={subStyle?.id === item.id ? 'primary' : undefined}
                    disabled={subStyleBusy}
                    title={item.note}
                    onClick={() => void pickSubtitleStyle(item.id)}
                  >
                    <span
                      className="mr-1 inline-block h-2 w-2 rounded-full border border-black/30 align-middle"
                      style={{ backgroundColor: item.color }}
                    />
                    {item.label}
                  </Button>
                ))}
                {subStyleBusy && <span className="text-[10.5px] text-ink-mute">保存中…</span>}
              </div>
              <p className="mb-2 text-[10.5px] leading-relaxed text-ink-mute">
                换样式只影响重新合成，不重新出图；绘制后端的整句字幕板仍用引擎自带样式。
              </p>
              {subStyleCoverage && !subStyleCoverage.ok && (
                <p className="mb-2 text-[10.5px] leading-relaxed text-warn">
                  样式字体缺 {subStyleCoverage.missing_count} 个字符
                  {subStyleCoverage.fallback
                    ? `，渲染时会回退到 ${subStyleCoverage.fallback}`
                    : '，且没有可用回退字体，可能显示方块'}
                  {subStyleCoverage.uncovered.length > 0
                    ? `（${subStyleCoverage.uncovered.slice(0, 6).join(' ')} 仍缺字）`
                    : ''}
                </p>
              )}
              {subStyleMsg && <p className="mb-2 text-[10.5px] text-ink-dim">{subStyleMsg}</p>}
              <div className="max-h-[320px] space-y-1.5 overflow-y-auto scroll-thin pr-1">
                {subLines.map((line, index) => (
                  <div key={index} className="flex flex-wrap items-center gap-1.5">
                    <input
                      type="number"
                      step={0.1}
                      min={0}
                      className="field-input w-[76px] text-[11px]"
                      value={line.start}
                      onChange={(event) => {
                        const value = Number(event.target.value)
                        patchLine(index, { start: Number.isFinite(value) ? Math.max(0, value) : 0 })
                      }}
                    />
                    <span className="text-[11px] text-ink-mute">→</span>
                    <input
                      type="number"
                      step={0.1}
                      min={0}
                      className="field-input w-[76px] text-[11px]"
                      value={line.end}
                      onChange={(event) => {
                        const value = Number(event.target.value)
                        patchLine(index, { end: Number.isFinite(value) ? Math.max(0, value) : 0 })
                      }}
                    />
                    <input
                      className="field-input min-w-[140px] flex-1 text-[11px]"
                      maxLength={200}
                      value={line.text}
                      onChange={(event) => patchLine(index, { text: event.target.value })}
                    />
                    <Button
                      small
                      disabled={subBusy || Array.from(line.text).length < 2}
                      onClick={() => splitLine(index)}
                    >
                      拆分
                    </Button>
                    <Button
                      small
                      disabled={subBusy || index >= subLines.length - 1}
                      onClick={() => mergeLine(index)}
                    >
                      合并
                    </Button>
                    <Button small variant="danger" disabled={subBusy} onClick={() => dropLine(index)}>
                      删除
                    </Button>
                  </div>
                ))}
                {subLines.length === 0 && (
                  <p className="py-2 text-[11.5px] text-ink-mute">
                    没有字幕行。可以新增一行，或恢复自动生成。
                  </p>
                )}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Button small disabled={subBusy} onClick={addLine}>
                  新增一行
                </Button>
                <Button
                  small
                  variant="primary"
                  disabled={subBusy || !subDirty}
                  title={subDirty ? '把当前字幕轨写进项目' : '字幕轨与已保存的一致'}
                  onClick={() => void persistSubtitles(subLines)}
                >
                  {subBusy ? '保存中…' : subDirty ? '保存字幕' : '字幕已保存'}
                </Button>
                <Button small disabled={subBusy} onClick={() => void persistSubtitles(null)}>
                  恢复自动生成
                </Button>
              </div>
              <p className="mt-1.5 text-[10.5px] leading-relaxed text-ink-mute">
                保存后点「只重合成」生效，不重新出图。这里填的是叠加整体偏移之前的原始时间；
                删除全部行再保存表示明确不要字幕。直接点拍摄/预览也会先自动保存这份字幕轨。
              </p>
              {subMsg && <p className="mt-1 text-[11px] text-ink-dim">{subMsg}</p>}
            </div>
          )}
        </Panel>
      )}

      {!runId && (
        <Panel className="px-6 py-14 text-center">
          <p className="text-[14px] font-medium">还没有拆解</p>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">回创作台写一句话，点「先拆解」</p>
          <div className="mt-4 flex justify-center">
            <Button onClick={() => onNavigate('create')}>去创作台</Button>
          </div>
        </Panel>
      )}

      {loading && <p className="py-12 text-center text-[12.5px] text-ink-mute">正在读拆解…</p>}

      {(error || notice) && (
        <Panel className="mb-3 px-4 py-2.5">
          {error ? (
            <p className="whitespace-pre-line text-[12.5px] leading-relaxed text-err">{error}</p>
          ) : (
            <p className="text-[12.5px] leading-relaxed text-ink-dim">{notice}</p>
          )}
        </Panel>
      )}

      {outOfRange.length > 0 && (
        <Panel className="mb-3 px-4 py-2.5">
          <p className="text-[12.5px] leading-relaxed text-warn">
            有 {outOfRange.length} 个镜头的时长不在 {MIN_SHOT}–{MAX_SHOT} 秒之间（
            {outOfRange.map((shot) => `镜 ${shot.id}：${fmtSeconds(shot.duration)}s`).join('、')}
            ），不能拍摄。
          </p>
        </Panel>
      )}

      </div>

      {plan && (
        <div className="grid items-start gap-3 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
          <Panel className="order-2 px-5 py-4 lg:order-1">
            <p className="text-[15px] font-semibold">{plan.script?.title || '未命名'}</p>
            <p className="mt-1 text-[12.5px] leading-relaxed text-ink-dim">{plan.script?.logline}</p>

            <div className="mt-3 space-y-1">
              {[
                ['基调', plan.script?.tone],
                ['受众', plan.script?.target_audience],
                ['钩子', plan.script?.hook],
              ]
                .filter(([, value]) => value)
                .map(([label, value]) => (
                  <div key={label} className="flex gap-2 text-[11.5px] leading-relaxed">
                    <span className="shrink-0 text-ink-mute">{label}</span>
                    <span className="min-w-0 text-ink-dim">{value}</span>
                  </div>
                ))}
            </div>

            <div className="mt-4 space-y-2">
              {acts.map((act, index) => (
                <ActCard
                  key={`${act.name}-${index}`}
                  act={act}
                  index={index}
                  shotCount={shots.filter((shot) => shot.act === act.name).length}
                  staleCount={
                    shots.filter(
                      (shot) => shot.act === act.name && staleShots.includes(shot.id),
                    ).length
                  }
                  busy={busyAct === act.name}
                  locked={rewriteBusy}
                  onSave={(next) => saveAct(next, index)}
                  onReplan={replan}
                />
              ))}
            </div>
            <p className="mt-3 text-[11px] leading-relaxed text-ink-mute">
              改场次文字不会动已经拆好的分镜，只把受影响的镜头标出来。
              要按新文字重拆，就在那一场点「重拆这一场」——只重拆这一场，
              别的场次一个镜头都不碰。
            </p>
          </Panel>

          <Panel className="order-1 px-5 py-4 lg:order-2">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[13px] font-medium">分镜</span>
              <span className="font-mono text-[10.5px] text-ink-mute">
                {shots.length} 镜 · {fmtSeconds(total)}s
              </span>
            </div>

            <div>
              {shots.map((shot, index) => (
                <ShotEditor
                  key={shot.id}
                  shot={shot}
                  edited={editedShots.includes(shot.id)}
                  stale={staleShots.includes(shot.id)}
                  cached={cachedShots.includes(shot.id)}
                  locked={rewriteBusy}
                  onSave={saveShot}
                  // 没拍过就没有"上次的片段"可复用，重拍一镜会失败。
                  // 在按钮上先拦住，别让用户点了才看到报错。
                  canReshoot={Boolean(meta.rendered) && !rewriteBusy}
                  onReshoot={(id) =>
                    void startAction((rid, opts) => onReshoot(rid, id, opts), mixOpts())
                  }
                  canPreview={!rewriteBusy}
                  onPreviewShot={(id) =>
                    void startAction((rid, opts) => onPreviewShot(rid, id, opts), mixOpts())
                  }
                  onMove={(id, dir) => void moveShot(id, dir)}
                  canMoveUp={index > 0 && !rewriteBusy}
                  canMoveDown={index < shots.length - 1 && !rewriteBusy}
                />
              ))}
            </div>

            <div className="mt-4 flex items-center justify-between gap-3 border-t border-[var(--ring)] pt-3">
              <span className="text-[11.5px] leading-relaxed text-ink-mute">
                {pendingSaves > 0
                  ? `正在保存 ${pendingSaves} 处修改… `
                  : editedShots.length || meta.edited?.script
                    ? `已改：${[
                        meta.edited?.script ? '剧本' : '',
                        editedShots.length ? `${editedShots.length} 个镜头` : '',
                      ]
                        .filter(Boolean)
                        .join(' · ')}　·　已保存`
                    : '时长与文字都可以直接改，失焦即保存'}
                {cachedShots.length > 0 &&
                  `　·　已有 ${cachedShots.length} 镜片段；「只重合成」会逐镜校验指纹，输入变过的镜头会明确要求重拍`}
              </span>
              <div className="flex shrink-0 items-center gap-2">
                <Button
                  disabled={renderDisabled}
                  title={
                    outOfRange.length
                      ? '有镜头时长越界，先改回来'
                      : '半分辨率快速出一版，用于核对时间轴/字幕/音轨'
                  }
                  onClick={() => void startAction(onPreview, mixOpts())}
                >
                  低清预览
                </Button>
                <Button
                  disabled={recomposeDisabled}
                  title={
                    !meta.rendered
                      ? '这份拆解还没拍过，先拍一次'
                      : `${cachedShots.length} 镜片段存在；开始后会逐镜校验是否与当前分镜一致，过期会明确拒绝`
                  }
                  onClick={() => void startAction(onRecompose, mixOpts())}
                >
                  只重合成
                </Button>
                <Button
                  variant="primary"
                  disabled={renderDisabled}
                  title={profileText ? `拍摄配置：${profileText}` : undefined}
                  onClick={() => void startAction(onRender, mixOpts())}
                >
                  开始拍摄
                </Button>
              </div>
            </div>
          </Panel>
        </div>
      )}

      {plan && (plan.notes ?? []).length > 0 && (
        <Panel className="mt-3 px-5 py-3">
          <p className="text-[11.5px] font-medium text-warn">拆解时的降级与提示</p>
          <ul className="mt-1 space-y-0.5">
            {(plan.notes ?? []).map((note, index) => (
              <li key={index} className="text-[11.5px] leading-relaxed text-ink-dim">
                · {note}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  )
}
