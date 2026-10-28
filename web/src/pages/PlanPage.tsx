import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  ApiError,
  fetchPlan,
  patchScript,
  patchShot,
  replanAct,
  reorderShots,
  type PlanAct,
  type PlanMeta,
  type PlanResult,
  type PlanShot,
  type ShotPatch,
} from '../lib/api'
import { Badge, Button, Panel } from '../components/ui'

/** 与 `pipeline/models.py` 的 MIN/MAX_SHOT_SECONDS 一致。改这里也要改那边。 */
const MIN_SHOT = 2
const MAX_SHOT = 10

function fmtSeconds(value: number): string {
  return (Math.round(value * 10) / 10).toFixed(1)
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
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
  onSave,
  onReplan,
}: {
  act: PlanAct
  index: number
  shotCount: number
  staleCount: number
  busy: boolean
  onSave: (next: PlanAct) => Promise<boolean>
  onReplan: (actName: string, force: boolean) => Promise<ReplanOutcome>
}) {
  const text = String(act.content ?? act.summary ?? '')
  const [draft, setDraft] = useState(text)
  const [confirming, setConfirming] = useState('')
  const [error, setError] = useState('')

  useEffect(() => setDraft(String(act.content ?? act.summary ?? '')), [act.content, act.summary])

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
        className="field-input mt-1.5 min-h-[46px] w-full resize-y text-[11.5px] leading-relaxed"
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
      />

      {showReplan && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <Button small disabled={busy} onClick={() => void run(false)}>
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
            <Button small variant="primary" disabled={busy} onClick={() => void run(true)}>
              仍然重拆
            </Button>
            <Button small disabled={busy} onClick={() => setConfirming('')}>
              取消
            </Button>
          </div>
        </div>
      )}

      {error && <p className="mt-1.5 text-[11px] leading-relaxed text-err">{error}</p>}
    </div>
  )
}

/** 一镜。四个可改字段各自失焦保存，改哪格存哪格。 */
function ShotEditor({
  shot,
  edited,
  stale,
  onSave,
  onReshoot,
  canReshoot,
  onMove,
  canMoveUp,
  canMoveDown,
}: {
  shot: PlanShot
  edited: boolean
  stale: boolean
  onSave: (id: number, patch: ShotPatch) => Promise<boolean>
  onReshoot: (id: number) => void
  canReshoot: boolean
  /** 上移 / 下移一位。只改顺序，镜号与时长都不动。 */
  onMove: (id: number, dir: -1 | 1) => void
  canMoveUp: boolean
  canMoveDown: boolean
}) {
  const [duration, setDuration] = useState(fmtSeconds(shot.duration))
  const [visual, setVisual] = useState(shot.visual)
  const [camera, setCamera] = useState(shot.camera)
  const [narration, setNarration] = useState(shot.narration)

  useEffect(() => {
    setDuration(fmtSeconds(shot.duration))
    setVisual(shot.visual)
    setCamera(shot.camera)
    setNarration(shot.narration)
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
          <span className="inline-flex items-center gap-1">
            <span className="inline-block w-[52px]">
              <input
                value={duration}
                inputMode="decimal"
                className="field-input w-full px-1 py-0.5 text-center font-mono text-[11px] tabular-nums"
                onChange={(event) => setDuration(event.target.value)}
                onBlur={() => void commitDuration()}
              />
            </span>
            <span className="text-[10.5px] text-ink-mute">秒</span>
          </span>
          {edited && <Badge tone="live">改过</Badge>}
          {stale && <Badge tone="warn">剧本已改，待确认</Badge>}
          {/* 只重拍这一镜。其余镜头沿用上次的片段，所以它比整片重拍快得多，
              但成片会重新合成——按钮的 title 里把这两件事都说清楚。 */}
          <Button
            small
            className="ml-auto"
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
          placeholder="画面"
          className="field-input mt-1.5 min-h-[40px] w-full resize-y text-[12.5px] leading-relaxed"
          onChange={(event) => setVisual(event.target.value)}
          onBlur={() => void commitText('visual', visual)}
        />

        <div className="mt-1.5 flex gap-2">
          {/* 定宽要包在外层：`.field-input` 自带 width:100%，直接写 w-[132px]
              会被它盖掉，结果把台词框挤成一条缝。 */}
          <div className="w-[136px] shrink-0">
            <input
              value={camera}
              placeholder="机位"
              className="field-input w-full px-2 py-1 text-[11.5px]"
              onChange={(event) => setCamera(event.target.value)}
              onBlur={() => void commitText('camera', camera)}
            />
          </div>
          <div className="min-w-0 flex-1">
            <input
              value={narration}
              placeholder="台词"
              className="field-input w-full px-2 py-1 text-[11.5px]"
              onChange={(event) => setNarration(event.target.value)}
              onBlur={() => void commitText('narration', narration)}
            />
          </div>
        </div>

        {/* 默认只给中文（用户写什么就看到什么），实际发给模型的 prompt 折起来。
            为什么要留入口：这套东西的卖点之一是"可追溯"——出图不对时，
            用户能自己看到底把哪句话、加了哪些修饰词发了出去，而不是只能猜。 */}
        {shot.prompt_image && (
          <details className="mt-1.5">
            <summary className="cursor-pointer select-none text-[11px] text-ink-mute transition-colors hover:text-ink-dim">
              查看发给模型的原文
            </summary>
            <pre className="mt-1 whitespace-pre-wrap break-all rounded-lg bg-[var(--sunk)] px-2 py-1.5 font-mono text-[10.5px] leading-relaxed text-ink-dim">
              {shot.prompt_image}
            </pre>
          </details>
        )}
      </div>
    </div>
  )
}

export function PlanPage({
  runId,
  onNavigate,
  onRender,
  onReshoot,
  onRecompose,
}: {
  runId: string
  onNavigate: (next: string) => void
  onRender: (runId: string) => void
  onReshoot: (runId: string, shotId: number) => void
  onRecompose: (runId: string) => void
}) {
  const [plan, setPlan] = useState<PlanResult | null>(null)
  const [meta, setMeta] = useState<PlanMeta>({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  /** 正在重拆的那一场。重拆期间锁住拍摄按钮——否则会拿旧分镜出片。 */
  const [busyAct, setBusyAct] = useState('')

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
        setError('')
      })
      .catch((err: unknown) => setError(errText(err) || '读不到这次拆解'))
      .finally(() => setLoading(false))
  }, [runId])

  const shots = plan?.shots ?? []
  const acts = plan?.script?.acts ?? []
  const total = useMemo(() => shots.reduce((sum, shot) => sum + shot.duration, 0), [shots])
  const editedShots = meta.edited?.shots ?? []
  const staleShots = meta.stale_shots ?? []
  const outOfRange = shots.filter((shot) => shot.duration < MIN_SHOT || shot.duration > MAX_SHOT)

  const saveShot = useCallback(
    async (id: number, patch: ShotPatch): Promise<boolean> => {
      try {
        const res = await patchShot(runId, id, patch)
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
        if (res.meta) setMeta(res.meta)
        setNotice(res.note)
        setError('')
        return true
      } catch (err) {
        setError(errText(err))
        setNotice('')
        return false
      }
    },
    [runId],
  )

  const saveAct = useCallback(
    async (act: PlanAct, index: number): Promise<boolean> => {
      const next = acts.map((item, i) => (i === index ? act : item))
      try {
        const res = await patchScript(runId, { acts: next })
        setPlan((prev) => (prev ? { ...prev, script: res.script } : prev))
        setMeta(res.meta)
        setNotice(res.note)
        setError('')
        return true
      } catch (err) {
        setError(errText(err))
        setNotice('')
        return false
      }
    },
    [acts, runId],
  )

  /** 挪一位。发的是整条顺序——后端只接受完整置换，缺项一律拒，
   *  免得"悄悄少了一镜"这种最难查的丢失。 */
  const moveShot = useCallback(
    async (id: number, dir: -1 | 1) => {
      const order = shots.map((shot) => shot.id)
      const from = order.indexOf(id)
      const to = from + dir
      if (from < 0 || to < 0 || to >= order.length) return
      order.splice(from, 1)
      order.splice(to, 0, id)
      try {
        const res = await reorderShots(runId, order)
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
        if (res.meta) setMeta(res.meta)
        setNotice(res.note)
        setError('')
      } catch (err) {
        setError(errText(err))
        setNotice('')
      }
    },
    [runId, shots],
  )

  const replan = useCallback(
    async (actName: string, force: boolean): Promise<ReplanOutcome> => {
      if (!actName) return { kind: 'error', detail: '这一场没有场次名，无法重拆' }
      setBusyAct(actName)
      try {
        const res = await replanAct(runId, actName, force)
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
    [runId],
  )

  const renderDisabled = !plan || outOfRange.length > 0 || Boolean(busyAct)

  // 只重合成：拍过才有片段可重拼；时长越界也不能拼（会拼出一段不该有的长度）。
  const recomposeDisabled =
    !plan || !meta.rendered || outOfRange.length > 0 || Boolean(busyAct)

  // 音轨徽章。用户从创作台跳过来之后，这是唯一能确认"我刚才选的 MV 还在"的地方——
  // 拍摄阶段靠 `meta.music` 回落，界面上看不见就没人知道它还生效。
  const music = plan?.music
  const musicName = (music?.name || '').replace(/\.[^.]+$/, '')
  const musicLabel = musicName.length > 12 ? `${musicName.slice(0, 12)}…` : musicName

  return (
    <div className="mx-auto w-full max-w-[1340px] px-6 pt-7 pb-16">
      <div className="mb-5 flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">拆解</h2>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">
            {plan
              ? `${acts.length} 场 · ${shots.length} 镜 · ${fmtSeconds(total)} 秒　—　改完再拍，拍的就是你现在看到的`
              : '这次拆解的剧本与分镜'}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {music?.token && (
            <Badge tone="idle">
              {`音轨 ${musicLabel} · ${fmtSeconds(music.duration ?? 0)}s · ${
                music.role === 'bgm' ? '配乐' : 'MV'
              }`}
            </Badge>
          )}
          <Badge tone={plan?.live ? 'ok' : 'idle'}>{plan?.live ? '真实模型' : '离线引擎'}</Badge>
          <Button small onClick={() => onNavigate('create')}>
            回创作台
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
                  : '按现在的镜长与顺序重拼一遍，不重新出图'
            }
            onClick={() => plan && onRecompose(plan.run_id || runId)}
          >
            只重合成
          </Button>
          <Button
            variant="primary"
            disabled={renderDisabled}
            title={outOfRange.length ? '有镜头时长越界，先改回来' : '按这份分镜拍摄'}
            onClick={() => plan && onRender(plan.run_id || runId)}
          >
            开始拍摄
          </Button>
        </div>
      </div>

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

      {plan && (
        <div className="grid items-start gap-3 xl:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
          <Panel className="px-5 py-4">
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

          <Panel className="px-5 py-4">
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
                  onSave={saveShot}
                  // 没拍过就没有"上次的片段"可复用，重拍一镜会失败。
                  // 在按钮上先拦住，别让用户点了才看到报错。
                  canReshoot={Boolean(meta.rendered) && !busyAct}
                  onReshoot={(id) => onReshoot(plan?.run_id || runId, id)}
                  onMove={(id, dir) => void moveShot(id, dir)}
                  canMoveUp={index > 0 && !busyAct}
                  canMoveDown={index < shots.length - 1 && !busyAct}
                />
              ))}
            </div>

            <div className="mt-4 flex items-center justify-between gap-3 border-t border-[var(--ring)] pt-3">
              <span className="text-[11.5px] leading-relaxed text-ink-mute">
                {editedShots.length || meta.edited?.script
                  ? `已改：${[
                      meta.edited?.script ? '剧本' : '',
                      editedShots.length ? `${editedShots.length} 个镜头` : '',
                    ]
                      .filter(Boolean)
                      .join(' · ')}`
                  : '时长与文字都可以直接改，失焦即保存'}
              </span>
              <div className="flex shrink-0 items-center gap-2">
                <Button
                  disabled={recomposeDisabled}
                  title={
                    !meta.rendered
                      ? '这份拆解还没拍过，先拍一次'
                      : '按现在的镜长与顺序重拼一遍，不重新出图'
                  }
                  onClick={() => plan && onRecompose(plan.run_id || runId)}
                >
                  只重合成
                </Button>
                <Button variant="primary" disabled={renderDisabled} onClick={() => plan && onRender(plan.run_id || runId)}>
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
