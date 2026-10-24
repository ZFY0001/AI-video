import { useCallback, useEffect, useRef, useState } from 'react'

import {
  cancelRun,
  createRun,
  eventsUrl,
  getJob,
  renderPlan,
  renderShot,
  recomposePlan,
  type EnvPayload,
  type FormState,
  type RenderOptions,
  type RunAccepted,
  type RunResult,
} from '../lib/api'
import { readSse, sleep, type SseEvent } from '../lib/sse'

export type Phase = 'idle' | 'running' | 'done' | 'error'

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
          setResult((data?.result ?? null) as RunResult | null)
          setPct(1)
          setStage('完成')
          setPhase('done')
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

  const connect = useCallback(
    async (id: string) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      let attempt = 0

      while (!controller.signal.aborted) {
        try {
          await readSse(eventsUrl(id, cursorRef.current), handleEvent, controller.signal)
          return
        } catch (err) {
          if (controller.signal.aborted) return
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
    [handleEvent],
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
        setError(err instanceof Error ? err.message : String(err))
        setPhase('error')
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
        if (snap.result) {
          setResult(snap.result)
          setPct(1)
          setStage('完成')
          setPhase('done')
        } else if (snap.finished) {
          setError(snap.error || '上次运行未产出成片')
          setPhase('error')
        } else {
          setStage('恢复连接…')
          setPhase('running')
        }
        void connect(saved)
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
  }, [connect])

  return {
    phase,
    jobId,
    stage,
    pct,
    logs,
    env,
    result,
    error,
    start,
    startPlan,
    startShot,
    startRecompose,
    cancel,
    reset,
  }
}
