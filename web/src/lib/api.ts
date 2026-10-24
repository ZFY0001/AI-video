/** REST 封装与类型。字段名与后端 `RunOptions` 严格一致，不做任何改名。 */

import { getToken } from './auth'

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

export interface FormState {
  theme: string
  duration: number
  aspect: string
  style: string
  shot_len: number
  max_shots: number | null
  live: boolean
  guides: boolean
  no_cache: boolean
  painted: boolean
  painted_fps: number
  painted_quality: 'fast' | 'medium' | 'high'
  clean_frames: boolean
  /** 只允许真图：勾上后不挂占位帧兜底，ComfyUI 不可用会直接报错 */
  require_real_image: boolean

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
    shot_len: form.shot_len,
    max_shots: form.max_shots,
    live: form.live,
    guides: form.guides,
    no_cache: form.no_cache,
    painted: form.painted,
    painted_fps: form.painted_fps,
    painted_quality: form.painted_quality,
    clean_frames: form.clean_frames,
    require_real_image: form.require_real_image,
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
}

export interface RunAccepted {
  job_id: string
  theme: string
  running: boolean
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

export async function cancelRun(jobId: string): Promise<void> {
  const res = await apiFetch(`/api/runs/${jobId}/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error(`停止失败（HTTP ${res.status}）`)
}

export function eventsUrl(jobId: string, cursor: number): string {
  return `/api/runs/${jobId}/events?cursor=${cursor}`
}

export function videoUrl(jobId: string): string {
  return `/api/runs/${jobId}/video`
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
}

export async function fetchHistory(limit = 60, mine = false): Promise<HistoryItem[]> {
  const query = `limit=${limit}${mine ? '&mine=true' : ''}`
  const data = await toJson<{ items: HistoryItem[] }>(
    await apiFetch(`/api/history?${query}`, { cache: 'no-store' }),
  )
  return data.items
}

export async function fetchRunDetail(runId: string): Promise<RunDetail> {
  return toJson<RunDetail>(await apiFetch(`/api/history/${encodeURIComponent(runId)}`, { cache: 'no-store' }))
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
function mediaToken(): string {
  const token = getToken()
  return token ? `?token=${encodeURIComponent(token)}` : ''
}

export function historyVideoUrl(runId: string): string {
  return `/api/history/${encodeURIComponent(runId)}/video${mediaToken()}`
}

export function historyCoverUrl(runId: string): string {
  return `/api/history/${encodeURIComponent(runId)}/cover${mediaToken()}`
}

export function historySubtitleUrl(runId: string): string {
  return `/api/history/${encodeURIComponent(runId)}/subtitle${mediaToken()}`
}

export async function fetchSubtitle(runId: string): Promise<string> {
  const res = await apiFetch(historySubtitleUrl(runId), { cache: 'no-store' })
  if (!res.ok) throw new Error('没有字幕文件')
  return res.text()
}

/* ---------------------------------------------------------------- 环境 */

/* ---------------------------------------------------------------- 设备 */

/** 后端真实探测的设备画像（pipeline/hardware.py）。 */
export interface DeviceInfo {
  checked_at: string
  gpu: { vendor: string; name: string; vram_gb: number; source: string; accurate: boolean }
  cpu: { cores: number }
  ram_gb: number
  disk: { free_gb: number; enough_for_model: boolean; volumes: Array<{ label: string; root: string; free_gb: number; total_gb: number }> }
  os: { system: string; release: string; version: string; friendly: string }
  runtimes: { python: string; python_ok: boolean; node: string; node_ok: boolean }
  webview2: { installed: boolean; version: string; source: string }
  cloud: Record<string, { configured: boolean; reachable: boolean; detail: string }>
  comfyui: {
    installed: boolean
    usable: boolean
    pick: string
    locations: Array<{ path: string; has_main: boolean; models: string[]; model_count: number }>
  }
  /** tier: sdxl / sdxl_low / sd15 / none */
  verdict: {
    tier: string
    reason: string
    local_allowed: boolean
    ready_now: boolean
    needs_cloud: boolean
  }
  advice: string[]
  platform: string
}

/** 重新检测并保存体检报告（json + markdown 落到 work/ 下）。 */
export async function saveDeviceReport(): Promise<{
  saved: boolean
  report: DeviceInfo
  paths: { json: string; markdown: string }
}> {
  return toJson(await apiFetch('/api/device/report', { method: 'POST' }))
}

/** markdown 报告的直链，浏览器点开就能另存。 */
export const DEVICE_REPORT_MD_URL = '/api/device/report.md'

export async function fetchDevice(): Promise<DeviceInfo> {
  return toJson<DeviceInfo>(await apiFetch('/api/device', { cache: 'no-store' }))
}

export async function fetchEnv(fresh = false): Promise<EnvPayload> {
  return toJson<EnvPayload>(await apiFetch(`/api/env?fresh=${fresh}`, { cache: 'no-store' }))
}

export async function saveEnv(pairs: Record<string, string>): Promise<string> {
  const data = await toJson<{ detail: string }>(
    await apiFetch('/api/env', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pairs),
    }),
  )
  return data.detail
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
}

export interface PlanSubject {
  id: string
  name: string
  visual_lock?: string
}

export interface PlanMeta {
  run_id?: string
  live?: boolean
  model?: string
  created?: string
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
  notes?: string[]
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
  live?: boolean
  painted?: boolean
  painted_fps?: number
  painted_quality?: 'fast' | 'medium' | 'high'
  clean_frames?: boolean
  audio?: string
  lyrics?: string
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
): Promise<ReplanResult> {
  return toJson<ReplanResult>(
    await apiFetch(
      `/api/plan/${encodeURIComponent(runId)}/acts/${encodeURIComponent(actName)}/replan`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force }),
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
export async function reorderShots(runId: string, order: number[]): Promise<ReorderResult> {
  return toJson<ReorderResult>(
    await apiFetch(`/api/plan/${encodeURIComponent(runId)}/shots/reorder`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order }),
    }),
  )
}
