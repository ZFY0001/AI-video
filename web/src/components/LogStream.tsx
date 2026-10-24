import { useEffect, useRef } from 'react'

import type { LogItem } from '../hooks/useRun'

const TONE: Record<string, string> = {
  error: 'text-err',
  warn: 'text-warn',
  stage: 'text-stage',
  env: 'text-mint',
  info: 'text-ink-dim',
}

function clock(seconds: number): string {
  const date = new Date(seconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function LogStream({ logs }: { logs: LogItem[] }) {
  const boxRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)

  // 只在用户本来就贴着底部时才自动跟随。往上翻查日志时别把他拽回来。
  useEffect(() => {
    const box = boxRef.current
    if (!box || !stickRef.current) return
    box.scrollTop = box.scrollHeight
  }, [logs])

  const onScroll = () => {
    const box = boxRef.current
    if (!box) return
    stickRef.current = box.scrollHeight - box.scrollTop - box.clientHeight < 48
  }

  return (
    <div
      ref={boxRef}
      onScroll={onScroll}
      className="glass-sunk scroll-thin h-full min-h-0 overflow-y-auto px-3 py-2.5 font-mono text-[11.5px] leading-[1.75]"
    >
      {logs.length === 0 ? (
        <div className="flex h-full items-center justify-center">
          <p className="text-[11.5px] text-ink-mute">开跑之后，每一步都会落在这里</p>
        </div>
      ) : (
        logs.map((item) => (
          <div key={item.key} className="log-line flex gap-2.5">
            <span className="shrink-0 text-ink-mute tabular-nums">{clock(item.at)}</span>
            <span className={`min-w-0 break-words ${TONE[item.level] ?? 'text-ink-dim'}`}>
              {item.msg}
            </span>
          </div>
        ))
      )}
    </div>
  )
}
