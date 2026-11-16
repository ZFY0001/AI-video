import { useEffect, useMemo, useRef, useState } from 'react'

import {
  fetchEnv,
  fetchProviders,
  fetchUsage,
  saveEnv,
  type EnvCheck,
  type EnvPayload,
  type ProviderCatalog,
  type ProviderItem,
  type UsagePayload,
} from '../lib/api'
import { useAuth } from '../lib/auth'
import { Badge, Button, Panel, SectionTitle } from '../components/ui'

type Tone = 'ok' | 'warn' | 'bad' | 'idle'

const KIND_LABEL: Record<string, string> = {
  paint: '绘制',
  image: '出图',
  video: '视频',
  tts: '配音',
  text: '文本',
}

const KIND_ORDER = ['paint', 'image', 'video', 'tts', 'text']

function toneOf(state: string): Tone {
  if (state === 'ok') return 'ok'
  if (state === 'warn') return 'warn'
  if (state === 'bad') return 'bad'
  return 'idle'
}

function stateColor(state: string): string {
  if (state === 'ok') return 'var(--color-ok)'
  if (state === 'warn') return 'var(--color-warn)'
  if (state === 'bad') return 'var(--color-err)'
  return 'var(--ctl-edge)'
}

function stateText(state: string): string {
  if (state === 'ok') return '就绪'
  if (state === 'warn') return '降级'
  return '缺失'
}

/** 把探针原始输出压成一句人话。
 *
 * 后端给的是诊断原文：ffmpeg 版权横幅、绝对探测路径、"ok ｜ 参考图：否"。
 * 直接当正文渲染，设置页就变成一屏日志——用户要的是「这项能不能用」，
 * 原文收进「原始输出」里，需要排查时再展开。
 */
function humanDetail(name: string, raw: string): string {
  const text = String(raw || '').trim()
  if (!text) return '—'
  if (name.includes('silent')) return '由 FFmpeg 铺等长静音轨，不需要配音引擎'
  const version = /ffmpeg version (\S+)/.exec(text)
  if (version) return `FFmpeg ${version[1]}`
  return (
    text
      .replace(/（探测位置：[^）]*）/g, '')
      .replace(/\s*｜\s*参考图：[^｜]*/g, '')
      .replace(/^ok$/i, '就绪')
      .trim() || '—'
  )
}

type FieldGroup = 'text' | 'vlm' | 'local'

const GROUPS: { key: FieldGroup; title: string; hint: string }[] = [
  { key: 'text', title: '文本模型', hint: '创意、剧本与分镜由它写；不配就走离线引擎' },
  { key: 'vlm', title: '视觉评审', hint: '配了质检才真正读图；留空只做技术检查 + 模拟评审' },
  { key: 'local', title: '本地服务与预算', hint: '本机出图服务与单次付费上限' },
]

const FIELDS: {
  key: string
  label: string
  hint: string
  group: FieldGroup
  wide?: boolean
  secret?: boolean
  placeholder: string
}[] = [
  {
    key: 'LLM_API_KEY',
    label: '文本模型 Key',
    hint: '不配就走离线创意，配了才轮到真实模型',
    group: 'text',
    secret: true,
    placeholder: 'sk-…',
  },
  {
    key: 'LLM_MODEL',
    label: '模型名',
    hint: '留空用默认',
    group: 'text',
    placeholder: 'deepseek-chat',
  },
  {
    key: 'LLM_BASE_URL',
    label: '服务地址',
    hint: '兼容 OpenAI 协议即可',
    group: 'text',
    wide: true,
    placeholder: 'https://api.deepseek.com/v1',
  },
  {
    key: 'VLM_MODEL',
    label: '评审模型名',
    hint: '留空则不读图',
    group: 'vlm',
    placeholder: 'qwen-vl-plus / gpt-4o-mini',
  },
  {
    key: 'VLM_API_KEY',
    label: '评审 Key',
    hint: '留空沿用文本模型的 Key',
    group: 'vlm',
    secret: true,
    placeholder: 'sk-…',
  },
  {
    key: 'VLM_BASE_URL',
    label: '评审服务地址',
    hint: '留空沿用文本模型的地址',
    group: 'vlm',
    wide: true,
    placeholder: 'https://api.example.com/v1',
  },
  {
    key: 'COMFYUI_BASE_URL',
    label: 'ComfyUI 地址',
    hint: '没启动会自动降级为离线占位帧',
    group: 'local',
    placeholder: 'http://127.0.0.1:8188',
  },
  {
    key: 'BUDGET_CNY',
    label: '单次预算上限（元）',
    hint: '云端视频估价超过它就在提交前拒绝；本地出片不受影响',
    group: 'local',
    placeholder: '20',
  },
]

const SECTIONS = [
  { key: 'keys', label: '密钥与模型' },
  { key: 'generators', label: '生成器' },
  { key: 'usage', label: '费用台账' },
  { key: 'env', label: '环境体检' },
] as const

type SectionKey = (typeof SECTIONS)[number]['key']

/** 状态点：颜色 + 发光；色盲用户还能靠右侧文字徽章区分。 */
function Dot({ color, className = '' }: { color: string; className?: string }) {
  return (
    <span
      className={`block h-[7px] w-[7px] shrink-0 rounded-full ${className}`}
      style={{ background: color, color, boxShadow: '0 0 8px currentColor' }}
      aria-hidden="true"
    />
  )
}

/** 原始诊断输出：等宽、可换行、默认收起。 */
function RawOutput({ text }: { text: string }) {
  if (!text) return null
  return (
    <details className="mt-1.5">
      <summary className="w-fit cursor-pointer list-none text-[11px] text-ink-mute transition-colors hover:text-ink">
        原始输出
      </summary>
      <p className="mt-1.5 rounded-lg border border-[var(--ring)] bg-[var(--sunk)] px-2.5 py-2 font-mono text-[10.5px] leading-relaxed break-all text-ink-dim">
        {text}
      </p>
    </details>
  )
}

function EnvRow({ item }: { item: EnvCheck }) {
  return (
    <div className="border-b border-[var(--ring)] py-2 last:border-b-0">
      <div className="flex items-center gap-2.5">
        <Dot color={stateColor(item.state)} />
        <span className="min-w-0 flex-1 truncate text-[12.5px]" title={item.name}>
          {item.name}
        </span>
        <Badge tone={toneOf(item.state)}>{stateText(item.state)}</Badge>
      </div>
      <p className="mt-1 pl-[17px] text-[11px] leading-relaxed text-ink-mute line-clamp-2">
        {humanDetail(item.name, item.detail)}
      </p>
      <div className="pl-[17px]">
        <RawOutput text={item.detail} />
      </div>
    </div>
  )
}

/** 一个生成器一行：状态与计费一眼可见，原因和原始探测输出点开才看。
 *
 * 以前每张卡都把四段诊断文字平铺出来，九个后端叠成一屏半，
 * 真正要填的表单被推到折叠线以下。 */
function ProviderCard({ item }: { item: ProviderItem }) {
  const tone: Tone = item.available ? 'ok' : item.configured ? 'warn' : 'idle'
  const state = item.available ? '可用' : item.configured ? '未接入' : '未配置'
  const color = item.available
    ? 'var(--color-ok)'
    : item.configured
      ? 'var(--color-warn)'
      : 'var(--ctl-edge)'
  const detail = humanDetail(item.label, item.detail)
  return (
    <details className="group border-b border-[var(--ring)] py-1.5 last:border-b-0">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-2 gap-y-1">
        <Dot color={color} />
        <span className="text-[12.5px] font-medium">{item.label}</span>
        <Badge tone={tone}>{state}</Badge>
        <span className="tag-chip">{item.cost_label}</span>
        {item.reference_images != null && (
          <span className="tag-chip">参考图{item.reference_images ? '支持' : '不支持'}</span>
        )}
        <span className="ml-auto shrink-0 text-[10.5px] text-ink-mute transition-colors group-hover:text-ink">
          <span className="group-open:hidden">详情</span>
          <span className="hidden group-open:inline">收起</span>
        </span>
        {!item.available && (
          <span className="w-full pl-[17px] text-[11px] leading-snug text-ink-mute line-clamp-1">
            {detail}
          </span>
        )}
      </summary>
      <div className="mt-1.5 pl-[17px]">
        <p className="text-[11.5px] leading-relaxed text-ink-dim">{detail}</p>
        <dl className="mt-1.5 space-y-1 text-[11px] leading-relaxed">
          {item.reference_detail && (
            <div className="flex gap-2">
              <dt className="w-[52px] shrink-0 text-ink-mute">参考图</dt>
              <dd className="min-w-0 flex-1 text-ink-dim">{item.reference_detail}</dd>
            </div>
          )}
          <div className="flex gap-2">
            <dt className="w-[52px] shrink-0 text-ink-mute">计费</dt>
            <dd className="min-w-0 flex-1 text-ink-dim">{item.price_hint}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="w-[52px] shrink-0 text-ink-mute">测试口径</dt>
            <dd className="min-w-0 flex-1 text-ink-dim">{item.test}</dd>
          </div>
        </dl>
        <RawOutput text={item.detail} />
      </div>
    </details>
  )
}

export function SettingsPage({ onEnvChange }: { onEnvChange?: (env: EnvPayload) => void } = {}) {
  const { user } = useAuth()
  const [env, setEnv] = useState<EnvPayload | null>(null)
  const [checking, setChecking] = useState(false)
  const [providers, setProviders] = useState<ProviderCatalog | null>(null)
  const [usage, setUsage] = useState<UsagePayload | null>(null)
  const [probing, setProbing] = useState(false)
  const [form, setForm] = useState<Record<string, string>>({})
  const [shown, setShown] = useState<Record<string, boolean>>({})
  /** 明确要清除的键。"输入框留空"是保留，不是删除。 */
  const [deletes, setDeletes] = useState<Record<string, boolean>>({})
  const [message, setMessage] = useState('')
  const [messageTone, setMessageTone] = useState<'ok' | 'bad'>('ok')
  const [saving, setSaving] = useState(false)
  const [checkedAt, setCheckedAt] = useState('')
  const [active, setActive] = useState<SectionKey>('keys')
  const timer = useRef<number | undefined>(undefined)
  const envSink = useRef(onEnvChange)
  envSink.current = onEnvChange

  const check = (fresh: boolean) => {
    setChecking(true)
    fetchEnv(fresh)
      .then((data) => {
        setEnv(data)
        envSink.current?.(data)
        setMessage('')
        if (data.pending) {
          window.clearTimeout(timer.current)
          timer.current = window.setTimeout(() => check(false), 1500)
        } else {
          setCheckedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
        }
      })
      .catch((err: unknown) => {
        setMessageTone('bad')
        setMessage(err instanceof Error ? err.message : '体检失败')
      })
      .finally(() => setChecking(false))
  }

  useEffect(() => {
    check(false)
    return () => window.clearTimeout(timer.current)
  }, [])

  /** 生成器目录与费用台账。测试只探测可用性，不生成收费样片。 */
  const loadProviderInfo = (fresh: boolean) => {
    setProbing(true)
    Promise.all([fetchProviders(fresh), fetchUsage()])
      .then(([catalog, ledger]) => {
        setProviders(catalog)
        setUsage(ledger)
      })
      .catch((err: unknown) => {
        setMessageTone('bad')
        setMessage(err instanceof Error ? err.message : '读取生成器状态失败')
      })
      .finally(() => setProbing(false))
  }

  useEffect(() => {
    loadProviderInfo(false)
  }, [])

  const pendingWrites = Object.entries(form).filter(
    ([key, value]) => value.trim() !== '' && !deletes[key],
  )
  const pendingDeletes = Object.keys(deletes).filter((key) => deletes[key])
  const dirty = pendingWrites.length + pendingDeletes.length
  const dirtyLabel = dirty
    ? `${pendingWrites.length} 项待写入${pendingDeletes.length ? ` · ${pendingDeletes.length} 项待清除` : ''}`
    : ''

  const discard = () => {
    setForm({})
    setDeletes({})
    setMessage('')
  }

  const save = async () => {
    // 三态：输入框有值 = 替换；标了清除 = 删除；其余 = 保留。
    const values = Object.fromEntries(pendingWrites)
    if (Object.keys(values).length === 0 && pendingDeletes.length === 0) {
      setMessageTone('bad')
      setMessage('没有要写入或清除的内容')
      return
    }
    setSaving(true)
    try {
      const res = await saveEnv({ values, delete: pendingDeletes })
      setMessageTone('ok')
      setMessage(res.detail)
      setForm({})
      setDeletes({})
      if (res.configured) {
        setEnv((prev) => (prev ? { ...prev, configured: res.configured } : prev))
      }
      check(true)
      loadProviderInfo(true)
    } catch (err: unknown) {
      setMessageTone('bad')
      setMessage(err instanceof Error ? err.message : '写入失败')
    } finally {
      setSaving(false)
    }
  }

  const checks = env?.checks ?? []
  const degradedCount = checks.filter((item) => item.state !== 'ok').length
  const items = providers?.items ?? []
  const availableCount = items.filter((item) => item.available).length

  const grouped = useMemo(() => {
    const map = new Map<string, ProviderItem[]>()
    for (const item of items) {
      const list = map.get(item.kind) ?? []
      list.push(item)
      map.set(item.kind, list)
    }
    const kinds = [
      ...KIND_ORDER.filter((kind) => map.has(kind)),
      ...[...map.keys()].filter((kind) => !KIND_ORDER.includes(kind)),
    ]
    return kinds.map((kind) => ({ kind, list: map.get(kind) ?? [] }))
  }, [items])

  const jump = (key: SectionKey) => {
    setActive(key)
    document.getElementById(`settings-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const counts: Record<SectionKey, string> = {
    keys: env?.live_ready ? '已配 Key' : '未配 Key',
    env: degradedCount ? `${degradedCount} 项降级` : '全部就绪',
    generators: providers ? `${availableCount}/${items.length} 可用` : '探测中',
    usage: `¥${usage?.total_cny ?? 0}`,
  }

  return (
    <div className="mx-auto w-full max-w-[1240px] px-4 pt-6 pb-20 sm:px-6">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h2 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">设置</h2>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">
            填密钥、看依赖状态。配好 Key 之后真实模型才会接管，否则全程走本地方案。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          <span className="font-mono text-[10.5px] tracking-[0.06em] text-ink-mute">
            {checkedAt ? `检测于 ${checkedAt}` : '正在检测…'}
          </span>
          <Badge tone={env?.ready ? 'ok' : 'warn'}>{env?.ready ? '环境就绪' : '有降级项'}</Badge>
        </div>
      </div>

      {/* 常驻操作条：保存按钮不再埋在两千像素长的页面底部。
          锚点 + 待写入计数 + 保存/放弃，滚到任何位置都够得着。 */}
      <div className="sticky top-[100px] z-30 -mx-4 mb-4 border-y border-[var(--ring)] bg-[var(--nav-bg)] px-4 py-2 backdrop-blur-2xl sm:-mx-6 sm:px-6 md:top-[56px]">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <nav className="scroll-thin -mb-1 flex min-w-0 flex-1 items-center gap-1 overflow-x-auto pb-1">
            {SECTIONS.map((section) => (
              <button
                key={section.key}
                type="button"
                onClick={() => jump(section.key)}
                aria-current={active === section.key ? 'true' : undefined}
                className={[
                  'flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] transition-colors',
                  active === section.key
                    ? 'bg-[var(--ctl)] font-medium text-ink'
                    : 'text-ink-dim hover:bg-[var(--ctl)]/60 hover:text-ink',
                ].join(' ')}
              >
                {section.label}
                <span className="font-mono text-[10px] text-ink-mute">{counts[section.key]}</span>
              </button>
            ))}
          </nav>
          <div className="flex shrink-0 items-center gap-2">
            <span className="hidden text-[11.5px] text-ink-mute lg:inline">{dirtyLabel}</span>
            {dirty > 0 && (
              <Button small onClick={discard} disabled={saving}>
                放弃
              </Button>
            )}
            <Button
              small
              variant="primary"
              onClick={() => void save()}
              disabled={saving || !user || dirty === 0}
              title={user ? '写回本机 .env' : '写入配置需要登录'}
            >
              {saving ? '写入中…' : '保存到 .env'}
            </Button>
          </div>
        </div>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_330px]">
        <div className="min-w-0 space-y-4">
          <Panel
            id="settings-keys"
            className="scroll-mt-[176px] px-4 py-4 sm:px-5 md:scroll-mt-[124px]"
          >
            <SectionTitle
              title="密钥与模型"
              hint="只替换命中的行，注释和其他配置原样保留；留空 = 保留原值"
              right={
                <Badge tone={env?.live_ready ? 'ok' : 'idle'}>
                  {env?.live_ready ? '已配 Key' : '未配 Key'}
                </Badge>
              }
            />

            {message && (
              <div
                className={[
                  'mt-3 flex items-start gap-2 rounded-xl border px-3 py-2.5',
                  messageTone === 'ok' ? 'border-ok/25 bg-ok/[0.07]' : 'border-err/25 bg-err/[0.07]',
                ].join(' ')}
                role="status"
              >
                <p
                  className={`min-w-0 flex-1 text-[11.5px] leading-relaxed ${messageTone === 'ok' ? 'text-ok' : 'text-err'}`}
                >
                  {message}
                </p>
                <button
                  type="button"
                  onClick={() => setMessage('')}
                  aria-label="关闭提示"
                  className="shrink-0 rounded px-1 text-[11.5px] text-ink-mute transition-colors hover:text-ink"
                >
                  ✕
                </button>
              </div>
            )}

            {!user && (
              <p className="mt-3 rounded-xl border border-[var(--ring)] bg-[var(--sunk)] px-3 py-2.5 text-[11.5px] leading-relaxed text-ink-dim">
                写入配置需要登录——配置会决定模型与密钥的去向。可以先查看各项状态。
              </p>
            )}

            <div className="mt-3 space-y-4">
              {GROUPS.map((group) => (
                <section key={group.key}>
                  <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5 border-b border-[var(--ring)] pb-1.5">
                    <h3 className="text-[12.5px] font-semibold tracking-[-0.01em]">{group.title}</h3>
                    <p className="min-w-0 flex-1 text-[11px] text-ink-mute">{group.hint}</p>
                  </div>
                  <div className="mt-3 grid gap-x-4 gap-y-3.5 sm:grid-cols-2">
                    {FIELDS.filter((field) => field.group === group.key).map((field) => {
                      const saved = Boolean(env?.configured?.[field.key])
                      const marked = Boolean(deletes[field.key])
                      const id = `setting-${field.key}`
                      return (
                        <div key={field.key} className={field.wide ? 'sm:col-span-2' : 'min-w-0'}>
                          <div className="mb-1.5 flex items-baseline justify-between gap-2">
                            <label htmlFor={id} className="text-[12.5px] font-medium text-ink-dim">
                              {field.label}
                            </label>
                            <span
                              className={`shrink-0 font-mono text-[10px] tracking-[0.04em] ${marked ? 'text-warn' : saved ? 'text-ok' : 'text-ink-mute'}`}
                            >
                              {marked ? '待清除' : saved ? '已保存' : '未配置'}
                            </span>
                          </div>
                          <div className="flex items-center gap-1.5">
                            <input
                              id={id}
                              type={field.secret && !shown[field.key] ? 'password' : 'text'}
                              className="field-input min-w-0 flex-1 text-[12.5px]"
                              placeholder={saved ? '留空表示保留' : field.placeholder}
                              value={form[field.key] ?? ''}
                              disabled={!user}
                              onChange={(event) => {
                                setForm((prev) => ({ ...prev, [field.key]: event.target.value }))
                                // 重新输入等于"替换"，撤销之前的清除标记。
                                setDeletes((prev) => ({ ...prev, [field.key]: false }))
                              }}
                              spellCheck={false}
                              autoComplete="off"
                            />
                            {field.secret && (
                              <button
                                type="button"
                                onClick={() =>
                                  setShown((prev) => ({ ...prev, [field.key]: !prev[field.key] }))
                                }
                                className="shrink-0 rounded-md px-1.5 py-1 text-[11.5px] text-ink-mute transition-colors hover:text-ink"
                              >
                                {shown[field.key] ? '隐藏' : '显示'}
                              </button>
                            )}
                            {marked ? (
                              <button
                                type="button"
                                onClick={() =>
                                  setDeletes((prev) => ({ ...prev, [field.key]: false }))
                                }
                                className="shrink-0 rounded-md px-1.5 py-1 text-[11.5px] text-warn transition-colors hover:text-ink"
                              >
                                撤销
                              </button>
                            ) : (
                              saved && (
                                <button
                                  type="button"
                                  onClick={() =>
                                    setDeletes((prev) => ({ ...prev, [field.key]: true }))
                                  }
                                  className="shrink-0 rounded-md px-1.5 py-1 text-[11.5px] text-ink-mute transition-colors hover:text-err"
                                  title="从这个 .env 文件里删除这一行"
                                >
                                  清除
                                </button>
                              )
                            )}
                          </div>
                          <p className="mt-1 text-[11px] leading-relaxed text-ink-mute">
                            {field.hint}
                          </p>
                        </div>
                      )
                    })}
                  </div>
                </section>
              ))}
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-[var(--ring)] pt-3.5">
              <Button
                variant="primary"
                onClick={() => void save()}
                disabled={saving || !user || dirty === 0}
              >
                {saving ? '写入中…' : '保存到 .env'}
              </Button>
              {dirty > 0 && (
                <Button onClick={discard} disabled={saving}>
                  放弃改动
                </Button>
              )}
              <p className="min-w-0 flex-1 text-[11.5px] leading-relaxed text-ink-mute">
                {dirty
                  ? `${dirtyLabel}，保存后重启任务生效`
                  : '留空表示保留原值；已保存的项可点「清除」整行删除'}
              </p>
            </div>
          </Panel>

          <Panel
            id="settings-generators"
            className="scroll-mt-[176px] px-4 py-4 sm:px-5 md:scroll-mt-[124px]"
          >
            <SectionTitle
              title="生成器"
              hint="未接入的后端如实标成不可用；测试只探测连通性，不生成收费样片"
              right={
                <div className="flex shrink-0 items-center gap-2">
                  <Badge tone="idle">单次预算 ¥{providers?.budget_cny ?? '—'}</Badge>
                  <Button small onClick={() => loadProviderInfo(true)} disabled={probing}>
                    {probing ? '探测中…' : '重新探测'}
                  </Button>
                </div>
              }
            />
            {providers?.budget_note && (
              <p className="mt-2 text-[11px] leading-relaxed text-ink-mute">{providers.budget_note}</p>
            )}

            <div className="mt-3 space-y-3.5">
              {grouped.map(({ kind, list }) => (
                <section key={kind}>
                  <div className="flex items-baseline gap-2 border-b border-[var(--ring)] pb-1.5">
                    <h3 className="text-[12.5px] font-semibold tracking-[-0.01em]">
                      {KIND_LABEL[kind] ?? kind}
                    </h3>
                    <span className="font-mono text-[10.5px] text-ink-mute">
                      {list.filter((item) => item.available).length}/{list.length} 可用
                    </span>
                  </div>
                  <div className="mt-1">
                    {list.map((item) => (
                      <ProviderCard key={item.id} item={item} />
                    ))}
                  </div>
                </section>
              ))}
              {providers && items.length === 0 && (
                <p className="py-3 text-[11.5px] text-ink-mute">没有可报告的生成器</p>
              )}
              {!providers && (
                <div className="mt-1">
                  {[0, 1, 2, 3, 4].map((index) => (
                    <div
                      key={index}
                      className="h-[30px] border-b border-[var(--ring)] bg-[var(--ctl)]/30 last:border-b-0"
                    />
                  ))}
                </div>
              )}
            </div>
          </Panel>

          <Panel
            id="settings-usage"
            className="scroll-mt-[176px] px-4 py-4 sm:px-5 md:scroll-mt-[124px]"
          >
            <SectionTitle
              title="费用台账"
              hint="本地绘制、ComfyUI 与离线占位帧不计费；云端适配器接入后成功调用才记账"
              right={
                <div className="shrink-0 text-right">
                  <p className="font-mono text-[16px] leading-tight">¥{usage?.total_cny ?? 0}</p>
                  <p className="text-[10.5px] text-ink-mute">
                    上限 ¥{usage?.budget_cny ?? providers?.budget_cny ?? '—'}
                    {usage?.estimated ? ' · 估价' : ''}
                  </p>
                </div>
              }
            />
            <p className="mt-2 text-[11px] leading-relaxed text-ink-mute">
              {usage?.note ?? '正在读取费用台账…'}
            </p>

            {usage && usage.items.length > 0 ? (
              <div className="mt-3 overflow-hidden rounded-xl border border-[var(--ring)]">
                <div className="hidden grid-cols-[minmax(0,1fr)_150px_80px_90px] gap-2 border-b border-[var(--ring)] bg-[var(--sunk)] px-3 py-1.5 text-[10.5px] tracking-[0.04em] text-ink-mute sm:grid">
                  <span>项目</span>
                  <span>时间 · 调用次数</span>
                  <span className="text-right">金额</span>
                  <span className="text-right">预算</span>
                </div>
                {usage.items.map((item) => (
                  <div
                    key={item.run_id}
                    className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1 border-b border-[var(--ring)] px-3 py-2 text-[11.5px] last:border-b-0 sm:grid-cols-[minmax(0,1fr)_150px_80px_90px]"
                  >
                    <span className="min-w-0 truncate" title={item.theme}>
                      {item.theme}
                    </span>
                    <span className="col-span-2 font-mono text-[10.5px] text-ink-mute sm:col-span-1">
                      {item.created} · {item.events} 次
                    </span>
                    <span className="text-right font-mono">¥{item.total_cny}</span>
                    <span className="text-right">
                      {item.over_budget ? (
                        <Badge tone="warn">超预算</Badge>
                      ) : (
                        <span className="text-[10.5px] text-ink-mute">正常</span>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              usage && (
                <p className="mt-3 rounded-xl border border-[var(--ring)] bg-[var(--sunk)] px-3 py-3 text-[11.5px] leading-relaxed text-ink-mute">
                  还没有付费调用记录。这台机器目前全程走本地方案，不产生 API 费用。
                </p>
              )
            )}
          </Panel>
        </div>

        {/* 体检是只读状态：宽屏放右栏吸顶，窄屏排在表单之后，不占折叠线。
            吸顶栏比视口高时底部会被钉住看不到，所以给它自己的滚动区。 */}
        <aside
          id="settings-env"
          className="scroll-thin scroll-mt-[176px] xl:sticky xl:top-[124px] xl:max-h-[calc(100vh-148px)] xl:overflow-y-auto xl:scroll-mt-[124px]"
        >
          <Panel className="px-4 py-3.5">
            <SectionTitle
              title="环境体检"
              hint="「降级」不是故障：没配 ComfyUI 或 Key 时会自动换本地方案"
              right={
                <Button small onClick={() => check(true)} disabled={checking}>
                  {checking ? '检测中…' : '重新检测'}
                </Button>
              }
            />
            <div className="mt-2">
              {checks.map((item) => (
                <EnvRow key={item.name} item={item} />
              ))}
              {checks.length === 0 &&
                [0, 1, 2, 3].map((index) => (
                  <div key={index} className="border-b border-[var(--ring)] py-2.5 last:border-b-0">
                    <div className="flex items-center gap-2.5">
                      <span className="h-[7px] w-[7px] shrink-0 rounded-full bg-[var(--ctl-edge)]" />
                      <div className="h-[12px] w-[86px] shrink-0 rounded bg-[var(--ctl)]" />
                      <div className="ml-auto h-[18px] w-[46px] shrink-0 rounded-full bg-[var(--ctl)]" />
                    </div>
                    <div className="mt-1.5 ml-[17px] h-[10px] w-3/4 rounded bg-[var(--ctl)] opacity-70" />
                  </div>
                ))}
            </div>

            {env && env.degraded.length > 0 && (
              <div className="mt-3 rounded-xl border border-warn/25 bg-warn/[0.07] px-3 py-2.5">
                <p className="text-[12px] font-medium text-warn">降级说明</p>
                <ul className="mt-1 space-y-1">
                  {env.degraded.map((line) => (
                    <li key={line} className="text-[11px] leading-relaxed text-ink-dim">
                      {line}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="mt-3 flex items-start gap-2 border-t border-[var(--ring)] pt-3">
              <span className="shrink-0 text-[11.5px] text-ink-mute">写入位置</span>
              <span className="shrink-0 font-mono text-[11.5px] text-ink-dim">.env</span>
              <span className="ml-auto text-right text-[10.5px] leading-relaxed text-ink-mute">
                只改这台机器上的文件
                <br />
                不进版本库 · 改完重启任务生效
              </span>
            </div>
          </Panel>
        </aside>
      </div>
    </div>
  )
}