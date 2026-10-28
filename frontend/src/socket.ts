// One WebSocket per tab, with the reliability logic a real chat client needs:
// - reconnect with exponential backoff + jitter
// - heartbeat ping every 15 s (keeps presence alive on the server)
// - resend messages that were never acked, with the SAME client_msg_id
//   (the server de-duplicates them)
// - tell the app when we (re)connected, so it can fetch missed messages

import type { ServerChoice } from "./api";

export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "offline";

export type ServerEvent = { type: string; [key: string]: unknown };

type Listener = (event: ServerEvent) => void;

const PING_EVERY_MS = 15_000;
const BASE_DELAY_MS = 500;
const MAX_DELAY_MS = 30_000;
const CLOSE_UNAUTHORIZED = 4401;

// Full jitter: a random delay between 0 and the exponential cap. When a
// server restarts, thousands of clients drop at the same instant; jitter
// spreads their reconnects out instead of a synchronized stampede.
export function backoffDelay(attempt: number): number {
  const cap = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** attempt);
  return Math.random() * cap;
}

function socketUrl(server: ServerChoice, token: string): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const path = server === "auto" ? "/ws" : `/direct/${server}/ws`;
  return `${scheme}://${window.location.host}${path}?token=${encodeURIComponent(token)}`;
}

export class ChatSocket {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private pingTimer: number | undefined;
  private retryTimer: number | undefined;
  private stopped = false;
  private listeners = new Set<Listener>();
  private statusListeners = new Set<(s: ConnectionStatus) => void>();
  // client_msg_id -> the send event, kept until the server acks it.
  private unacked = new Map<string, ServerEvent>();
  status: ConnectionStatus = "connecting";

  constructor(private token: string, private server: ServerChoice, private onUnauthorized: () => void) {
    this.open();
    window.addEventListener("online", this.retryNow);
  }

  onEvent(listener: Listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onStatus(listener: (s: ConnectionStatus) => void) {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  send(event: ServerEvent) {
    if (event.type === "send") this.unacked.set(event.client_msg_id as string, event);
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(event));
    // If we are offline the message stays in `unacked` and goes out on reconnect.
  }

  close() {
    this.stopped = true;
    window.removeEventListener("online", this.retryNow);
    clearInterval(this.pingTimer);
    clearTimeout(this.retryTimer);
    this.ws?.close(1000, "logout");
  }

  private setStatus(status: ConnectionStatus) {
    this.status = status;
    this.statusListeners.forEach((l) => l(status));
  }

  private open = () => {
    this.ws = new WebSocket(socketUrl(this.server, this.token));
    this.ws.onopen = () => {
      this.attempt = 0;
      this.setStatus("connected");
      this.pingTimer = window.setInterval(() => this.ws?.send(JSON.stringify({ type: "ping" })), PING_EVERY_MS);
      // Anything sent while disconnected (or whose ack was lost) goes again.
      this.unacked.forEach((event) => this.ws?.send(JSON.stringify(event)));
    };
    this.ws.onmessage = (raw) => {
      const event = JSON.parse(raw.data) as ServerEvent;
      if (event.type === "ack") this.unacked.delete(event.client_msg_id as string);
      this.listeners.forEach((l) => l(event));
    };
    this.ws.onclose = (close) => {
      clearInterval(this.pingTimer);
      if (this.stopped) return;
      if (close.code === CLOSE_UNAUTHORIZED) {
        // Retrying with a bad token can never work; send the user to login.
        this.setStatus("offline");
        this.onUnauthorized();
        return;
      }
      this.scheduleReconnect();
    };
  };

  private scheduleReconnect() {
    const delay = backoffDelay(this.attempt++);
    this.setStatus(navigator.onLine ? "reconnecting" : "offline");
    this.retryTimer = window.setTimeout(this.open, delay);
  }

  // The browser says the network is back: do not wait out the backoff.
  private retryNow = () => {
    if (this.ws?.readyState === WebSocket.OPEN || this.stopped) return;
    clearTimeout(this.retryTimer);
    this.attempt = 0;
    this.open();
  };
}
