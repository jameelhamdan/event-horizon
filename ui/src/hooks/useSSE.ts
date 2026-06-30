import { useEffect, useRef } from "react"
import type { SSEEvent } from "../types"
type SSEHandler = (event: SSEEvent) => void

const SSE_URL = `/api/sse`

export function useSSE(onEvent: SSEHandler) {
  const handlerRef = useRef(onEvent)
  handlerRef.current = onEvent

  useEffect(() => {
    let es: EventSource | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let closed = false

    function connect() {
      if (closed) return
      es = new EventSource(SSE_URL)

      es.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data) as SSEEvent
          handlerRef.current(data)
        } catch {
          // ignore malformed messages
        }
      }

      es.onerror = () => {
        es?.close()
        if (!closed) {
          reconnectTimer = setTimeout(connect, 5000)
        }
      }
    }

    connect()

    return () => {
      closed = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      es?.close()
    }
  }, [])
}
