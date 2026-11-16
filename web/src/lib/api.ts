/** REST 封装与类型。字段名与后端 `RunOptions` 严格一致，不做任何改名。 */

import { getGuestId, getToken, mediaCredentialQuery } from './auth'

/** 三种输入模式。`narration` 是纯一句话解说；后两种都要音轨。 */
export type MusicMode = 'narration' | 'song' | 'bgm'

/** 一次音轨上传的结果（`POST /api/media/audio`）。 */
export interface AudioTrack {
  token: string
  name: string
  size: number
  duration: number
  format: string
}

export type PaletteId = 'auto' | 'warm' | 'cool' | 'film' | 'ink' | 'vivid'

export interface FormState {
  theme: string
  duration: number
  aspect: string
  style: string
  palette: PaletteId
  shot_len: number
  max_shots: number | null
  live: boolean
  guides: boolean
  no_cache: boolean
  painted: boolean
  painted_fps: number
  painted_quality: 'fast' | 'medium' | 'high'
  clean_frames: boolean

  // ---- 音乐。`mode` 是界面自己的概念，发请求时映射成 `music_role`；
  // `audio_name` / `audio_duration` 只用于显示，后端没有这两个字段。
  mode: MusicMode
  /** 音轨 token。空串 = 没有音轨，后端据此走解说模式。 */
  audio: string
  audio_name: string
  audio_duration: number
  lyrics: string
  /** 手工 BPM。0 = 交给自动估计。 */
  bpm: number
  beat_offset: number
  auto_beats: boolean
}

/** 表单 → 请求体。
 *
 * 不直接把 `FormState` 整个发出去：`mode` / `audio_name` / `audio_duration`
 * 是界面自己的字段，混进请求体会让"界面上有、引擎里没有"的假参数重新长出来。
 * 解说模式下连音乐字段都不发，请求体与改动前逐字节相同。
 */
function toPayload(form: FormState): Record<string, unknown> {
  const base = {
    theme: form.theme,
    duration: form.duration,
    aspect: form.aspect,
    style: form.style,
    palette: form.palette,
    shot_len: form.shot_len,
    max_shots: form.max_shots,
    live: form.live,
    guides: form.guides,
    no_cache: form.no_cache,
    painted: form.painted,
    painted_fps: form.painted_fps,
    painted_quality: form.painted_quality,
    clean_frames: form.clean_frames,
  }
  if (form.mode === 'narration') return base
  return {
    ...base,
    audio: form.audio,
    lyrics: form.lyrics,
    music_role: form.mode === 'bgm' ? 'bgm' : 'song',
    bpm: form.bpm,
    beat_offset: form.beat_offset,
    auto_beats: form.auto_beats,
  }
}

export interface Preset {
  theme: string
  duration: number
  aspect: string
  style: string
}

export interface Meta {
  version: string
  tagline: string
  presets: Preset[]
  aspects: string[]
  qualities: { key: 'fast' | 'medium' | 'high'; label: string; hint: string }[]
}

export interface EnvCheck {
  name: string
  state: 'ok' | 'warn' | 'bad'
  detail: string
}

export interface EnvPayload {
  pending?: boolean
  ready: boolean
  checks: EnvCheck[]
  degraded: string[]
  live_ready: boolean
  keys: Record<string, string>
  /** 哪些配置项已保存了非空值。界面据此提供"清除"，不回传密钥明文。 */
  configured?: Record<string, boolean>
}

export interface ComposeInfo {
  video: string
  subtitle: string
  storyboard: string
  duration: number
  resolution: string
  has_audio: boolean
  audio_source: string
  audio: string
  size_mb: number
  delivered: number
  dropped: number
  notes: string[]
}

export interface ShotInfo {
  id: number
  duration: number
  start: number
  status: string
  quality: number
  attempts: number
  narration: string
  caption: string
  visual: string
  route: string
}

export interface RunResult {
  run_id: string
  project_dir: string
  summary: string
  log: string
  brief: { theme: string; total_duration: number; aspect_ratio: string }
  script: { title: string; logline: string; tone: string }
  storyboard: { project: Record<string, unknown>; shots: ShotInfo[]; subjects: unknown[] }
  compose: ComposeInfo
  music: Record<string, unknown>
  lyrics: unknown[]
  decisions: number
  notes: string[]
  stopped: boolean
  /** 低清预览产物：分辨率减半、写在 preview/ 下，不是正式成片。 */
  preview?: boolean
  /** 单镜预览：只生成这一镜的片段，不重合成整片。 */
  shot_preview?: boolean
  shot_id?: number
}

export interface JobSnapshot {
  job_id: string
  theme: string
  running: boolean
  finished: boolean
  failed: boolean
  error: string
  cursor: number
  video: string
  result: RunResult | null
  /** 以下字段来自任务表：服务重启后仍能拿到，界面据此显示"中断/完成"。 */
  kind?: string
  status?: string
  run_id?: string
  created?: number
  updated?: number
  has_video?: boolean
  options?: Record<string, unknown>
}

export interface RunAccepted {
  job_id: string
  theme: string
  running: boolean
}

/** 任务中心的一条记录。与服务重启无关——它读的是落库的任务表。 */
export interface TaskItem {
  job_id: string
  run_id: string
  theme: string
  /** create = 一句话跑完；plan/shot/recompose = 按拆解拍摄/单镜重拍/只重合成。 */
  kind: 'create' | 'plan' | 'shot' | 'recompose' | string
  status: 'running' | 'done' | 'canceled' | 'failed' | 'interrupted' | string
  error: string
  running: boolean
  finished: boolean
  failed: boolean
  has_video: boolean
  created: number
  updated: number
  options?: Record<string, unknown>
}

/** 统一出口：**自动带上登录 token**。
 *
 * 直接调 `fetch` 的话，登录之后请求依然以游客身份发出——成片墙会看不到自己的
 * 作品、创建的任务也不归属自己。这类 bug 不报错，只是"数据不对"。
 */
function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  // 游客凭据始终带上：服务端优先认 token，没有 token 时才用它绑定归属。
  headers.set('X-Guest-Id', getGuestId())
  return fetch(input, { ...init, headers })
}

/** 带状态码的接口错误。
 *
 * `toJson` 原来只抛一句 detail，前端拿不到状态码，于是分不清"这一场有手改过的
 * 镜头，需要你确认"（409）和"真的出错了"——两者对用户的处理方式完全不同。
 */
export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function toJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body)
    } catch {
      /* 响应不是 JSON，保留状态码 */
    }
    throw new ApiError(detail, res.status)
  }
  return (await res.json()) as T
}

export async function fetchMeta(): Promise<Meta> {
  return toJson<Meta>(await apiFetch('/api/meta', { cache: 'no-store' }))
}

export async function createRun(form: FormState): Promise<RunAccepted> {
  return toJson<RunAccepted>(
    await apiFetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(toPayload(form)),
    }),
  )
}

/** 上传音轨。**不要设 Content-Type**——multipart 的 boundary 由浏览器自己写，
 * 手写一个 `multipart/form-data` 会把 boundary 弄丢，后端解析直接 400。 */
export async function uploadAudio(file: File): Promise<AudioTrack> {
  const body = new FormData()
  body.append('file', file)
  return toJson<AudioTrack>(await apiFetch('/api/media/audio', { method: 'POST', body }))
}

export async function getJob(jobId: string): Promise<JobSnapshot> {
  return toJson<JobSnapshot>(await apiFetch(`/api/runs/${jobId}`, { cache: 'no-store' }))
}

/** 任务中心列表。进行中的任务来自内存注册表，已结束的来自任务表。 */
export async function fetchTasks(limit = 60): Promise<TaskItem[]> {
  const data = await toJson<{ jobs: TaskItem[] }>(
    await apiFetch(`/api/runs?limit=${limit}`, { cache: 'no-store' }),
  )
  return data.jobs
}

export async function cancelRun(jobId: string): Promise<void> {
  const res = await apiFetch(`/api/runs/${jobId}/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error(`停止失败（HTTP ${res.status}）`)
}

/** 重试一条失败/中断的拍摄任务。返回新任务的 job_id，参数按项目当前版本重建。 */
export async function retryTask(jobId: string): Promise<RunAccepted & { retried_from?: string }> {
  return toJson<RunAccepted & { retried_from?: string }>(
    await apiFetch(`/api/runs/${jobId}/retry`, { method: 'POST' }),
  )
}

export function eventsUrl(jobId: string, cursor: number): string {
  return `/api/runs/${jobId}/events?cursor=${cursor}`
}

/** `<video>` / 下载链接带不了请求头，凭据只能进 URL。 */
export function videoUrl(jobId: string): string {
  return `/api/runs/${jobId}/video${mediaCredentialQuery()}`
}

/** 任务中心的成片地址。按 job_id 取，重启后依然可播。 */
export function jobVideoUrl(jobId: string): string {
  return `/api/runs/${encodeURIComponent(jobId)}/video${mediaCredentialQuery()}`
}

/** 代码绘制的粗略耗时：帧数 × 每帧秒数。画质档位来自 pipeline/painted.py 的实测。 */
export function estimatePaintedSeconds(duration: number, fps: number, quality: string): number {
  const perFrame = quality === 'fast' ? 0.55 : quality === 'high' ? 0.76 : 0.7
  return Math.round(duration * fps * perFrame)
}

export function humanDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return s ? `${m} 分 ${s} 秒` : `${m} 分钟`
}

/* ---------------------------------------------------------------- 成片墙 */

export interface HistoryItem {
  run_id: string
  title: string
  style: string
  aspect: string
  duration: number
  shots: number
  size_mb: number
  has_video: boolean
  status: 'ok' | 'canceled' | 'failed'
  created: string
  tags: string[]
}

export interface HistoryShot {
  id: number
  start: number
  duration: number
  status: string
  quality: number | null
  attempts: number
  caption: string
  /** simulated / technical / vlm / technical_failed。界面据此标注评审依据。 */
  review_mode?: string
  visual_check?: string
  review_rule?: string
  /** 出图来源。offline = 离线占位背景，静帧与评审证据图不是同一张。 */
  frame_source?: string
}

export interface RunDetail {
  run_id: string
  title: string
  style: string
  aspect: string
  duration: number
  created: string
  shots: HistoryShot[]
  has_video: boolean
  has_subtitle: boolean
  size_mb: number
  tags: string[]
  /** ok / canceled / failed，与列表页同一套判定。 */
  status: string
}

export async function fetchHistory(limit = 60, mine = false): Promise<HistoryItem[]> {
  const query = `limit=${limit}${mine ? '&mine=true' : ''}`
  const data = await toJson<{ items: HistoryItem[] }>(
    await apiFetch(`/api/history?${query}`, { cache: 'no-store' }),
  )
  return data.items
}

/* ---------------------------------------------------------------- 项目库 */

export interface ProjectItem {
  run_id: string
  title: string
  /** draft = 只拆未拍；ok/canceled = 已拍；failed = 拍过但没有成片。 */
  state: 'draft' | 'ok' | 'canceled' | 'failed'
  shots: number
  duration: number
  revision: number
  has_video: boolean
  updated: string
  render_profile?: Partial<RenderProfile>
}

export async function fetchProjects(
  state: 'all' | 'draft' | 'rendered' = 'all',
  limit = 100,
): Promise<ProjectItem[]> {
  const data = await toJson<{ items: ProjectItem[] }>(
    await apiFetch(`/api/projects?state=${state}&limit=${limit}`, { cache: 'no-store' }),
  )
  return data.items
}

export interface PlanVersion {
  revision: number
  at: string
  edited?: { script?: boolean; shots?: number[] }
  restored_from?: number | null
}

export async function fetchVersions(
  runId: string,
): Promise<{ current: number; versions: PlanVersion[] }> {
  return toJson<{ current: number; versions: PlanVersion[] }>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/versions`, { cache: 'no-store' }),
  )
}

export async function restoreVersion(runId: string, revision: number): Promise<PlanResult> {
  return toJson<PlanResult>(
    await apiFetch(
      `/api/plan/${encodeURIComponent(runId)}/versions/${revision}/restore`,
      { method: 'POST' },
    ),
  )
}

/** 当前生效的字幕轨：人工编辑 > 上次成片 SRT > 按分镜推导。 */
export async function fetchSubtitles(runId: string): Promise<SubtitleTrack> {
  return toJson<SubtitleTrack>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/subtitles`, { cache: 'no-store' }),
  )
}

/** 保存整条字幕轨。`lines=null` 恢复自动生成，`[]` 表示明确不要字幕。 */
export async function saveSubtitles(
  runId: string,
  lines: SubtitleLine[] | null,
  expectedRevision?: number,
  subtitleMode?: 'burn' | 'sidecar' | 'none',
): Promise<{
  source: string
  lines: SubtitleLine[] | null
  revision: number
  subtitle_mode?: 'burn' | 'sidecar' | 'none'
  note: string
}> {
  return toJson(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/subtitles`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lines,
        expected_revision: expectedRevision,
        subtitle_mode: subtitleMode,
      }),
    }),
  )
}

/** 字幕样式预置（6 套，与色板呼应）。 */
export interface SubtitleStylePreset {
  id: string
  label: string
  note: string
  color: string
  stroke: string
  box: boolean
  position: 'bottom' | 'low' | string
}

export interface SubtitleStyleState extends SubtitleStylePreset {
  family: string
  scale: number
  stroke_ratio: number
  shadow: number
  box_color: string
  max_lines: number
  line_gap: number
  version: number
  updated?: string
}

/** 当前样式字体能否覆盖现有字幕；missing/fallback 用于界面明确提示缺字。 */
export interface SubtitleStyleCoverage {
  ok: boolean
  missing: string[]
  missing_count: number
  fallback: string
  uncovered: string[]
}

export interface SubtitleStyleInfo {
  current: SubtitleStyleState
  presets: SubtitleStylePreset[]
  revision: number
  coverage: SubtitleStyleCoverage
}

export async function fetchSubtitleStyle(runId: string): Promise<SubtitleStyleInfo> {
  return toJson<SubtitleStyleInfo>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/subtitle-style`, { cache: 'no-store' }),
  )
}

/** 换一套整句字幕样式。只影响重合成，不重出片段。 */
export async function saveSubtitleStyle(
  runId: string,
  preset: string,
  expectedRevision?: number,
): Promise<{
  current: SubtitleStyleState
  revision: number
  coverage: SubtitleStyleCoverage
  note: string
}> {
  return toJson(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/subtitle-style`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset, expected_revision: expectedRevision }),
    }),
  )
}

/** 音轨峰值包络。只用于画图，不是播放接口。 */
export interface Waveform {
  duration: number
  buckets: number
  /** 归一化到本音轨峰值，0..1。 */
  peaks: number[]
  /** 音轨绝对峰值，0..1。 */
  peak: number
  sample_rate: number
  name: string
  role?: string
  cached: boolean
}

export async function fetchWaveform(runId: string, buckets = 1200): Promise<Waveform> {
  return toJson<Waveform>(
    await apiFetch(
      `/api/plan/${encodeURIComponent(runId)}/waveform?buckets=${Math.round(buckets)}`,
      { cache: 'no-store' },
    ),
  )
}

export async function fetchRunDetail(runId: string): Promise<RunDetail> {
  return toJson<RunDetail>(await apiFetch(`/api/history/${encodeURIComponent(runId)}`, { cache: 'no-store' }))
}

/* ---------------------------------------------------------------- 质检复核 */

export interface ReviewTechnical {
  /** 这次运行有没有留下技术检查证据。false = 旧版本产物，不能当成通过。 */
  recorded: boolean
  ok: boolean
  reason: string
  width: number
  height: number
  mean: number
  contrast: number
}

export interface ManualReview {
  verdict: 'approved' | 'rejected' | string
  note: string
  reviewed_by: string
  reviewed_at: string
  /** 画面哈希变过：结论只代表旧画面，不能沿用。 */
  stale: boolean
}

export interface ReviewShot {
  id: number
  start: number
  duration: number
  status: string
  quality: number | null
  attempts: number
  defect_type: string
  /** simulated / vlm / technical_failed；空串 = 旧运行没有记录。 */
  review_mode: string
  visual_check: string
  review_rule: string
  /** 出图来源。offline = 离线占位背景，静帧与评审证据图不是同一张。 */
  frame_source?: string
  image_sha256: string
  technical: ReviewTechnical
  manual: ManualReview | null
}

export interface ReviewSummary {
  total: number
  manual_approved: number
  manual_rejected: number
  pending: number
  stale: number
  modes: Record<string, number>
  technical_recorded: number
  technical_failed: number
}

export interface ReviewPanel {
  run_id: string
  updated: string
  shots: ReviewShot[]
  summary: ReviewSummary
}

export async function fetchReview(runId: string): Promise<ReviewPanel> {
  return toJson<ReviewPanel>(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/review`, { cache: 'no-store' }),
  )
}

export async function submitReview(
  runId: string,
  shotId: number,
  body: { verdict: 'approved' | 'rejected' | 'clear'; note?: string; image_sha256?: string },
): Promise<{ shot: ReviewShot; summary: ReviewSummary; note: string }> {
  return toJson(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/review/${shotId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  )
}

export async function deleteRun(runId: string): Promise<string> {
  const data = await toJson<{ detail: string }>(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}`, { method: 'DELETE' }),
  )
  return data.detail
}

export async function revealRun(runId: string): Promise<void> {
  const res = await apiFetch(`/api/history/${encodeURIComponent(runId)}/reveal`, { method: 'POST' })
  if (!res.ok) throw new Error(`打不开目录（HTTP ${res.status}）`)
}

/** 媒体地址的 token 后缀。
 *
 * `<img src>` / `<video src>` 是浏览器自己发起的请求，**带不了 Authorization 头**，
 * `apiFetch` 管不到它们。只认头的话，登录用户看自己作品的封面照样 404，
 * 而列表却是正常的——表现为"有的卡片没封面"，很难查。
 * 所以后端这三个接口额外接受 `?token=`。
 */
export function historyVideoUrl(runId: string): string {
  return `/api/history/${encodeURIComponent(runId)}/video${mediaCredentialQuery()}`
}

export function historyCoverUrl(runId: string): string {
  return `/api/history/${encodeURIComponent(runId)}/cover${mediaCredentialQuery()}`
}

/** 单镜留存画面。质检面板叠安全区用；没有留存画面时返回 404。 */
export function historyStillUrl(runId: string, shotId: number): string {
  return (
    `/api/history/${encodeURIComponent(runId)}/shots/${encodeURIComponent(String(shotId))}/still` +
    mediaCredentialQuery()
  )
}

export function historySubtitleUrl(runId: string): string {
  return `/api/history/${encodeURIComponent(runId)}/subtitle${mediaCredentialQuery()}`
}

/** LRC 导出：从实际成片 SRT 转出来，时间轴与片子一致。 */
export function historyLrcUrl(runId: string): string {
  return `/api/history/${encodeURIComponent(runId)}/subtitle?format=lrc${mediaCredentialQuery('&')}`
}

export async function fetchSubtitle(runId: string): Promise<string> {
  const res = await apiFetch(historySubtitleUrl(runId), { cache: 'no-store' })
  if (!res.ok) throw new Error('没有字幕文件')
  return res.text()
}

/* ---------------------------------------------------------------- 多规格导出 */

export interface ExportPreset {
  id: string
  label: string
  /** 规格说明。码率/响度是平台参考值，不是官方规范。 */
  note: string
  fps: number
  loudness: number | null
  /** 固定画幅；空串表示跟随所选画幅。 */
  aspect: string
}

export interface ExportItem {
  export_id: string
  /** 目标画幅：9:16 / 1:1。16:9 原片不重复编码。 */
  aspect: string
  /** fit = 模糊补边保留全画面；crop = 中心裁切。 */
  mode: 'fit' | 'crop' | string
  status: 'running' | 'done' | 'failed' | string
  error?: string
  width: number
  height: number
  size_mb: number
  /** 导出时是否套用了品牌层。 */
  branded?: boolean
  brand_version?: number
  /** 规格预设 id；standard = 沿用原参数。 */
  preset?: string
  fps?: number
  loudness?: number | null
  created?: string
}

export async function fetchExports(runId: string): Promise<{
  items: ExportItem[]
  source: string
  branded?: boolean
  brand_version?: number
  presets?: ExportPreset[]
}> {
  return toJson(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/exports`, { cache: 'no-store' }),
  )
}

/** 启动一次导出。已缓存且原片未变时服务端直接返回 done。 */
export async function startExport(
  runId: string,
  aspect: '16:9' | '9:16' | '1:1',
  mode: 'fit' | 'crop',
  preset = 'standard',
): Promise<ExportItem> {
  return toJson<ExportItem>(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/exports`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ aspect, mode, preset }),
    }),
  )
}

export interface BatchExportResult {
  items: ExportItem[]
  skipped: { aspect: string; mode: string; reason: string; preset?: string }[]
  branded: boolean
}

/** 批量导出：服务端按顺序渲染，不并发抢 CPU。原片画幅相同的项会如实跳过。 */
export async function startBatchExports(
  runId: string,
  targets: { aspect: '16:9' | '9:16' | '1:1'; mode: 'fit' | 'crop'; preset?: string }[],
): Promise<BatchExportResult> {
  return toJson<BatchExportResult>(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/exports/batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ targets }),
    }),
  )
}

export function exportFileUrl(runId: string, exportId: string): string {
  return (
    `/api/history/${encodeURIComponent(runId)}/exports/${encodeURIComponent(exportId)}/file` +
    mediaCredentialQuery()
  )
}

/* ---------------------------------------------------------------- 品牌模板 */

export type BrandPosition = 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left'

export interface BrandLogo {
  asset?: string
  sha?: string
  width?: number
  height?: number
  name?: string
}

export interface BrandConfig {
  name: string
  intro_text: string
  outro_text: string
  position: BrandPosition
  opacity: number
  intro_seconds: number
  outro_seconds: number
  logo: BrandLogo
  version: number
  updated: string
}

export interface BrandState {
  run_id: string
  brand: BrandConfig
  applied: boolean
  can_undo: boolean
  history: number
  logo_url: string
}

export interface BrandPatch {
  name: string
  intro_text: string
  outro_text: string
  position: BrandPosition
  opacity: number
  intro_seconds: number
  outro_seconds: number
  expected_version?: number
}

export async function fetchBrand(runId: string): Promise<BrandState> {
  return toJson<BrandState>(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/brand`, { cache: 'no-store' }),
  )
}

export async function saveBrand(runId: string, patch: BrandPatch): Promise<BrandState> {
  return toJson<BrandState>(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/brand`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  )
}

export async function undoBrand(runId: string): Promise<BrandState & { note?: string }> {
  return toJson(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/brand/undo`, { method: 'POST' }),
  )
}

export async function clearBrand(runId: string): Promise<BrandState & { note?: string }> {
  return toJson(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/brand`, { method: 'DELETE' }),
  )
}

export async function uploadBrandLogo(
  runId: string,
  file: File,
  expectedVersion?: number,
): Promise<BrandState> {
  const form = new FormData()
  form.append('file', file)
  const query = expectedVersion == null ? '' : `?expected_version=${expectedVersion}`
  return toJson<BrandState>(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/brand/logo${query}`, {
      method: 'POST',
      body: form,
    }),
  )
}

export async function deleteBrandLogo(
  runId: string,
  expectedVersion?: number,
): Promise<BrandState> {
  const query = expectedVersion == null ? '' : `?expected_version=${expectedVersion}`
  return toJson<BrandState>(
    await apiFetch(`/api/history/${encodeURIComponent(runId)}/brand/logo${query}`, {
      method: 'DELETE',
    }),
  )
}

/** `<img>` 带不了请求头，logo 地址同样要带 token/guest；版本号用于换图后破缓存。 */
export function brandLogoUrl(runId: string, version?: string): string {
  const base = `/api/history/${encodeURIComponent(runId)}/brand/logo`
  const credential = mediaCredentialQuery()
  if (!version) return base + credential
  return `${base}${credential}${credential ? '&' : '?'}v=${encodeURIComponent(version)}`
}

/* ---------------------------------------------------------------- 生成器与费用 */

export interface ProviderItem {
  id: string
  label: string
  kind: 'paint' | 'image' | 'video' | 'tts' | 'text' | string
  available: boolean
  detail: string
  reference_images: boolean | null
  reference_detail: string
  cost: 'free_local' | 'self_hosted' | 'paid' | 'paid_unknown' | string
  cost_label: string
  price_hint: string
  configured: boolean
  /** 能力测试的口径：只探测可用性，不生成收费样片。 */
  test: string
}

export interface ProviderCatalog {
  items: ProviderItem[]
  ready: string[]
  budget_cny: number
  budget_note: string
  estimated: boolean
  probed_at: string
}

export interface UsageItem {
  run_id: string
  theme: string
  created: string
  events: number
  total_cny: number
  by_provider: Record<string, number>
  over_budget: boolean
}

export interface UsagePayload {
  items: UsageItem[]
  total_cny: number
  budget_cny: number
  estimated: boolean
  scanned: number
  note: string
}

export async function fetchProviders(fresh = false): Promise<ProviderCatalog> {
  return toJson<ProviderCatalog>(
    await apiFetch(`/api/providers?fresh=${fresh ? 'true' : 'false'}`, { cache: 'no-store' }),
  )
}

export async function fetchUsage(): Promise<UsagePayload> {
  return toJson<UsagePayload>(await apiFetch('/api/usage', { cache: 'no-store' }))
}

/* ---------------------------------------------------------------- 环境 */

export async function fetchEnv(fresh = false): Promise<EnvPayload> {
  return toJson<EnvPayload>(await apiFetch(`/api/env?fresh=${fresh}`, { cache: 'no-store' }))
}

export interface EnvUpdate {
  /** 要替换/新增的键。不出现的键 = 保留原值。 */
  values?: Record<string, string>
  /** 要整行移除的键。与 values 互斥。 */
  delete?: string[]
}

export interface EnvSaveResult {
  detail: string
  updated: string[]
  deleted: string[]
  configured?: Record<string, boolean>
}

export async function saveEnv(update: EnvUpdate): Promise<EnvSaveResult> {
  return toJson<EnvSaveResult>(
    await apiFetch('/api/env', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(update),
    }),
  )
}

/* ---------------------------------------------------------------- 拆解层 */

export interface PlanAct {
  name: string
  time_range?: string
  content?: string
  /** 老版本/大模型直出的写法，与 content 同义。读的时候两个都认。 */
  summary?: string
  /** 保留未知键：改文字时原样回传，别把模型给的结构抹掉。 */
  [key: string]: unknown
}

export interface PlanScript {
  title: string
  logline: string
  tone: string
  target_audience?: string
  hook?: string
  acts: PlanAct[]
}

export interface PlanShot {
  id: number
  act: string
  duration: number
  start: number
  narration: string
  visual: string
  camera: string
  caption?: string
  status?: string
  /** 这一镜引用了哪些角色。出图时这些角色的视觉锁定会拼进提示词。 */
  subject_refs?: string[]
}

/** 角色参考图。只是项目素材存档，当前出图后端不消费它。 */
export interface SubjectReference {
  /** 项目内相对路径。实际图片走 subjectReferenceUrl。 */
  asset: string
  sha256: string
  width: number
  height: number
  bytes: number
  name?: string
  at?: string
}

export interface PlanSubject {
  id: string
  name: string
  visual_lock?: string
  reference?: SubjectReference | null
}

export interface SubjectPatch {
  expected_revision?: number
  name?: string
  visual_lock?: string
}

export interface SubjectPatchResult {
  subject: PlanSubject
  /** 引用这个角色的镜头。改锁定后它们下次出图才会带上新文字。 */
  affected_shots?: number[]
  meta: PlanMeta
  note: string
  capability?: string
}

export interface SubjectReferenceResult {
  subject: PlanSubject
  meta: PlanMeta
  note: string
  capability?: string
}

export interface RenderProfile {
  live: boolean
  painted: boolean
  painted_fps: number
  painted_quality: 'fast' | 'medium' | 'high'
  clean_frames: boolean
  palette?: PaletteId
}

/** 镜头间转场。none = 硬切。 */
export type TransitionId = 'none' | 'fade' | 'slide' | 'black'

/** 项目保存的混音/字幕设置。改它不需要重画，只需重合成。 */
export interface AudioEdit {
  /** 人声电平：0 = 静音，1 = 原值。 */
  voice_gain: number
  /** 音乐电平：0 = 静音，1 = 原值。配乐模式默认约 0.28。 */
  music_gain: number
  /** 对白优先：人声出现时自动压低垫乐。默认关。 */
  voice_duck: boolean
  /** 外挂字幕整体偏移（秒），正值延后。 */
  subtitle_offset: number
  /** 镜头间转场：叠化 / 左滑 / 黑场。只影响合成。 */
  transition: TransitionId
  /** 字幕卡入场淡入（0.2s）。默认关。 */
  text_anim: boolean
}

/** 一条人工字幕。时间是叠加项目整体偏移之前的原始时间。 */
export interface SubtitleLine {
  start: number
  end: number
  text: string
}

export interface SubtitleTrack {
  /** edited = 人工保存；rendered = 读自上次成片；plan = 按分镜推导。 */
  source: 'edited' | 'rendered' | 'plan' | string
  lines: SubtitleLine[]
  duration: number
  revision: number
  subtitle_mode?: 'burn' | 'sidecar' | 'none'
  note?: string
}

export interface PlanMeta {
  run_id?: string
  live?: boolean
  model?: string
  created?: string
  /** 项目版本。每次编辑 +1，拍摄时按它取冻结快照。 */
  revision?: number
  /** 项目色板：只改配色，不改题材与文案。 */
  palette?: PaletteId
  /** 创作页选定的完整拍摄配置。拍摄入口只覆盖显式传入的字段。 */
  render_profile?: RenderProfile
  /** 已保存的混音与字幕设置。拍摄/重合成时没显式覆盖就沿用它。 */
  audio_edit?: AudioEdit
  /** 最近一次拍摄实际使用的配置与对应版本。 */
  last_render_profile?: RenderProfile
  last_revision?: number
  /** 最近一次低清预览（不是正式拍摄，不影响 rendered 与片段缓存）。 */
  last_preview_at?: string
  last_preview_revision?: number
  /** 最近一次单镜预览：哪一镜、基于哪个版本。 */
  last_shot_preview?: { shot: number; at: string; revision: number }
  /** 哪些是你改过的。没有它，重跑之后就分不清"模型的判断"和"我的修改"。 */
  edited?: { script: boolean; shots: number[] }
  edited_at?: string
  /** 剧本改过之后可能受影响的镜号。只标注，不自动重派生。 */
  stale_shots?: number[]
  /** 按场重拆的历史（最近 20 条）。重拆会改镜号，留个痕便于对账。 */
  replanned?: { act: string; at: string; before: number; after: number }[]
  /** 这份拆解是否已经拍过。没拍过就没有"上次的片段"，单镜重拍无从复用。 */
  rendered?: boolean
  rendered_at?: string
  /** 最近一次单镜重拍：哪一镜、什么时候。 */
  last_single_shot?: { shot: number; at: string }
  /** 最近一次只重合成（改镜长 / 挪顺序之后重拼）。 */
  last_recompose?: { at: string }
  degradations?: string[]
}

export interface PlanResult {
  run_id: string
  project: Record<string, unknown>
  script: PlanScript
  subjects: PlanSubject[]
  shots: PlanShot[]
  /** 已有正式片段的镜头。只重合成会复用这些片段，画面不会变。 */
  clips?: { available: number[] }
  notes?: string[]
  /** 恢复版本等操作的一次性说明。 */
  note?: string
  live?: boolean
  meta?: PlanMeta
  /** 拆解时带的音轨。没有就是空对象。
   *
   * 拍摄阶段靠它回落——拆解页的「开始拍摄」只传 run_id，没有第二个地方能
   * 重新填歌词和节拍。少了它，从拆解页拍出来的 MV 会是无声的。
   */
  music?: PlanMusic
}

export interface PlanMusic {
  token?: string
  name?: string
  duration?: number
  role?: 'song' | 'bgm'
  lyrics?: string
  bpm?: number
  beat_offset?: number
  auto_beats?: boolean
}

/** 单镜改动。只传改了的字段——不传等于"不动"。 */
export interface ShotPatch {
  /** 期望的项目版本。与服务端当前版本不符时返回 409，而不是静默覆盖。 */
  expected_revision?: number
  duration?: number
  visual?: string
  camera?: string
  narration?: string
  caption?: string
  prompt_image?: string
  prompt_video?: string
  acceptance?: string
}

export interface ShotPatchResult {
  shot: PlanShot
  total_duration: number
  /** 起点跟着挪了的镜头。改时长必然牵动后面所有镜头。 */
  moved: { id: number; start: number }[]
  meta: PlanMeta
  note: string
}

export interface ScriptPatch {
  expected_revision?: number
  title?: string
  logline?: string
  tone?: string
  target_audience?: string
  hook?: string
  narration_full?: string
  acts?: PlanAct[]
}

export interface ScriptPatchResult {
  script: PlanScript
  stale_shots: number[]
  meta: PlanMeta
  note: string
}

export interface ReplanResult {
  act: string
  /** 整份新分镜。镜号会全片重排，所以不能只换这一场的几行。 */
  storyboard: {
    project: Record<string, unknown>
    subjects: PlanSubject[]
    shots: PlanShot[]
  }
  /** 这一场新拆出来的镜头（已带全片镜号）。 */
  shots: PlanShot[]
  total_duration: number
  old_ids: number[]
  stale_shots: number[]
  meta: PlanMeta
  note: string
}

export interface RenderOptions {
  /** 拍摄前先 flush 编辑，再带上最新版本；版本不符服务端会拒绝拍摄。 */
  expected_revision?: number
  live?: boolean
  painted?: boolean
  painted_fps?: number
  painted_quality?: 'fast' | 'medium' | 'high'
  clean_frames?: boolean
  palette?: PaletteId
  audio?: string
  lyrics?: string
  /** 混音电平与字幕偏移。显式传入时随拍摄/重合成保存到项目。 */
  voice_gain?: number
  music_gain?: number
  /** 对白优先：人声出现时自动压低垫乐。 */
  voice_duck?: boolean
  /** 镜头间转场。 */
  transition?: TransitionId
  /** 字幕卡入场淡入。 */
  text_anim?: boolean
  subtitle_offset?: number
}

export async function createPlan(form: FormState): Promise<PlanResult> {
  return toJson<PlanResult>(
    await apiFetch('/api/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(toPayload(form)),
    }),
  )
}

export async function fetchPlan(runId: string): Promise<PlanResult> {
  return toJson<PlanResult>(await apiFetch(`/api/plan/${encodeURIComponent(runId)}`, { cache: 'no-store' }))
}

export interface PalettePreset {
  id: PaletteId
  label: string
  hint: string
  colors: string[]
}

export async function fetchPalettes(): Promise<{ items: PalettePreset[] }> {
  return toJson(await apiFetch('/api/palettes', { cache: 'no-store' }))
}

export interface PaletteSaveResult {
  run_id: string
  palette: PaletteId
  revision: number
  note: string
}

/** 保存项目色板。会递增项目版本；改色板后需整片重拍才会改变画面。 */
export async function savePalette(
  runId: string,
  palette: PaletteId,
  expectedRevision?: number,
): Promise<PaletteSaveResult> {
  return toJson<PaletteSaveResult>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/palette`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ palette, expected_revision: expectedRevision }),
    }),
  )
}

export async function patchShot(runId: string, shotId: number, patch: ShotPatch): Promise<ShotPatchResult> {
  return toJson<ShotPatchResult>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/shots/${shotId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  )
}

export async function patchScript(runId: string, patch: ScriptPatch): Promise<ScriptPatchResult> {
  return toJson<ScriptPatchResult>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/script`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  )
}

/** 按场重拆。只重拆这一场，其余场次的镜头不动。
 *
 * 这一场若有你手改过的镜头，后端会回 409 而不是直接覆盖——调用方要接住
 * 这个状态码，让用户确认后再带 `force=true` 重发一次。
 */
export async function replanAct(
  runId: string,
  actName: string,
  force = false,
  expectedRevision?: number,
): Promise<ReplanResult> {
  return toJson<ReplanResult>(
    await apiFetch(
      `/api/plan/${encodeURIComponent(runId)}/acts/${encodeURIComponent(actName)}/replan`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force, expected_revision: expectedRevision }),
      },
    ),
  )
}

/** 按这份拆解出片。返回的 job_id 与"一次跑完"完全同构，前端复用同一套事件流。 */
export async function renderPlan(runId: string, opts: RenderOptions = {}): Promise<RunAccepted> {
  return toJson<RunAccepted>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts),
    }),
  )
}

/** 低清预览：半分辨率 + 绘制帧率封顶 12，产物独立写在 preview/。
 *
 * 它不改正式成片、不把项目标记为已拍；只用来确认时间轴、字幕与音轨。
 */
export async function previewPlan(runId: string, opts: RenderOptions = {}): Promise<RunAccepted> {
  return toJson<RunAccepted>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts),
    }),
  )
}

/** 只重拍一镜。其余镜头复用上次的片段，整片重新合成。
 *
 * 返回的 `job_id` 与整片拍摄同构——前端照样走那套 SSE 事件流，不用为它写第二套。
 */
export async function renderShot(
  runId: string,
  shotId: number,
  opts: RenderOptions = {},
): Promise<RunAccepted> {
  return toJson<RunAccepted>(
    await apiFetch(
      `/api/plan/${encodeURIComponent(runId)}/shots/${encodeURIComponent(String(shotId))}/render`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(opts),
      },
    ),
  )
}

/** 单镜低清预览：只出一镜片段，不重合成整片、不改正式产物。 */
export async function previewShot(
  runId: string,
  shotId: number,
  opts: RenderOptions = {},
): Promise<RunAccepted> {
  return toJson<RunAccepted>(
    await apiFetch(
      `/api/plan/${encodeURIComponent(runId)}/shots/${encodeURIComponent(String(shotId))}/preview`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(opts),
      },
    ),
  )
}

/** 只重合成：不重新出图，按现在的镜长与顺序把已有片段重拼一遍。
 *
 * 这是"改时间轴"的落地口。同样返回 `job_id`，前端照样走那套 SSE 事件流。
 */
export async function recomposePlan(
  runId: string,
  opts: RenderOptions = {},
): Promise<RunAccepted> {
  return toJson<RunAccepted>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/recompose`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts),
    }),
  )
}

export interface ReorderResult {
  shots: PlanShot[]
  order: number[]
  total_duration: number
  meta?: PlanMeta
  note: string
}

/** 重排镜头顺序。传的必须是现有全部镜号的一个完整排列。
 *
 * 只改顺序：镜号不动、时长不动、`work/clips` 里的文件一个都不碰。
 * 让前端去改 `id` 来实现重排，等于把文件名、`edited` 记录、单镜重拍的路径
 * 三处一起改坏，而且改坏了不报错。
 */
export async function reorderShots(
  runId: string,
  order: number[],
  expectedRevision?: number,
): Promise<ReorderResult> {
  return toJson<ReorderResult>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/shots/reorder`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order, expected_revision: expectedRevision }),
    }),
  )
}

/* ---------------------------------------------------------------- 角色卡 */

/** 改角色卡。`visual_lock` 是文本锁定，出图时拼进提示词；参考图不参与生成。 */
export async function patchSubject(
  runId: string,
  subjectId: string,
  patch: SubjectPatch,
): Promise<SubjectPatchResult> {
  return toJson<SubjectPatchResult>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/subjects/${encodeURIComponent(subjectId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  )
}

/** 上传角色参考图。只存档与人工对照，当前出图后端不会读它。 */
export async function uploadSubjectReference(
  runId: string,
  subjectId: string,
  file: File,
  expectedRevision?: number,
): Promise<SubjectReferenceResult> {
  const form = new FormData()
  form.append('file', file)
  const query = expectedRevision == null ? '' : `?expected_revision=${expectedRevision}`
  return toJson<SubjectReferenceResult>(
    await apiFetch(
      `/api/plan/${encodeURIComponent(runId)}/subjects/${encodeURIComponent(subjectId)}/reference${query}`,
      { method: 'POST', body: form },
    ),
  )
}

export async function deleteSubjectReference(
  runId: string,
  subjectId: string,
  expectedRevision?: number,
): Promise<SubjectReferenceResult> {
  const query = expectedRevision == null ? '' : `?expected_revision=${expectedRevision}`
  return toJson<SubjectReferenceResult>(
    await apiFetch(
      `/api/plan/${encodeURIComponent(runId)}/subjects/${encodeURIComponent(subjectId)}/reference${query}`,
      { method: 'DELETE' },
    ),
  )
}

/** `<img>` 带不了请求头，参考图地址要带 token/guest 查询参数。
 *
 * `version` 用参考图哈希做缓存破坏：换图后 URL 不变，浏览器可能继续显示旧图。
 */
export function subjectReferenceUrl(runId: string, subjectId: string, version?: string): string {
  const base = `/api/plan/${encodeURIComponent(runId)}/subjects/${encodeURIComponent(subjectId)}/reference`
  const credential = mediaCredentialQuery()
  if (!version) return base + credential
  return `${base}${credential}${credential ? '&' : '?'}v=${encodeURIComponent(version)}`
}
