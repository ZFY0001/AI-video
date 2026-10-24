import { useMemo, useRef, useState } from 'react'

import { LogStream } from '../components/LogStream'
import {
  Badge,
  Button,
  EtaRow,
  Panel,
  PipelineBeam,
  SectionTitle,
  Segmented,
  SettingRow,
  Switch,
} from '../components/ui'
import type { UseRun } from '../hooks/useRun'
import {
  estimatePaintedSeconds,
  humanDuration,
  createPlan,
  uploadAudio,
  videoUrl,
  type AudioTrack,
  type FormState,
  type Meta,
  type MusicMode,
  type Preset,
} from '../lib/api'

const DEFAULT_FORM: FormState = {
  theme: '给我的家乡方城烩面做一条 15 秒宣传片',
  duration: 15,
  aspect: '16:9',
  style: '烟火气、暖色调、纪实风',
  shot_len: 5,
  max_shots: null,
  live: false,
  guides: false,
  no_cache: false,
  painted: false,
  painted_fps: 24,
  painted_quality: 'medium',
  clean_frames: false,
  // 默认「必须真图」：此前默认允许占位帧兜底，ComfyUI 没开时会静默出一条
  // 色块片、界面还显示成功——用户会以为是自己提示词写错了。
  // 验收链路（不要真画面）时取消勾选即可。
  require_real_image: true,
  mode: 'narration',
  audio: '',
  audio_name: '',
  audio_duration: 0,
  lyrics: '',
  bpm: 0,
  beat_offset: 0,
  auto_beats: true,
}

const MODE_OPTIONS: { value: MusicMode; label: string; hint: string }[] = [
  { value: 'narration', label: '解说', hint: '一句话进，中文配音，字幕即解说词' },
  { value: 'song', label: 'MV', hint: '总时长以音频为准，不出配音，歌词逐字上屏' },
  { value: 'bgm', label: '配乐', hint: '保留配音解说，音乐压低垫底，短了循环铺满' },
]

/** 超过这个镜数就提醒一句。45 镜 × 24fps × 0.7s ≈ 一小时。 */
const SHOT_WARN = 40

const QUALITY_OPTIONS = [
  { value: 'fast' as const, label: '快', hint: '约 0.55 秒/帧' },
  { value: 'medium' as const, label: '均衡', hint: '约 0.70 秒/帧（默认）' },
  { value: 'high' as const, label: '精细', hint: '约 0.76 秒/帧' },
]

function audioLabel(source: string, hasAudio: boolean): string {
  if (!hasAudio || source === 'silent') return '静音轨'
  if (source === 'music') return '音乐'
  if (source === 'music+voice') return '配音 + 音乐'
  return '配音'
}

function mb(size: number): string {
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function clock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`
}

export function CreatePage({
  meta,
  run,
  onPlan,
}: {
  meta: Meta | null
  run: UseRun
  onPlan?: (runId: string) => void
}) {
  const { phase, jobId, stage, pct, logs, env, result, error, start, cancel, reset } = run
  const [form, setForm] = useState<FormState>(DEFAULT_FORM)
  const [presetIndex, setPresetIndex] = useState(0)
  const [planning, setPlanning] = useState(false)
  const [planError, setPlanError] = useState('')
  const [track, setTrack] = useState<AudioTrack | null>(null)
  const [audioBusy, setAudioBusy] = useState(false)
  const [audioError, setAudioError] = useState('')
  const [dragging, setDragging] = useState(false)
  const audioInput = useRef<HTMLInputElement>(null)
  const lyricsInput = useRef<HTMLInputElement>(null)
  /** 切进 MV 之前的时长。切回解说短片时还给他——不然一次上传会把 15 秒改成 222 秒。 */
  const preMusicDuration = useRef(DEFAULT_FORM.duration)

  const presets = meta?.presets ?? []
  const patch = (next: Partial<FormState>) => setForm((prev) => ({ ...prev, ...next }))

  const isMusic = form.mode !== 'narration'

  const switchMode = (mode: MusicMode) => {
    if (mode === form.mode) return
    if (mode === 'narration') {
      patch({ mode, duration: preMusicDuration.current })
      return
    }
    if (form.mode === 'narration') preMusicDuration.current = form.duration
    patch({ mode })
    // 音乐 MV 的总时长归音频管；配乐模式仍以解说为准，不动 duration。
    if (mode === 'song' && track) patch({ duration: Math.max(4, Math.round(track.duration)) })
  }

  const applyAudio = async (file: File) => {
    setAudioBusy(true)
    setAudioError('')
    try {
      const next = await uploadAudio(file)
      setTrack(next)
      patch({
        audio: next.token,
        audio_name: next.name,
        audio_duration: next.duration,
        ...(form.mode === 'song' ? { duration: Math.max(4, Math.round(next.duration)) } : {}),
      })
    } catch (err) {
      setAudioError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setAudioBusy(false)
    }
  }

  const clearAudio = () => {
    setTrack(null)
    setAudioError('')
    patch({ audio: '', audio_name: '', audio_duration: 0 })
  }

  /** 歌词在浏览器里读，不上传：纯文本几 KB，走一趟服务端只是多一个接口；
   * 而且读进 textarea 之后用户还能改，上传就没法改了。 */
  const readLyrics = (file: File) => {
    const reader = new FileReader()
    reader.onload = () => patch({ lyrics: String(reader.result ?? '') })
    reader.onerror = () => setAudioError('歌词文件读取失败')
    reader.readAsText(file, 'utf-8')
  }

  const nextPreset = () => {
    if (presets.length === 0) return
    const index = (presetIndex + 1) % presets.length
    const preset: Preset = presets[index]
    setPresetIndex(index)
    patch({ theme: preset.theme, duration: preset.duration, aspect: preset.aspect, style: preset.style })
  }

  const estimate = useMemo(
    () => estimatePaintedSeconds(form.duration, form.painted_fps, form.painted_quality),
    [form.duration, form.painted_fps, form.painted_quality],
  )

  const running = phase === 'running'
  const percent = Math.round(pct * 100)
  // MV / 配乐没有音轨就不许发：后端会把 music_role 忽略掉、当成解说模式，
  // 用户拿到一条没有音乐的片子却看不到任何报错。
  const missingAudio = isMusic && !form.audio
  const canStart = form.theme.trim().length > 0 && !running && !missingAudio
  /** 按镜长估个镜数，只用于提示代价，不参与任何计算。 */
  const shotEstimate = Math.max(1, Math.ceil(form.duration / Math.max(2, form.shot_len)))
  const lyricsLines = form.lyrics.split('\n').filter((line) => line.trim()).length

  const statusTone = phase === 'running' ? 'live' : phase === 'done' ? 'ok' : phase === 'error' ? 'bad' : 'idle'
  const statusText =
    phase === 'running' ? '运行中' : phase === 'done' ? '已产出' : phase === 'error' ? '已中断' : '待命'
  const statusHint =
    phase === 'running'
      ? '管线在工作线程里跑，可以随时停止'
      : phase === 'done'
        ? '成片已落盘，可直接播放或下载'
        : phase === 'error'
          ? '看日志定位原因，修好后重跑'
          : '写好一句话，剩下的交给管线'

  const compose = result?.compose

  return (
    <div className="mx-auto w-full max-w-[1340px] px-6 pt-7 pb-16">
      <div className="mb-5 flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">创作台</h2>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">
            写一句话，剩下的交给管线。中途可以停，已完成的镜头照常进成片。
          </p>
        </div>
        <span className="shrink-0 font-mono text-[10.5px] tracking-[0.06em] text-ink-mute">
          本地运行 · 不出网
        </span>
      </div>
      <div className="grid items-stretch gap-4 xl:grid-cols-[minmax(0,1.12fr)_minmax(0,0.88fr)]">
        {/* ------------------------------------------------ 输入 */}
        <div className="flex flex-col gap-3">
          <Panel className="px-5 py-4">
            <div className="flex items-center justify-between gap-3">
              <h3 className="text-[15px] font-semibold">内容</h3>
              <button
                type="button"
                onClick={nextPreset}
                disabled={presets.length === 0 || running}
                className="text-[12.5px] text-brand transition-opacity disabled:opacity-40"
              >
                载入示例
              </button>
            </div>
            <textarea
              value={form.theme}
              onChange={(event) => patch({ theme: event.target.value })}
              placeholder="例如：城市夜景宣传片，电影感冷色调"
              rows={3}
              disabled={running}
              className="field-input mt-3 min-h-[82px] text-[13.5px] leading-[1.7]"
            />
            <p className="mt-2 text-[11.5px] text-ink-mute">写清题材、时长与气质，剩下的交给管线</p>
          </Panel>

          <Panel className="px-5 py-0">
            <SettingRow
              label="模式"
              sub={form.mode === 'narration' ? '中文配音' : form.mode === 'song' ? '音乐即音轨' : '音乐垫底'}
            >
              <Segmented
                options={MODE_OPTIONS}
                value={form.mode}
                onChange={switchMode}
                disabled={running}
              />
            </SettingRow>

            <SettingRow label="时长" sub={form.mode === 'song' ? '以音频为准' : '总秒数'}>
              {form.mode === 'song' ? (
                // 只读不是摆设：MV 的总时长归音频管，让这里还能改，
                // 用户会以为填的 15 秒能把一首 3 分 42 秒的歌压成 15 秒。
                <span className="font-mono text-[12.5px] tabular-nums text-ink-dim">
                  {form.audio_duration > 0 ? `${form.audio_duration.toFixed(1)} 秒` : '等音轨'}
                </span>
              ) : (
                <div className="flex items-center gap-1.5">
                  <input
                    type="number"
                    min={5}
                    max={600}
                    step={5}
                    value={form.duration}
                    disabled={running}
                    onChange={(event) => patch({ duration: Number(event.target.value) || 5 })}
                    className="field-input w-[62px] text-center tabular-nums"
                  />
                  <div className="seg">
                    {[15, 30, 60].map((value) => (
                      <button
                        key={value}
                        type="button"
                        data-active={form.duration === value}
                        disabled={running}
                        onClick={() => patch({ duration: value })}
                      >
                        {value}s
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </SettingRow>

            <SettingRow
              label="必须真图"
              sub={form.require_real_image ? 'ComfyUI 不可用就报错' : '允许占位帧兜底'}
            >
              <Switch
                checked={form.require_real_image}
                onChange={(value) => patch({ require_real_image: value })}
                label="必须真图"
                disabled={running}
              />
            </SettingRow>

            <SettingRow label="画幅" sub="出片比例">
              <Segmented
                options={(meta?.aspects ?? ['16:9', '9:16', '1:1']).map((value) => ({
                  value,
                  label: value,
                }))}
                value={form.aspect}
                onChange={(value) => patch({ aspect: value })}
                disabled={running}
              />
            </SettingRow>

            <SettingRow label="镜头时长" sub={`${form.shot_len.toFixed(1)} 秒 / 镜`}>
              <div className="flex w-[186px] items-center gap-2">
                <input
                  type="range"
                  min={2}
                  max={10}
                  step={0.5}
                  value={form.shot_len}
                  disabled={running}
                  onChange={(event) => patch({ shot_len: Number(event.target.value) })}
                />
                <span className="w-8 shrink-0 text-right font-mono text-[11.5px] tabular-nums text-ink-dim">
                  {form.shot_len.toFixed(1)}
                </span>
              </div>
            </SettingRow>

            <SettingRow label="风格" sub="影响出图与配音">
              <input
                value={form.style}
                onChange={(event) => patch({ style: event.target.value })}
                placeholder="烟火气、暖色调、纪实风"
                disabled={running}
                className="field-input w-[186px]"
              />
            </SettingRow>

            <SettingRow label="画质" sub={form.painted ? '三档实测速度' : '仅代码绘制时生效'}>
              <Segmented
                options={QUALITY_OPTIONS.map(({ value, label, hint }) => ({ value, label, hint }))}
                value={form.painted_quality}
                onChange={(value) => patch({ painted_quality: value })}
                disabled={running || !form.painted}
              />
            </SettingRow>

            <SettingRow label="帧率" sub={form.painted ? '低帧率换时间' : '仅代码绘制时生效'}>
              <div className="seg">
                {[12, 18, 24].map((fps) => (
                  <button
                    key={fps}
                    type="button"
                    data-active={form.painted_fps === fps}
                    disabled={running || !form.painted}
                    onClick={() => patch({ painted_fps: fps })}
                  >
                    {fps}
                  </button>
                ))}
              </div>
            </SettingRow>

            <SettingRow
              label="代码绘制"
              sub={
                form.painted
                  ? `${Math.round(form.duration * form.painted_fps)} 帧 · 约 ${humanDuration(estimate)}`
                  : '不用显卡，逐帧画水彩'
              }
            >
              <Switch
                checked={form.painted}
                onChange={(value) => patch({ painted: value })}
                label="代码绘制"
                disabled={running}
              />
            </SettingRow>

            <SettingRow label="真实模型" sub={env?.live_ready ? '已配 Key' : '未配 Key 时走离线'}>
              <Switch
                checked={form.live}
                onChange={(value) => patch({ live: value })}
                label="真实模型"
                disabled={running}
              />
            </SettingRow>

            <SettingRow label="三分线参考" sub="出图时叠构图辅助线">
              <Switch
                checked={form.guides}
                onChange={(value) => patch({ guides: value })}
                label="三分线参考"
                disabled={running}
              />
            </SettingRow>
          </Panel>

          {isMusic && (
            <Panel className="px-5 py-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="text-[15px] font-semibold">音轨与歌词</h3>
                  <p className="mt-1 text-[11.5px] leading-relaxed text-ink-mute">
                    {form.mode === 'song'
                      ? '音乐就是成片的音轨，不再生成配音；歌词逐字上屏'
                      : '保留配音解说，音乐压低垫底；短了会自动循环铺满'}
                  </p>
                </div>
                {track && (
                  <button
                    type="button"
                    onClick={clearAudio}
                    disabled={running || audioBusy}
                    className="shrink-0 text-[12.5px] text-ink-dim transition-colors hover:text-ink disabled:opacity-40"
                  >
                    移除
                  </button>
                )}
              </div>

              <input
                ref={audioInput}
                type="file"
                accept="audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus"
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  if (file) void applyAudio(file)
                  // 清空 value，否则同一个文件选第二次不触发 change——
                  // "替换"点回去选原来那首就毫无反应。
                  event.target.value = ''
                }}
              />

              {track ? (
                <div className="mt-3 flex items-center justify-between gap-3 rounded-xl border border-[var(--ctl-edge)] bg-[var(--sunk)] px-3.5 py-2.5">
                  <div className="min-w-0">
                    <p className="truncate text-[13px]" title={track.name}>
                      {track.name}
                    </p>
                    <p className="mt-0.5 font-mono text-[11px] tabular-nums text-ink-mute">
                      {clock(track.duration)} · {mb(track.size)} · {track.format}
                    </p>
                  </div>
                  <Button small disabled={running || audioBusy} onClick={() => audioInput.current?.click()}>
                    替换
                  </Button>
                </div>
              ) : (
                <button
                  type="button"
                  disabled={running || audioBusy}
                  onClick={() => audioInput.current?.click()}
                  onDragOver={(event) => {
                    event.preventDefault()
                    if (!running) setDragging(true)
                  }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={(event) => {
                    event.preventDefault()
                    setDragging(false)
                    const file = event.dataTransfer.files?.[0]
                    if (file && !running) void applyAudio(file)
                  }}
                  className={[
                    'mt-3 flex w-full flex-col items-center justify-center rounded-xl border border-dashed px-4 py-6 transition-colors',
                    dragging
                      ? 'border-brand bg-brand-soft/50'
                      : 'border-[var(--ctl-edge)] bg-[var(--sunk)] hover:border-brand/60',
                    'disabled:cursor-not-allowed disabled:opacity-60',
                  ].join(' ')}
                >
                  <span className="text-[13px]">{audioBusy ? '上传中…' : '拖入音频，或点这里选择'}</span>
                  <span className="mt-1 font-mono text-[10.5px] text-ink-mute">
                    mp3 / wav / m4a / flac / ogg · 最大 60 MB
                  </span>
                </button>
              )}

              {audioError && (
                <p className="mt-2 rounded-lg border border-err/30 bg-err/[0.07] px-3 py-2 text-[11.5px] text-err">
                  {audioError}
                </p>
              )}

              <div className="mt-4 flex items-center justify-between gap-3">
                <span className="text-[12px] text-ink-dim">歌词</span>
                <div className="flex items-center gap-3">
                  <span className="font-mono text-[10.5px] tabular-nums text-ink-mute">
                    {lyricsLines > 0 ? `${lyricsLines} 行` : '空'}
                  </span>
                  <input
                    ref={lyricsInput}
                    type="file"
                    accept=".lrc,.txt,text/plain"
                    className="hidden"
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      if (file) readLyrics(file)
                      event.target.value = ''
                    }}
                  />
                  <button
                    type="button"
                    disabled={running}
                    onClick={() => lyricsInput.current?.click()}
                    className="text-[12.5px] text-brand transition-opacity disabled:opacity-40"
                  >
                    读 .lrc 文件
                  </button>
                </div>
              </div>
              <textarea
                value={form.lyrics}
                onChange={(event) => patch({ lyrics: event.target.value })}
                rows={5}
                disabled={running}
                placeholder={'可粘贴歌词，支持 LRC 时间戳：\n[00:12.34]今天我\n[00:16.02]寒夜里看雪飘过'}
                className="field-input mt-2 min-h-[96px] font-mono text-[12px] leading-[1.7]"
              />
              {form.mode === 'song' && !form.lyrics.trim() && (
                <p className="mt-1.5 text-[11px] text-ink-mute">没有歌词时，上屏文字会退回解说词</p>
              )}

              {track && (
                <>
                  <SettingRow
                    label="节拍"
                    sub={form.auto_beats ? '从音频里找 BPM，切点踩在拍上' : '手工指定，切点按你给的速度走'}
                  >
                    <Switch
                      checked={form.auto_beats}
                      onChange={(value) => patch({ auto_beats: value })}
                      label="自动估计"
                      disabled={running}
                    />
                  </SettingRow>
                  {!form.auto_beats && (
                    <SettingRow label="BPM / 首拍" sub="首拍是第一个重拍的秒数">
                      <div className="flex items-center gap-1.5">
                        <input
                          type="number"
                          min={0}
                          max={400}
                          step={0.1}
                          value={form.bpm}
                          disabled={running}
                          onChange={(event) => patch({ bpm: Number(event.target.value) || 0 })}
                          placeholder="BPM"
                          className="field-input w-[66px] text-center tabular-nums"
                        />
                        <input
                          type="number"
                          min={0}
                          max={10}
                          step={0.01}
                          value={form.beat_offset}
                          disabled={running}
                          onChange={(event) => patch({ beat_offset: Number(event.target.value) || 0 })}
                          placeholder="首拍"
                          className="field-input w-[66px] text-center tabular-nums"
                        />
                      </div>
                    </SettingRow>
                  )}
                </>
              )}

              {track && shotEstimate > SHOT_WARN && (
                <p className="mt-2 rounded-lg border border-warn/25 bg-warn/[0.07] px-3 py-2 text-[11.5px] text-warn">
                  {form.duration.toFixed(0)} 秒会拆成约 {shotEstimate} 镜
                  {form.painted ? `，代码绘制 ${form.painted_fps}fps 约需 ${humanDuration(estimate)}` : ''}
                </p>
              )}
            </Panel>
          )}

          {form.painted && <EtaRow label="预计还需" value={humanDuration(estimate)} />}

          <div className="flex items-center justify-end gap-2 pt-1">
            {phase === 'done' && (
              <Button small onClick={reset}>
                清空本次
              </Button>
            )}
            {running ? (
              <Button variant="danger" onClick={cancel}>
                停止生成
              </Button>
            ) : (
              <>
                <Button
                  disabled={!canStart || planning}
                  onClick={() => {
                    if (!onPlan) return
                    setPlanning(true)
                    setPlanError('')
                    createPlan(form)
                      .then((plan) => onPlan(plan.run_id))
                      .catch((err: unknown) =>
                        setPlanError(err instanceof Error ? err.message : '拆解失败'),
                      )
                      .finally(() => setPlanning(false))
                  }}
                >
                  {planning ? '拆解中…' : '先拆解'}
                </Button>
                <Button variant="primary" disabled={!canStart} onClick={() => void start(form)}>
                  开始生成
                </Button>
              </>
            )}
          </div>
          <p className="text-right text-[11px] text-ink-mute">
            {planError ||
              (missingAudio
                ? '选了 MV / 配乐，先上传一段音频'
                : '「先拆解」只出剧本与分镜，不渲染；「开始生成」直接跑到成片')}
          </p>
        </div>

        {/* ------------------------------------------------ 运行状态 */}
        <Panel className="flex min-h-[620px] flex-col p-6">
          <div className="flex items-start justify-between gap-3">
            <SectionTitle title="运行状态" hint={statusHint} />
            <Badge tone={statusTone}>{statusText}</Badge>
          </div>

          <div className="mt-6 flex items-end justify-between gap-4">
            <div className="min-w-0">
              <p className="flex items-baseline gap-1 font-mono text-[34px] leading-none font-medium tabular-nums">
                {percent}
                <span className="text-[15px] text-ink-mute">%</span>
              </p>
              <p className="mt-2 truncate text-[12px] text-ink-dim">{stage}</p>
            </div>
            <p className="shrink-0 text-right font-mono text-[10.5px] leading-relaxed text-ink-mute">
              {form.aspect} · {form.duration}s
              <br />
              {isMusic
                ? form.mode === 'song'
                  ? 'MV · 音乐即音轨'
                  : '配乐 · 音乐垫底'
                : form.painted
                  ? `代码绘制 ${form.painted_fps}fps`
                  : form.live
                    ? '真实模型'
                    : '离线引擎'}
            </p>
          </div>

          <div className="progress-track mt-4">
            <div className="progress-fill" style={{ width: `${Math.max(percent, running ? 2 : 0)}%` }} />
          </div>

          <div className="mt-4">
            <PipelineBeam stage={stage} />
          </div>

          <div className="mt-6 flex min-h-0 flex-1 flex-col">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[10.5px] tracking-wide text-ink-mute">实时日志</span>
              <span className="font-mono text-[10px] tabular-nums text-ink-mute">
                {logs.length} 条
              </span>
            </div>
            <div className="min-h-[240px] flex-1">
              <LogStream logs={logs} />
            </div>
          </div>

          {error && (
            <div className="mt-3 rounded-xl border border-err/30 bg-err/10 px-3.5 py-2.5">
              <p className="text-[11.5px] leading-relaxed break-words text-err">{error}</p>
            </div>
          )}
        </Panel>
      </div>

      {/* ------------------------------------------------ 成片 */}
      {result && compose && (
        <Panel className="mt-4 p-6">
          <SectionTitle
            title="本次成片"
            hint={`${compose.resolution} · ${compose.duration.toFixed(1)} 秒 · ${compose.size_mb} MB`}
            right={
              <div className="flex items-center gap-2">
                <Badge tone="ok">已产出</Badge>
                {result.stopped && <Badge tone="warn">中途停止</Badge>}
              </div>
            }
          />

          <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_250px]">
            <div className="overflow-hidden rounded-xl bg-black ring-1 ring-black/10">
              <video
                key={result.run_id}
                src={videoUrl(jobId)}
                controls
                playsInline
                className="max-h-[430px] w-full object-contain"
              />
            </div>

            <div className="flex flex-col gap-3">
              <dl className="glass-sunk divide-y divide-[var(--ring)] px-3.5 py-1">
                {[
                  ['分辨率', compose.resolution],
                  ['时长', `${compose.duration.toFixed(1)} 秒`],
                  ['体积', `${compose.size_mb} MB`],
                  ['音轨', audioLabel(compose.audio_source, compose.has_audio)],
                  ['镜头', `交付 ${compose.delivered} · 剔除 ${compose.dropped}`],
                  ['决策', `${result.decisions} 次`],
                ].map(([label, value]) => (
                  <div key={label} className="flex items-baseline justify-between gap-3 py-1.5">
                    <dt className="text-[11px] text-ink-mute">{label}</dt>
                    <dd className="text-right text-[11.5px] text-ink-dim">{value}</dd>
                  </div>
                ))}
              </dl>

              <a className="btn btn-ghost" href={videoUrl(jobId)} download>
                下载成片
              </a>
              <p className="break-all font-mono text-[9.5px] leading-relaxed text-ink-mute">
                {compose.video}
              </p>
            </div>
          </div>

          {result.notes.length > 0 && (
            <div className="mt-4 rounded-xl border border-warn/25 bg-warn/[0.07] px-4 py-3">
              <p className="text-[11px] font-medium text-warn">需要人工关注</p>
              <ul className="mt-1.5 space-y-1">
                {result.notes.map((note, index) => (
                  <li key={index} className="text-[11.5px] leading-relaxed text-ink-dim">
                    · {note}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Panel>
      )}
    </div>
  )
}

