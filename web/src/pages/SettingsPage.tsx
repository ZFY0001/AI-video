import { useEffect, useRef, useState } from 'react'

import {
  DEVICE_REPORT_MD_URL,
  fetchDevice,
  fetchEnv,
  saveDeviceReport,
  saveEnv,
  type DeviceInfo,
  type EnvPayload,
} from '../lib/api'
import { Badge, Button, Panel, SectionTitle, SettingRow } from '../components/ui'

type Tone = 'ok' | 'warn' | 'bad' | 'idle'

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
    key: 'COMFYUI_BASE_URL',
    label: 'ComfyUI 地址',
    hint: '本地出图服务，没启动会降级',
    placeholder: 'http://127.0.0.1:8188',
  },
  {
    key: 'MINIMAX_API_KEY',
    label: '视频模型 Key（MiniMax）',
    hint: '出图 + 出视频都走它；不配就只能出占位帧',
    secret: true,
    placeholder: 'sk-cp-…',
  },
  {
    key: 'ZHIPU_API_KEY',
    label: '视频模型 Key（智谱）',
    hint: '备选。按次计费，1080P，比 MiniMax 便宜',
    secret: true,
    placeholder: 'xxxxxxxx.xxxxxxxx',
  },
  {
    key: 'IMAGE_PROVIDER',
    label: '出图用哪家',
    hint: 'minimax / zhipu；留空用 minimax',
    placeholder: 'minimax',
  },
  {
    key: 'VIDEO_PROVIDER',
    label: '出视频用哪家',
    hint: 'minimax / zhipu；留空用 minimax',
    placeholder: 'minimax',
  },
  {
    key: 'IMAGE_MODE',
    label: '出图优先级',
    hint: 'cloud = 云端优先（画质好）；local = 本地优先（免费但糙）',
    placeholder: 'cloud',
  },
]

export function SettingsPage({ onEnvChange }: { onEnvChange?: (env: EnvPayload) => void } = {}) {
  const [env, setEnv] = useState<EnvPayload | null>(null)
  const [checking, setChecking] = useState(false)
  // 设备检测：为什么做成自动跑——用户不知道"我这台机器能不能跑本地模型"，
  // 让他自己去查显存对参数表，就是又一次上手成本。
  const [device, setDevice] = useState<DeviceInfo | null>(null)
  const [devBusy, setDevBusy] = useState(false)
  // 报告保存状态：给用户一个"确实存下来了"的反馈，否则点完没动静会以为没生效
  const [reportSavedAt, setReportSavedAt] = useState('')
  const [reportBusy, setReportBusy] = useState(false)
  const [form, setForm] = useState<Record<string, string>>({})
  const [shown, setShown] = useState<Record<string, boolean>>({})
  const [message, setMessage] = useState('')
  const [saving, setSaving] = useState(false)
  const [checkedAt, setCheckedAt] = useState('')
  const timer = useRef<number | undefined>(undefined)

  async function detectDevice() {
    setDevBusy(true)
    try {
      setDevice(await fetchDevice())
    } catch {
      /* 检测失败不打扰用户：环境体检那边会报更具体的错 */
    } finally {
      setDevBusy(false)
    }
  }

  async function saveReportNow() {
    setReportBusy(true)
    try {
      const res = await saveDeviceReport()
      setDevice(res.report)
      setReportSavedAt(res.report.checked_at || new Date().toLocaleString())
    } catch {
      setReportSavedAt('保存失败')
    } finally {
      setReportBusy(false)
    }
  }

  // 进设置页就自动检测一次：用户不需要知道"要先点一下"
  useEffect(() => {
    void detectDevice()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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

  const save = async () => {
    const pairs = Object.fromEntries(Object.entries(form).filter(([, value]) => value.trim() !== ''))
    if (Object.keys(pairs).length === 0) {
      setMessage('没有要写入的内容')
      return
    }
    setSaving(true)
    try {
      setMessage(await saveEnv(pairs))
      setForm({})
      check(true)
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
          title="设备检测"
          hint="硬件够就开放本地模型，不够就引导线上 API——你不需要自己判断显存够不够"
          right={
            <Button small onClick={() => detectDevice()} disabled={devBusy}>
              {devBusy ? '检测中…' : '重新检测'}
            </Button>
          }
        />
        {device ? (
          <div className="mt-2">
            <SettingRow label="显卡" sub={device.gpu.name || '未检测到独显'}>
              <span className="font-mono text-[12px] tabular-nums">
                {device.gpu.vram_gb ? `${device.gpu.vram_gb} GB` : '显存未知'}
              </span>
            </SettingRow>
            <SettingRow label="CPU / 内存" sub={`${device.cpu.cores} 线程`}>
              <span className="font-mono text-[12px] tabular-nums">
                {device.ram_gb ? `${device.ram_gb} GB` : '未知'}
              </span>
            </SettingRow>
            <SettingRow
              label="本地模型"
              sub={device.comfyui.usable ? device.comfyui.pick : '未安装 ComfyUI'}
            >
              <Badge tone={device.comfyui.usable ? 'ok' : 'idle'}>
                {device.comfyui.usable ? '可用' : '未就绪'}
              </Badge>
            </SettingRow>
            <div className="mt-3 rounded-xl bg-[var(--sunk)] px-3.5 py-2.5">
              <div className="text-[12.5px] font-medium text-ink">{device.verdict.reason}</div>
              {device.advice.map((line) => (
                <div key={line} className="mt-1 text-[12px] leading-relaxed text-ink-dim">
                  · {line}
                </div>
              ))}
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button small onClick={() => saveReportNow()} disabled={reportBusy}>
                {reportBusy ? '保存中…' : '保存体检报告'}
              </Button>
              <a
                href={DEVICE_REPORT_MD_URL}
                target="_blank"
                rel="noreferrer"
                className="btn btn-ghost btn-sm"
              >
                下载 .md
              </a>
              {reportSavedAt && (
                <span className="text-[11.5px] text-ink-mute">已保存 · {reportSavedAt}</span>
              )}
            </div>
          </div>
        ) : (
          <p className="mt-3 text-[12.5px] text-ink-mute">正在检测设备…</p>
        )}
      </Panel>

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
                  placeholder={field.placeholder}
                  value={form[field.key] ?? ''}
                  onChange={(event) => setForm((prev) => ({ ...prev, [field.key]: event.target.value }))}
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
              </div>
            </SettingRow>
          ))}

          <SettingRow label="写入位置" sub="只改这一台机器上的文件，不进版本库">
            <span className="font-mono text-[11.5px] text-ink-mute">.env</span>
          </SettingRow>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <Button variant="primary" onClick={() => void save()} disabled={saving}>
            {saving ? '写入中…' : '保存到 .env'}
          </Button>
          <p className="text-[11.5px] leading-relaxed text-ink-mute">
            {message || '留空表示这一项不改；改完要重启任务才生效'}
          </p>
        </div>
      </Panel>
    </div>
  )
}
