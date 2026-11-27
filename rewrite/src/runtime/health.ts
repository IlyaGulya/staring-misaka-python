import type { RuntimeStatus, Settings } from '../domain.ts'
import type { Store } from '../store.ts'

export interface HealthServer {
  readonly port: number
  close(): Promise<void>
}

/** Lightweight HTTP liveness/readiness server for Kubernetes probes. Exposes zero secrets or chat text. */
export function startHealthServer(
  settings: Settings,
  store: Store,
  getStatus: () => RuntimeStatus,
): HealthServer | undefined {
  const port = settings.healthPort ?? 0
  if (port <= 0) return undefined

  const maxPollSilenceMs = Math.max(settings.pollTimeout * 4, 180) * 1000
  const server = Bun.serve({
    hostname: settings.healthHost ?? '127.0.0.1',
    port,
    fetch(request) {
      const url = new URL(request.url)
      if (request.method !== 'GET') {
        return new Response('Method Not Allowed', { status: 405 })
      }
      const now = Date.now()
      const status = getStatus()
      const dbOk = store.ping()
      const uptimeSeconds = Math.max(0, Math.floor((now - status.startedAt) / 1000))
      const pollAgeMs = status.lastPollAt > 0 ? now - status.lastPollAt : now - status.startedAt
      const pollHealthy = status.lastPollAt > 0 ? pollAgeMs <= maxPollSilenceMs : uptimeSeconds < 60

      if (url.pathname === '/healthz') {
        const ok = dbOk && pollHealthy
        return Response.json(
          {
            ok,
            mode: settings.mode,
            provider: settings.provider ?? 'anthropic',
            model: settings.model,
            uptimeSeconds,
            dbOk,
            pollAgeSeconds: Math.floor(pollAgeMs / 1000),
            observerConfigured: status.observerConfigured,
            observerConnected: status.observerConnected,
          },
          { status: ok ? 200 : 503 },
        )
      }

      if (url.pathname === '/readyz') {
        const ok = status.ready && dbOk
        return Response.json(
          {
            ok,
            ready: status.ready,
            dbOk,
            botId: status.botId,
            observerConfigured: status.observerConfigured,
            observerConnected: status.observerConnected,
          },
          { status: ok ? 200 : 503 },
        )
      }

      return Response.json({ error: 'not_found' }, { status: 404 })
    },
  })

  return {
    port: server.port ?? port,
    close() {
      return server.stop(true)
    },
  }
}
