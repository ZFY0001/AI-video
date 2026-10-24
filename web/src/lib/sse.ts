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

function parseChunk(chunk: string, onEvent: (ev: SseEvent) => void): void {
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

  if (dataLines.length === 0) return
  const raw = dataLines.join('\n')
  let data: unknown = raw
  try {
    data = JSON.parse(raw)
  } catch {
    /* 非 JSON 的 data 原样给出（ping 之类的注释不一定走这里） */
  }
  onEvent({ id, event, data })
}

export async function readSse(
  url: string,
  onEvent: (ev: SseEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    signal,
    headers: { Accept: 'text/event-stream' },
    cache: 'no-store',
  })
  if (!res.ok) throw new Error(`事件流连接失败（HTTP ${res.status}）`)
  if (!res.body) throw new Error('当前浏览器不支持流式响应')

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    let cut = buffer.indexOf('\n\n')
    while (cut >= 0) {
      parseChunk(buffer.slice(0, cut).replace(/\r/g, ''), onEvent)
      buffer = buffer.slice(cut + 2)
      cut = buffer.indexOf('\n\n')
    }
  }
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
