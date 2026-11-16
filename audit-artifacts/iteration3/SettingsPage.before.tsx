import { useEffect, useRef, useState } from 'react'

import {
  fetchEnv,
  fetchProviders,
  fetchUsage,
  saveEnv,
  type EnvPayload,
  type ProviderCatalog,
  type UsagePayload,
} from '../lib/api'
import { useAuth } from '../lib/auth'
import { Badge, Button, Panel, SectionTitle, SettingRow } from '../components/ui'

type Tone = 'ok' | 'warn' | 'bad' | 'idle'

const KIND_LABEL: Record<string, string> = {
  paint: '绘制',
  image: '出图',
  video: '视频',
  tts: '配音',
  text: '文本',
}

function toneOf(state: string): Tone {
  if (state === 'ok') return 'ok'
  if (state === 'warn') return 'warn'
  if (state === 'bad') return 'bad'
  return 'idle'
}

function stateColor(state: string): string {
  if (state === 'ok') return 'var(--color-ok)'
  if (state === 'warn') return 'var(--color-warn)'
  return 'var(--color-err)'
}

function stateText(state: string): string {
  if (state === 'ok') return '就绪'
  if (state === 'warn') return '降级'
  return '缺失'
}

const FIELDS: {
  key: string
  label: string
  hint: string
  secret?: boolean
  placeholder: string
}[] = [
  {
    key: 'LLM_API_KEY',
    label: '文本模型 Key',
    hint: '不配就走离线创意，配了才轮到真实模型',
    secret: true,
    placeholder: 'sk-…',
  },
  {
    key: 'LLM_BASE_URL',
    label: '服务地址',
    hint: '兼容 OpenAI 协议即可',
    placeholder: 'https://api.deepseek.com/v1',
  },
  { key: 'LLM_MODEL', label: '模型名', hint: '留空用默认', placeholder: 'deepseek-chat' },
  {
    key: 'VLM_MODEL',
    label: '视觉评审模型',
    hint: '配置后质检才真正读图；留空则只做技术检查 + 模拟评审',
    placeholder: 'qwen-vl-plus / gpt-4o-mini',
  },
  {
    key: 'VLM_BASE_URL',
    label: '视觉评审地址',
    hint: 'OpenAI 兼容端点；留空沿用文本模型的地址',
    placeholder: 'https://api.example.com/v1',
  },
  {
    key: 'VLM_API_KEY',
    label: '视觉评审 Key',
    secret: true,
    hint: '留空沿用文本模型的 Key',
    placeholder: 'sk-…',
  },
  {
    key: 'BUDGET_CNY',
    label: '单次预算上限',
    hint: '云端视频按估价超过它就在提交前拒绝；本地出片不受影响',
    placeholder: '20',
  },
  {
    key: 'COMFYUI_BASE_URL',
    label: 'ComfyUI 地址',
    hint: '本地出图服务，没启动会降级',
    placeholder: 'http://127.0.0.1:8188',
  },
]

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
  const [saving, setSaving] = useState(false)
  const [checkedAt, setCheckedAt] = useState('')
  const timer = useRef<number | undefined>(undefined)

  const check = (fresh: boolean) => {
    setChecking(true)
    fetchEnv(fresh)
      .then((data) => {
        setEnv(data)
        onEnvChange?.(data)
        setMessage('')
        if (data.pending) {
          window.clearTimeout(timer.current)
          timer.current = window.setTimeout(() => check(false), 1500)
        } else {
          setCheckedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
        }
      })
      .catch((err: unknown) => setMessage(err instanceof Error ? err.message : '体检失败'))
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
        setMessage(err instanceof Error ? err.message : '读取生成器状态失败')
      })
      .finally(() => setProbing(false))
  }

  useEffect(() => {
    loadProviderInfo(false)
  }, [])

  const pendingDeletes = Object.keys(deletes).filter((key) => deletes[key])

  const save = async () => {
    // 三态：输入框有值 = 替换；标了清除 = 删除；其余 = 保留。
    const values = Object.fromEntries(
      Object.entries(form).filter(([key, value]) => value.trim() !== '' && !deletes[key]),
    )
    if (Object.keys(values).length === 0 && pendingDeletes.length === 0) {
      setMessage('没有要写入或清除的内容')
      return
    }
    setSaving(true)
    try {
      const res = await saveEnv({ values, delete: pendingDeletes })
      setMessage(res.detail)
      setForm({})
      setDeletes({})
      if (res.configured) {
        setEnv((prev) => (prev ? { ...prev, configured: res.configured } : prev))
      }
      check(true)
      loadProviderInfo(true)
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : '写入失败')
    } finally {
      setSaving(false)
    }
  }

  const checks = env?.checks ?? []

  return (
    <div className="mx-auto w-full max-w-[1000px] px-6 pt-7 pb-16">
      <div className="mb-5 flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">设置</h2>
          <p className="mt-1.5 text-[12.5px] text-ink-mute">
            看每一项依赖现在是什么状态，配好 Key 之后真实模型才会接管。
          </p>
        </div>
        <span className="shrink-0 font-mono text-[10.5px] tracking-[0.06em] text-ink-mute">
          {checkedAt ? `检测于 ${checkedAt}` : '正在检测…'}
        </span>
      </div>

      <Panel className="mb-3 px-5 py-4">
        <SectionTitle
          title="环境体检"
          hint="「降级」不是故障：没配 ComfyUI 或 Key 时会自动换成本地方案"
          right={
            <div className="flex items-center gap-2">
              <Badge tone={env?.ready ? 'ok' : 'warn'}>{env?.ready ? '可用' : '有降级项'}</Badge>
              <Button small onClick={() => check(true)} disabled={checking}>
                {checking ? '检测中…' : '重新检测'}
              </Button>
            </div>
          }
        />

        <div className="mt-2">
          {checks.map((item) => (
            <SettingRow
              key={item.name}
              label={item.name}
              sub={item.detail || '—'}
              lead={
                <span
                  className="block h-[7px] w-[7px] rounded-full"
                  style={{
                    background: stateColor(item.state),
                    color: stateColor(item.state),
                    boxShadow: '0 0 8px currentColor',
                  }}
                />
              }
            >
              <Badge tone={toneOf(item.state)}>{stateText(item.state)}</Badge>
            </SettingRow>
          ))}
          {checks.length === 0 &&
            [0, 1, 2, 3].map((index) => (
              <div
                key={index}
                className="flex items-center gap-3.5 border-b border-[var(--ring)] py-2.5 last:border-b-0"
              >
                <span className="h-[7px] w-[7px] shrink-0 rounded-full bg-[var(--ctl-edge)]" />
                <div className="h-[13px] w-[86px] shrink-0 rounded bg-[var(--ctl)]" />
                <div className="h-[11px] flex-1 rounded bg-[var(--ctl)]" style={{ opacity: 0.7 }} />
                <div className="h-[18px] w-[46px] shrink-0 rounded-full bg-[var(--ctl)]" />
              </div>
            ))}
        </div>
      </Panel>

      <Panel className="mb-3 px-5 py-4">
        <SectionTitle
          title="生成器与费用"
          hint="未接入的后端如实标成不可用；测试只探测连通性，不生成收费样片"
          right={
            <div className="flex items-center gap-2">
              <Badge tone="idle">单次预算 ¥{providers?.budget_cny ?? '—'}</Badge>
              <Button small onClick={() => loadProviderInfo(true)} disabled={probing}>
                {probing ? '探测中…' : '重新探测'}
              </Button>
            </div>
          }
        />

        <div className="mt-2">
          {(providers?.items ?? []).map((item) => {
            const tone: Tone = item.available ? 'ok' : item.configured ? 'warn' : 'idle'
            const state = item.available ? '可用' : item.configured ? '未接入' : '未配置'
            const refs =
              item.reference_images == null
                ? '参考图：不适用'
                : item.reference_images
                  ? '参考图：支持'
                  : '参考图：不支持'
            const sub = [
              item.detail,
              item.reference_detail ? `${refs}（${item.reference_detail}）` : refs,
              `${item.cost_label} · ${item.price_hint}`,
            ].join(' ｜ ')
            return (
              <SettingRow
                key={item.id}
                label={`${item.label} · ${KIND_LABEL[item.kind] ?? item.kind}`}
                sub={sub}
                align="start"
                lead={
                  <span
                    className="mt-1 block h-[7px] w-[7px] rounded-full"
                    style={{
                      background: item.available
                        ? 'var(--color-ok)'
                        : item.configured
                          ? 'var(--color-warn)'
                          : 'var(--ctl-edge)',
                    }}
                  />
                }
              >
                <Badge tone={tone}>{state}</Badge>
              </SettingRow>
            )
          })}
          {providers && providers.items.length === 0 && (
            <p className="py-3 text-[11.5px] text-ink-mute">没有可报告的生成器</p>
          )}
        </div>

        <div className="mt-3 border-t border-[var(--ring)] pt-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[12.5px]">费用估算</p>
              <p className="mt-0.5 text-[10.5px] leading-relaxed text-ink-mute">
                {usage?.note ?? '正在读取费用台账…'}
              </p>
            </div>
            <div className="shrink-0 text-right">
              <p className="font-mono text-[16px]">¥{usage?.total_cny ?? 0}</p>
              <p className="text-[10.5px] text-ink-mute">
                单次上限 ¥{usage?.budget_cny ?? providers?.budget_cny ?? '—'}
                {usage?.estimated ? ' · 估价' : ''}
              </p>
            </div>
          </div>
          {usage && usage.items.length > 0 ? (
            <div className="mt-2 space-y-1">
              {usage.items.map((item) => (
                <div
                  key={item.run_id}
                  className="flex flex-wrap items-center gap-2 border-b border-[var(--ring)] py-1.5 text-[11px] last:border-b-0"
                >
                  <span className="min-w-0 flex-1 truncate">{item.theme}</span>
                  <span className="font-mono text-[10.5px] text-ink-mute">
                    {item.created} · {item.events} 次
                  </span>
                  <span className="font-mono">¥{item.total_cny}</span>
                  {item.over_budget && <Badge tone="warn">超过单次预算</Badge>}
                </div>
              ))}
            </div>
          ) : (
            usage && (
              <p className="mt-2 text-[10.5px] leading-relaxed text-ink-mute">
                还没有付费调用记录。本地绘制、ComfyUI 与离线占位帧不计费；
                云端视频/出图适配器接入后，成功调用才会出现在这里。
              </p>
            )
          )}
        </div>
      </Panel>

      <Panel className="px-5 py-4">
        <SectionTitle
          title="模型与密钥"
          hint="写回 .env，只替换命中的行，注释和其他配置原样保留"
          right={<Badge tone={env?.live_ready ? 'ok' : 'idle'}>{env?.live_ready ? '已配 Key' : '未配 Key'}</Badge>}
        />

        <div className="mt-2">
          {FIELDS.map((field) => (
            <SettingRow key={field.key} label={field.label} sub={field.hint}>
              <div className="flex items-center gap-1.5">
                <input
                  type={field.secret && !shown[field.key] ? 'password' : 'text'}
                  className="field-input w-[248px] text-[12.5px]"
                  placeholder={
                    env?.configured?.[field.key] ? '已保存，留空表示保留' : field.placeholder
                  }
                  value={form[field.key] ?? ''}
                  onChange={(event) => {
                    setForm((prev) => ({ ...prev, [field.key]: event.target.value }))
                    // 重新输入等于"替换"，撤销之前的清除标记。
                    setDeletes((prev) => ({ ...prev, [field.key]: false }))
                  }}
                  spellCheck={false}
                />
                {field.secret && (
                  <button
                    type="button"
                    onClick={() => setShown((prev) => ({ ...prev, [field.key]: !prev[field.key] }))}
                    className="rounded-md px-1.5 py-1 text-[11.5px] text-ink-mute transition-colors hover:text-ink"
                  >
                    {shown[field.key] ? '隐藏' : '显示'}
                  </button>
                )}
                {deletes[field.key] ? (
                  <button
                    type="button"
                    onClick={() => setDeletes((prev) => ({ ...prev, [field.key]: false }))}
                    className="rounded-md px-1.5 py-1 text-[11.5px] text-warn transition-colors hover:text-ink"
                  >
                    将清除 · 撤销
                  </button>
                ) : (
                  env?.configured?.[field.key] && (
                    <button
                      type="button"
                      onClick={() => setDeletes((prev) => ({ ...prev, [field.key]: true }))}
                      className="rounded-md px-1.5 py-1 text-[11.5px] text-ink-mute transition-colors hover:text-err"
                      title="从这个 .env 文件里删除这一行"
                    >
                      清除
                    </button>
                  )
                )}
              </div>
            </SettingRow>
          ))}

          <SettingRow label="写入位置" sub="只改这一台机器上的文件，不进版本库">
            <span className="font-mono text-[11.5px] text-ink-mute">.env</span>
          </SettingRow>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <Button variant="primary" onClick={() => void save()} disabled={saving || !user}>
            {saving ? '写入中…' : '保存到 .env'}
          </Button>
          <p className="text-[11.5px] leading-relaxed text-ink-mute">
            {message ||
              (user
                ? '留空表示保留原值；已保存的项可点「清除」。改完重启任务生效'
                : '写入配置需要登录（配置会决定模型与密钥去向）')}
          </p>
        </div>
      </Panel>
    </div>
  )
}
