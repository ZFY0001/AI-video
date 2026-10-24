import type { ButtonHTMLAttributes, CSSProperties, HTMLAttributes, PropsWithChildren, ReactNode } from 'react'

/* ---------------------------------------------------------------- 面板 */

type PanelProps = PropsWithChildren<HTMLAttributes<HTMLElement> & { soft?: boolean }>

export function Panel({ children, className = '', soft, ...rest }: PanelProps) {
  return (
    <section className={`${soft ? 'glass-soft' : 'glass'} ${className}`} {...rest}>
      <span className="sheen" />
      {children}
    </section>
  )
}

export function SectionTitle({
  title,
  hint,
  right,
}: {
  title: string
  hint?: string
  right?: ReactNode
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h2 className="text-[15px] leading-tight font-semibold tracking-[-0.01em]">{title}</h2>
        {hint && <p className="mt-1 text-[12px] leading-relaxed text-ink-mute">{hint}</p>}
      </div>
      {right}
    </div>
  )
}

/* ---------------------------------------------------------------- 按钮 */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'ghost' | 'danger'
  small?: boolean
}

export function Button({ variant = 'ghost', small, className = '', children, ...rest }: ButtonProps) {
  const tone = variant === 'primary' ? 'btn-primary' : variant === 'danger' ? 'btn-danger' : ''
  return (
    <button type="button" className={`btn ${tone} ${small ? 'btn-sm' : ''} ${className}`} {...rest}>
      {variant === 'primary' ? <span className="btn-label">{children}</span> : children}
    </button>
  )
}

/* ---------------------------------------------------------------- 分段控件 */

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  disabled,
}: {
  options: { value: T; label: string; hint?: string }[]
  value: T
  onChange: (next: T) => void
  disabled?: boolean
}) {
  return (
    <div className="seg">
      {options.map((item) => (
        <button
          key={item.value}
          type="button"
          data-active={item.value === value}
          disabled={disabled}
          title={item.hint}
          onClick={() => onChange(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------- 开关 */

export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  label: string
  hint?: string
  disabled?: boolean
}) {
  return (
    <label
      className={[
        'flex items-center gap-2.5',
        disabled ? 'cursor-not-allowed opacity-45' : 'cursor-pointer',
      ].join(' ')}
    >
      <span className="sw">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          aria-label={label}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="sw-body">
          <span className="sw-knob" />
          <span className="sw-ring" />
        </span>
      </span>
      <span className="text-[13px] text-ink-dim">{label}</span>
      {hint && <span className="text-[11px] text-ink-mute">{hint}</span>}
    </label>
  )
}

/* ---------------------------------------------------------------- 字段壳 */

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <div className="min-w-0">
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <span className="text-[12px] font-medium text-ink-dim">{label}</span>
        {hint && <span className="truncate text-[11px] text-ink-mute">{hint}</span>}
      </div>
      {children}
    </div>
  )
}

/* ---------------------------------------------------------------- 徽章 */

export function Badge({
  tone,
  children,
}: {
  tone: 'ok' | 'warn' | 'bad' | 'idle' | 'live'
  children: ReactNode
}) {
  return (
    <span className="badge" data-tone={tone}>
      <i className="badge-dot" />
      {children}
    </span>
  )
}

/* ---------------------------------------------------------------- 加载块 */

/** 3×3 方块阵：整块 72px 原件缩到 18px，九条曲线互不同步。 */
export function BanterLoader() {
  return (
    <span className="eta-loader" aria-hidden="true">
      <span className="banter">
        {Array.from({ length: 9 }, (_, index) => (
          <i key={index} />
        ))}
      </span>
    </span>
  )
}

export function EtaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="eta">
      <BanterLoader />
      <span className="eta-label">{label}</span>
      <span className="eta-value">{value}</span>
    </div>
  )
}

/* ---------------------------------------------------------------- 管线光束 */

const BEAM_NODES = ['简报', '剧本', '分镜', '出图配音', '终检', '合成']

function beamIndex(stage: string): number {
  if (!stage || stage.includes('已停止')) return -1
  if (stage.includes('简报')) return 0
  if (stage.includes('剧本')) return 1
  if (stage.includes('分镜')) return 2
  if (stage.includes('生成镜头')) return 3
  if (stage.includes('终检')) return 4
  if (stage.includes('合成')) return 5
  if (stage.includes('完成')) return BEAM_NODES.length
  return -1
}

export function PipelineBeam({ stage }: { stage: string }) {
  const index = beamIndex(stage)
  const ratio = index < 0 ? 0 : Math.min(1, (index + 1) / BEAM_NODES.length)
  const shot = /生成镜头\s*(\d+)\s*\/\s*(\d+)/.exec(stage)
  const style = { '--beam-progress': `${Math.round(ratio * 100)}%` } as CSSProperties

  return (
    <div className="beam" style={style}>
      {BEAM_NODES.map((name, i) => {
        const state = index > i ? 'done' : index === i ? 'now' : 'idle'
        const label = i === 3 && shot ? `${name} ${shot[1]}/${shot[2]}` : name
        return (
          <div key={name} className="beam-item">
            <span className="beam-dot" data-state={state} />
            <span className="beam-label" data-state={state}>
              {label}
            </span>
          </div>
        )
      })}
    </div>
  )
}

/** 不带文字标签的开关，供设置行右侧使用。 */
export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  label: string
  disabled?: boolean
}) {
  return (
    <label className="sw" style={disabled ? { opacity: 0.45 } : undefined}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-label={label}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="sw-body">
        <span className="sw-knob" />
        <span className="sw-ring" />
      </span>
    </label>
  )
}

/** 设置行：标题在左、控件在右、行间 1px 分隔。创作台与设置页共用。 */
export function SettingRow({
  label,
  sub,
  lead,
  children,
  align,
}: {
  label: string
  sub?: string
  lead?: ReactNode
  children?: ReactNode
  align?: 'center' | 'start'
}) {
  return (
    <div
      className={[
        'flex gap-3.5 border-b border-[var(--ring)] py-2.5 last:border-b-0',
        align === 'start' ? 'items-start' : 'items-center',
      ].join(' ')}
    >
      {lead && <div className="shrink-0">{lead}</div>}
      <div className="min-w-0 flex-1">
        <p className="text-[13.5px] leading-snug">{label}</p>
        {sub && <p className="mt-0.5 text-[11.5px] leading-relaxed text-ink-mute">{sub}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}
