import { useCallback, useEffect, useMemo, useState } from 'react'

import { cancelRun, fetchTasks, jobVideoUrl, retryTask, type TaskItem } from '../lib/api'
import { Badge, Button, Panel, Segmented } from '../components/ui'

type Filter = 'all' | 'running' | 'done' | 'failed'

const STATUS_META: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'idle' }> = {
  running: { label: '进行中', tone: 'warn' },
  done: { label: '完成', tone: 'ok' },
  canceled: { label: '已停止', tone: 'warn' },
  failed: { label: '失败', tone: 'bad' },
  interrupted: { label: '已中断', tone: 'bad' },
}

const KIND_LABEL: Record<string, string> = {
  create: '一句话成片',
  plan: '按拆解拍摄',
  shot: '单镜重拍',
  recompose: '只重合成',
  preview: '低清预览',
  preview_shot: '单镜预览',
}

const FILTERS: { value: Filter; label: string; hint: string }[] = [
  { value: 'all', label: '全部', hint: '所有运行' },
  { value: 'running', label: '进行中', hint: '还能接着看' },
  { value: 'done', label: '已完成', hint: '有产出' },
  { value: 'failed', label: '失败/中断', hint: '需要重跑' },
]

function metaOf(task: TaskItem) {
  return STATUS_META[task.status] ?? STATUS_META.failed
}

function stamp(seconds: number): string {
  if (!seconds) return '—'
  const at = new Date(seconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}

/** 拍摄配置。任务表里存的是起任务那一刻的配置，重启后照样能说明"用的什么档"。 */
function profileText(task: TaskItem): string {
  const options = task.options ?? {}
  const live = Boolean(options.live)
  const painted = Boolean(options.painted)
  if (painted) {
    const fps = Number(options.painted_fps) || 24
    return `${live ? '真实模型' : '离线'} · 代码绘制 ${fps}fps`
  }
  return live ? '真实模型出图' : '离线出图'
}

/** 只有按拆解拍的任务才有 plan 目录可回。一句话成片没有拆解页。 */
function hasPlan(task: TaskItem): boolean {
  return Boolean(task.run_id) && task.kind !== 'create'
}

function matches(task: TaskItem, filter: Filter): boolean {
  if (filter === 'all') return true
  if (filter === 'running') return task.status === 'running'
  if (filter === 'done') return task.status === 'done' || task.status === 'canceled'
  return task.status === 'failed' || task.status === 'interrupted'
}

export function TasksPage({
  onNavigate,
  onWatch,
}: {
  onNavigate: (next: string) => void
  onWatch: (jobId: string) => void
}) {
  const [items, setItems] = useState<TaskItem[]>([])
  const [filter, setFilter] = useState<Filter>('all')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const reload = useCallback((silent = false) => {
    if (!silent) setLoading(true)
    fetchTasks(80)
      .then((data) => {
        setItems(data)
        setError('')
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : '读不到任务列表'))
      .finally(() => {
        if (!silent) setLoading(false)
      })
  }, [])

  useEffect(() => {
    reload()
    // 有任务在跑时状态会变（进度、完成），静默轮询比让用户手点刷新更省事。
    const timer = window.setInterval(() => {
      if (!document.hidden) reload(true)
    }, 5000)
    return () => window.clearInterval(timer)
  }, [reload])

  const counts = useMemo(
    () => ({
      running: items.filter((task) => task.status === 'running').length,
      done: items.filter((task) => task.status === 'done' || task.status === 'canceled').length,
      failed: items.filter((task) => task.status === 'failed' || task.status === 'interrupted').length,
    }),
    [items],
  )

  const visible = items.filter((task) => matches(task, filter))

  const stop = async (jobId: string) => {
    setBusy(jobId)
    try {
      await cancelRun(jobId)
      reload(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '停止失败')
    } finally {
      setBusy('')
    }
  }

  /** 重试 = 按项目当前拆解重新起一条拍摄任务，不是重放旧快照。 */
  const retry = async (jobId: string) => {
    setBusy(jobId)
    setError('')
    try {
      const accepted = await retryTask(jobId)
      reload(true)
      onWatch(accepted.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重试失败')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="mx-auto w-full max-w-[1340px] px-6 pt-7 pb-16">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">任务</h2>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">
            每次运行都留在这里，服务重启后也查得到。进行中的任务可以接着看进度。
            {items.length > 0 && (
              <span className="ml-2 text-ink-mute/80">
                进行中 {counts.running} · 完成 {counts.done} · 失败/中断 {counts.failed}
              </span>
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Segmented options={FILTERS} value={filter} onChange={setFilter} />
          <Button onClick={() => reload()} disabled={loading}>
            刷新
          </Button>
        </div>
      </div>

      {error && (
        <Panel className="mb-3 px-4 py-2.5">
          <p className="text-[12.5px] text-err">{error}</p>
        </Panel>
      )}

      {loading && items.length === 0 && (
        <p className="py-16 text-center text-[12.5px] text-ink-mute">正在读任务记录…</p>
      )}

      {!loading && items.length === 0 && !error && (
        <Panel className="px-6 py-16 text-center">
          <p className="text-[14px] font-medium">还没有任务</p>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">
            跑一条或拍一次，这里会记下它跑得怎么样
          </p>
          <div className="mt-4 flex justify-center">
            <Button variant="primary" onClick={() => onNavigate('create')}>
              去创作台
            </Button>
          </div>
        </Panel>
      )}

      {visible.length > 0 && (
        <div className="space-y-2.5">
          {visible.map((task) => {
            const meta = metaOf(task)
            const canWatch = task.status === 'running'
            const canOpenPlan = hasPlan(task)
            const canRetry =
              task.status === 'failed' || task.status === 'interrupted'
            return (
              <Panel key={task.job_id} className="px-5 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="line-clamp-2 text-[14px] font-medium leading-snug">
                      {task.theme || '（没有标题）'}
                    </p>
                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10.5px] text-ink-mute">
                      <span>{KIND_LABEL[task.kind] ?? task.kind}</span>
                      <span>·</span>
                      <span>{stamp(task.created)}</span>
                      <span>·</span>
                      <span>{profileText(task)}</span>
                    </div>
                  </div>
                  <Badge tone={meta.tone}>{meta.label}</Badge>
                </div>

                {(task.error || task.status === 'interrupted') && (
                  <p className="mt-2 text-[11.5px] leading-relaxed text-ink-mute">
                    {task.error || '服务重启时这次运行还没结束，已完成的镜头不受影响'}
                  </p>
                )}

                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-[var(--ring)] pt-3">
                  <span className="min-w-0 truncate font-mono text-[10px] text-ink-mute" title={task.job_id}>
                    {task.job_id}
                  </span>
                  <div className="flex flex-wrap items-center gap-2">
                    {task.has_video && (
                      <Button
                        small
                        onClick={() => window.open(jobVideoUrl(task.job_id), '_blank', 'noopener')}
                      >
                        看成片
                      </Button>
                    )}
                    {canOpenPlan && (
                      <Button small onClick={() => onNavigate(`plan/${task.run_id}`)}>
                        打开拆解
                      </Button>
                    )}
                    {canRetry && (
                      <Button
                        small
                        variant="primary"
                        disabled={busy === task.job_id || task.kind === 'create'}
                        title={
                          task.kind === 'create'
                            ? '一句话成片的创作参数没有完整保存；请回创作台重新发起'
                            : '按项目当前拆解重新起一条拍摄任务'
                        }
                        onClick={() => void retry(task.job_id)}
                      >
                        重试
                      </Button>
                    )}
                    {canWatch && (
                      <>
                        <Button small variant="primary" onClick={() => onWatch(task.job_id)}>
                          接着看
                        </Button>
                        <Button
                          small
                          variant="danger"
                          disabled={busy === task.job_id}
                          onClick={() => void stop(task.job_id)}
                        >
                          停止
                        </Button>
                      </>
                    )}
                  </div>
                </div>
              </Panel>
            )
          })}
        </div>
      )}

      {!loading && items.length > 0 && visible.length === 0 && (
        <Panel className="px-6 py-12 text-center">
          <p className="text-[13px] text-ink-mute">这个筛选下没有任务</p>
        </Panel>
      )}
    </div>
  )
}