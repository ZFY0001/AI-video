import { useCallback, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  cancelRun,
  createRun,
  eventsUrl,
  getJob,
  renderPlan,
  renderShot,
  recomposePlan,
  previewPlan,
  previewShot,
  type EnvPayload,
  type FormState,
  type JobSnapshot,
  type RenderOptions,
  type RunAccepted,
  type RunResult,
} from '../lib/api'
import { authHeaders, guestHeaders } from '../lib/auth'
import { readSse, sleep, type SseEvent } from '../lib/sse'

export type Phase = 'idle' | 'running' | 'done' | 'canceled' | 'error' | 'rejected'

export interface LogItem {
  key: number
  level: string
  msg: string
  at: number
}

const STORAGE_KEY = 'yiyan.job'
const MAX_LOGS = 800

export type UseRun = ReturnType<typeof useRun>

/**
 * 一次运行的全部前端状态。
 *
 * 日志不走 useState 直接 push：管线一秒能吐十几条，逐条 setState 会把整个页面
 * 重渲染十几遍。这里先进 ref，再用 requestAnimationFrame 合并成每帧一次。
 */
export function useRun() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [jobId, setJobId] = useState('')
  const [stage, setStage] = useState('待命')
  const [pct, setPct] = useState(0)
  const [logs, setLogs] = useState<LogItem[]>([])
  const [env, setEnv] = useState<EnvPayload | null>(null)
  const [result, setResult] = useState<RunResult | null>(null)
  const [error, setError] = useState('')

  const abortRef = useRef<AbortController | null>(null)
  const cursorRef = useRef(0)
  const logsRef = useRef<LogItem[]>([])
  const rafRef = useRef(0)
  const aliveRef = useRef(true)

  const flush = useCallback(() => {
    rafRef.current = 0
    if (!aliveRef.current) return
    setLogs(logsRef.current.slice(-MAX_LOGS))
  }, [])

  const scheduleFlush = useCallback(() => {
    if (rafRef.current) return
    rafRef.current = requestAnimationFrame(flush)
  }, [flush])

  const handleEvent = useCallback(
    (ev: SseEvent) => {
      if (ev.id >= 0) cursorRef.current = ev.id + 1
      const data = ev.data as Record<string, unknown>

      switch (ev.event) {
        case 'log': {
          logsRef.current.push({
            key: ev.id >= 0 ? ev.id : Date.now() + Math.random(),
            level: String(data?.level ?? 'info'),
            msg: String(data?.msg ?? ''),
            at: Number(data?.ts ?? Date.now() / 1000),
          })
          if (logsRef.current.length > MAX_LOGS + 100) {
            logsRef.current.splice(0, logsRef.current.length - MAX_LOGS)
          }
          scheduleFlush()
          break
        }
        case 'progress': {
          setStage(String(data?.stage ?? ''))
          setPct(Number(data?.pct ?? 0))
          break
        }
        case 'env': {
          setEnv(data as unknown as EnvPayload)
          break
        }
        case 'done': {
          const payload = (data?.result ?? null) as RunResult | null
          const stopped = Boolean(payload?.stopped)
          setResult(payload)
          setPct(1)
          setStage(stopped ? '已停止' : '完成')
          setPhase(stopped ? 'canceled' : 'done')
          break
        }
        case 'error': {
          setError(String(data?.message ?? '运行失败'))
          setPhase('error')
          break
        }
        default:
          break
      }
    },
    [scheduleFlush],
  )

  /** 用快照收尾：连接断了不代表任务失败，任务状态以服务端快照为准。 */
  const applySnapshot = useCallback((snap: JobSnapshot) => {
    const stopped = Boolean(snap.result?.stopped) || snap.status === 'canceled'
    if (snap.result) {
      setResult(snap.result)
      setPct(1)
      setStage(stopped ? '已停止' : '完成')
      // 不能按“有没有 result”推断成功：取消也有部分产物，但状态是 canceled。
      setPhase(stopped ? 'canceled' : 'done')
    } else if (snap.status === 'interrupted') {
      setError(snap.error || '服务重启，这次运行被中断')
      setPhase('error')
    } else if (snap.finished) {
      setError(snap.error || '上次运行未产出成片')
      setPhase('error')
    }
  }, [])

  const connect = useCallback(
    async (id: string) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      let attempt = 0
      let earlyEnds = 0

      while (!controller.signal.aborted) {
        try {
          const outcome = await readSse(
            eventsUrl(id, cursorRef.current),
            handleEvent,
            controller.signal,
            { ...authHeaders(), ...guestHeaders() },
          )
          if (controller.signal.aborted) return
          // 收到 done/error 才是真正结束。
          if (outcome.terminal) return

          // 流正常关闭却没有终态（代理截断、服务重启等）：先查快照。
          // 以前这里直接 return，界面就永远停在「生成中」。
          const snap = await getJob(id)
          if (controller.signal.aborted) return
          attempt = 0
          if (snap.finished) {
            applySnapshot(snap)
            return
          }
          earlyEnds += 1
          if (earlyEnds > 8) {
            setError('事件流反复中断，但任务仍在运行；可稍后刷新页面继续查看')
            setPhase('error')
            return
          }
          setStage('正在恢复事件流…')
          await sleep(Math.min(400 * earlyEnds, 3000))
        } catch (err) {
          if (controller.signal.aborted) return
          // 404 = 任务真的不在了（服务重启），继续重连没有意义。
          if (err instanceof ApiError && err.status === 404) {
            setError('这条任务没有记录（可能是账号体系上线前的旧运行）')
            setPhase('error')
            return
          }
          attempt += 1
          if (attempt > 6) {
            setError(err instanceof Error ? err.message : '事件流中断')
            setPhase('error')
            return
          }
          await sleep(Math.min(500 * 1.8 ** (attempt - 1), 4000))
        }
      }
    },
    [applySnapshot, handleEvent],
  )

  const begin = useCallback(
    async (launch: () => Promise<RunAccepted>) => {
      abortRef.current?.abort()
      cursorRef.current = 0
      logsRef.current = []
      setLogs([])
      setResult(null)
      setEnv(null)
      setError('')
      setPct(0)
      setStage('准备')
      setPhase('running')

      try {
        const accepted = await launch()
        setJobId(accepted.job_id)
        sessionStorage.setItem(STORAGE_KEY, accepted.job_id)
        void connect(accepted.job_id)
      } catch (err) {
        // 提交就被拒（预算闸、并发占用、参数不合法）：管线一步都没跑，
        // 不能报成"已中断"——那会让用户以为已经拍了一部分。
        setError(err instanceof Error ? err.message : String(err))
        setStage('待命')
        setPhase('rejected')
      }
    },
    [connect],
  )

  const start = useCallback((form: FormState) => begin(() => createRun(form)), [begin])

  /** 按拆解文件拍摄。与 `start` 只差一个入口，事件流、取消、恢复完全共用。 */
  const startPlan = useCallback(
    (runId: string, opts: RenderOptions = {}) => begin(() => renderPlan(runId, opts)),
    [begin],
  )

  /** 只重拍一镜。同样复用上面那套事件流——它返回的 job_id 与整片拍摄同构。 */
  const startShot = useCallback(
    (runId: string, shotId: number, opts: RenderOptions = {}) =>
      begin(() => renderShot(runId, shotId, opts)),
    [begin],
  )

  /** 只重合成：改过镜长或顺序之后重拼一遍，一个像素都不重画。 */
  const startRecompose = useCallback(
    (runId: string, opts: RenderOptions = {}) => begin(() => recomposePlan(runId, opts)),
    [begin],
  )

  /** 低清预览：半分辨率快速出一版，用来核对时间轴/字幕/音轨。
   *  与正式拍摄共用事件流，结果里会带 preview 标记。 */
  const startPreview = useCallback(
    (runId: string, opts: RenderOptions = {}) => begin(() => previewPlan(runId, opts)),
    [begin],
  )

  /** 单镜低清预览：只出一镜片段，不重合成整片，也不动正式片段缓存。 */
  const startShotPreview = useCallback(
    (runId: string, shotId: number, opts: RenderOptions = {}) =>
      begin(() => previewShot(runId, shotId, opts)),
    [begin],
  )

  /** 从任务中心接回一条正在跑的任务。
   *
   * 与"刷新页面自动恢复"同一条路：先查快照，跑完的直接出结果，还在跑的再订阅。
   * 区别是要先把上一个任务的日志与结果清掉，否则两条运行的记录会混在一起。
   */
  const attach = useCallback(
    async (id: string) => {
      abortRef.current?.abort()
      cursorRef.current = 0
      logsRef.current = []
      setLogs([])
      setResult(null)
      setEnv(null)
      setError('')
      setPct(0)
      setJobId(id)
      sessionStorage.setItem(STORAGE_KEY, id)
      try {
        const snap = await getJob(id)
        if (snap.finished) {
          applySnapshot(snap)
          return
        }
        setStage('恢复连接…')
        setPhase('running')
        void connect(id)
      } catch (err) {
        setError(err instanceof Error ? err.message : '接不回这条任务')
        setPhase('error')
      }
    },
    [applySnapshot, connect],
  )

  const cancel = useCallback(async () => {
    if (!jobId) return
    try {
      await cancelRun(jobId)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [jobId])

  const reset = useCallback(() => {
    abortRef.current?.abort()
    sessionStorage.removeItem(STORAGE_KEY)
    cursorRef.current = 0
    logsRef.current = []
    setLogs([])
    setResult(null)
    setError('')
    setPct(0)
    setStage('待命')
    setJobId('')
    setPhase('idle')
  }, [])

  // 刷新页面后接回上一次的任务：任务还在跑就继续看日志，已跑完就直接出结果。
  // 这也是「关掉页面再打开能看到之前的过程」的实现处。
  useEffect(() => {
    aliveRef.current = true
    const saved = sessionStorage.getItem(STORAGE_KEY)
    if (!saved) {
      return () => {
        aliveRef.current = false
      }
    }

    let cancelled = false
    void (async () => {
      try {
        const snap = await getJob(saved)
        if (cancelled) return
        setJobId(saved)
        if (snap.finished) {
          // 已经结束的任务不需要再接事件流；快照就是最终状态。
          applySnapshot(snap)
        } else {
          setStage('恢复连接…')
          setPhase('running')
          void connect(saved)
        }
      } catch {
        sessionStorage.removeItem(STORAGE_KEY)
      }
    })()

    return () => {
      cancelled = true
      aliveRef.current = false
      abortRef.current?.abort()
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
    }
  }, [applySnapshot, connect])

  return {
    phase,
    jobId,
    stage,
    pct,
    logs,
    env,
    // 设置页体检完把结果交回全局，顶栏的环境徽章才能跟着变。
    publishEnv: setEnv,
    result,
    error,
    start,
    startPlan,
    startShot,
    startShotPreview,
    startRecompose,
    startPreview,
    attach,
    cancel,
    reset,
  }
}
