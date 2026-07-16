import { authHeaderForToken } from './apiClient';
import { emitOrgChanged } from './orgEvents';

/**
 * Client for the org change stream (`GET /api/events/stream`).
 *
 * When any user in the org mutates something, the backend pushes a ping; we
 * announce it locally via emitOrgChanged() and every mounted view refreshes what
 * it shows (see orgEvents.ts). That is what makes a collaborator's change appear
 * in about a second instead of on the next focus / 30s tick — those remain the
 * fallback whenever this stream is unavailable, so losing it degrades latency,
 * never correctness.
 *
 * Why fetch + a stream reader rather than EventSource: EventSource cannot send
 * an Authorization header, and this API is Bearer-authenticated. Passing the JWT
 * as a query param instead would leak it into URLs, proxy logs and history — so
 * we read the stream with fetch, which carries the header normally.
 *
 * Reconnects with exponential backoff (a dropped stream is expected: server
 * restarts, sleeping laptops, idle proxies). The backoff resets once connected.
 */
const BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000';

const INITIAL_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;

/** Open the stream. Returns a cleanup function that closes it for good. */
export function connectOrgEventStream(token: string | null): () => void {
  // No session, or an environment without streaming fetch (e.g. some test
  // runners): stay silent — the focus/interval fallbacks still keep data fresh.
  if (!token || typeof fetch !== 'function' || typeof ReadableStream === 'undefined') {
    return () => {};
  }

  let cancelled = false;
  let controller: AbortController | null = null;
  let retryMs = INITIAL_RETRY_MS;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const scheduleReconnect = () => {
    if (cancelled) return;
    timer = setTimeout(() => void run(), retryMs);
    retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
  };

  const run = async () => {
    if (cancelled) return;
    controller = new AbortController();
    try {
      const response = await fetch(`${BASE_URL}/api/events/stream`, {
        credentials: 'omit',
        headers: { ...authHeaderForToken(token), Accept: 'text/event-stream' },
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error(`stream ${response.status}`);

      retryMs = INITIAL_RETRY_MS; // connected — reset the backoff
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      for (;;) {
        const { done, value } = await reader.read();
        if (done || cancelled) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line. Keepalives are comment
        // frames (": ..."), which simply don't match and are ignored.
        let split = buffer.indexOf('\n\n');
        while (split !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          if (frame.includes('event: org.changed')) emitOrgChanged();
          split = buffer.indexOf('\n\n');
        }
      }
    } catch {
      // Blip, server restart, or abort — fall through to a backoff reconnect.
    }
    if (!cancelled) scheduleReconnect();
  };

  void run();

  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
    controller?.abort();
  };
}
