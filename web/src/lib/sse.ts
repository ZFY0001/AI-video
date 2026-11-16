/**
 * SSE 客户端。
 *
 * 不用原生 `EventSource`：它不能带 cursor 参数、不能设请求头、重连间隔固定 3 秒。
 * 用 fetch + ReadableStream 自己解析，才能做到「断线后从最后一个事件续上」。
 */

export interface SseEvent {
  id: number
  event: string
  data: unknown
}

/** 流结束时的状态。`terminal` 表示服务端已明确给出 done/error。 */
export interface SseOutcome {
  terminal: boolean
  lastEvent: string
}

function parseChunk(chunk: string, onEvent: (ev: SseEvent) => void): SseEvent | null {
  let id = -1
  let event = 'message'
  const dataLines: string[] = []

  for (const line of chunk.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'id') {
      const parsed = Number(value)
      if (Number.isFinite(parsed)) id = parsed
    } else if (field === 'event') {
      event = value
    } else if (field === 'data') {
      dataLines.push(value)
    }
  }

  if (dataLines.length === 0) return null
  const raw = dataLines.join('\n')
  let data: unknown = raw
  try {
    data = JSON.parse(raw)
  } catch {
    /* 非 JSON 的 data 原样给出（ping 之类的注释不一定走这里） */
  }
  const parsedEvent = { id, event, data }
  onEvent(parsedEvent)
  return parsedEvent
}

export async function readSse(
  url: string,
  onEvent: (ev: SseEvent) => void,
  signal: AbortSignal,
  headers: Record<string, string> = {},
): Promise<SseOutcome> {
  const res = await fetch(url, {
    signal,
    // 事件流也要带凭据：任务是私有的，匿名订阅必须被 404 挡住。
    // 凭据由调用方传入，本模块只管协议——这样它也能被独立测试。
    headers: { Accept: 'text/event-stream', ...headers },
    cache: 'no-store',
  })
  if (!res.ok) throw new Error(`事件流连接失败（HTTP ${res.status}）`)
  if (!res.body) throw new Error('当前浏览器不支持流式响应')

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  let terminal = false
  let lastEvent = ''

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += value
      // 先把 CRLF 归一成 LF：服务端发的是 LF，但替代流源/反代可能转成 CRLF。
      // 只替换 \r\n 不会误伤跨块边界——上一块末尾的 \r 会和下一块开头的 \n 拼上。
      buffer = buffer.replace(/\r\n/g, '\n')

      let cut = buffer.indexOf('\n\n')
      while (cut >= 0) {
        const parsed = parseChunk(buffer.slice(0, cut), onEvent)
        buffer = buffer.slice(cut + 2)
        if (parsed) {
          lastEvent = parsed.event
          if (parsed.event === 'done' || parsed.event === 'error') terminal = true
        }
        if (terminal) break
        cut = buffer.indexOf('\n\n')
      }
      // 收到终态就主动释放连接，别让一个已经结束的任务继续占着 reader。
      if (terminal) {
        await reader.cancel().catch(() => undefined)
        break
      }
    }
  } finally {
    reader.releaseLock?.()
  }

  return { terminal, lastEvent }
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
