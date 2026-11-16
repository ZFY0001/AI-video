import { useEffect, useMemo, useState } from 'react'

import { fetchProjects, type ProjectItem } from '../lib/api'
import { Badge, Button, Panel, Segmented } from '../components/ui'

type Filter = 'all' | 'draft' | 'rendered'

const STATE_META: Record<
  ProjectItem['state'],
  { label: string; tone: 'ok' | 'warn' | 'bad' | 'idle' }
> = {
  draft: { label: '草稿 · 未拍', tone: 'idle' },
  ok: { label: '已拍完', tone: 'ok' },
  canceled: { label: '已停止', tone: 'warn' },
  failed: { label: '拍失败', tone: 'bad' },
}

const FILTERS: { value: Filter; label: string; hint: string }[] = [
  { value: 'all', label: '全部', hint: '草稿与已拍' },
  { value: 'draft', label: '草稿', hint: '只拆未拍' },
  { value: 'rendered', label: '已拍', hint: '有拍摄记录' },
]

function profileText(item: ProjectItem): string {
  const profile = item.render_profile
  if (!profile) return ''
  const mode = profile.painted
    ? `代码绘制 ${profile.painted_fps ?? 24}fps`
    : profile.live
      ? '真实模型出图'
      : '离线出图'
  return `${profile.live ? '真实模型' : '离线'} · ${mode}`
}

export function ProjectsPage({ onNavigate }: { onNavigate: (next: string) => void }) {
  const [filter, setFilter] = useState<Filter>('all')
  const [items, setItems] = useState<ProjectItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchProjects(filter)
      .then((data) => {
        if (!cancelled) {
          setItems(data)
          setError('')
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '读不到项目列表')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [filter])

  const counts = useMemo(
    () => ({
      drafts: items.filter((item) => item.state === 'draft').length,
      rendered: items.filter((item) => item.state !== 'draft').length,
    }),
    [items],
  )

  return (
    <div className="mx-auto w-full max-w-[1340px] px-6 pt-7 pb-16">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">项目</h2>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">
            草稿和成片放在一起找得到。只拆未拍的项目不会再消失。
            {filter === 'all' && items.length > 0 && (
              <span className="ml-2 text-ink-mute/80">
                草稿 {counts.drafts} · 已拍 {counts.rendered}
              </span>
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Segmented options={FILTERS} value={filter} onChange={setFilter} />
          <Button onClick={() => onNavigate('create')}>新建</Button>
        </div>
      </div>

      {error && (
        <Panel className="mb-3 px-4 py-2.5">
          <p className="text-[12.5px] text-err">{error}</p>
        </Panel>
      )}

      {loading && items.length === 0 && (
        <p className="py-16 text-center text-[12.5px] text-ink-mute">正在读项目…</p>
      )}

      {!loading && items.length === 0 && !error && (
        <Panel className="px-6 py-16 text-center">
          <p className="text-[14px] font-medium">
            {filter === 'draft' ? '还没有草稿项目' : filter === 'rendered' ? '还没有拍过的项目' : '还没有项目'}
          </p>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">去创作台写一句话，点「先拆解」就会出现在这里</p>
          <div className="mt-4 flex justify-center">
            <Button variant="primary" onClick={() => onNavigate('create')}>
              去创作台
            </Button>
          </div>
        </Panel>
      )}

      {items.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {items.map((item) => {
            const meta = STATE_META[item.state] ?? STATE_META.failed
            const profile = profileText(item)
            return (
              <Panel key={item.run_id} className="flex flex-col px-5 py-4">
                <div className="flex items-start justify-between gap-3">
                  <p className="min-w-0 line-clamp-2 text-[14px] font-medium leading-snug">
                    {item.title || item.run_id}
                  </p>
                  <Badge tone={meta.tone}>{meta.label}</Badge>
                </div>

                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10.5px] text-ink-mute">
                  <span>{item.updated || '—'}</span>
                  <span>·</span>
                  <span>{item.shots} 镜</span>
                  <span>·</span>
                  <span>{item.duration}s</span>
                  <span>·</span>
                  <span title="编辑版本号">v{item.revision}</span>
                </div>

                {profile && (
                  <p className="mt-1.5 text-[11px] leading-relaxed text-ink-mute">拍摄配置：{profile}</p>
                )}

                <div className="mt-3 flex items-center justify-between gap-2 border-t border-[var(--ring)] pt-3">
                  <span className="min-w-0 truncate font-mono text-[10px] text-ink-mute" title={item.run_id}>
                    {item.run_id}
                  </span>
                  <Button small variant="primary" onClick={() => onNavigate(`plan/${item.run_id}`)}>
                    继续编辑
                  </Button>
                </div>
              </Panel>
            )
          })}
        </div>
      )}
    </div>
  )
}
