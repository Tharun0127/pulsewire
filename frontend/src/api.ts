// REST calls (auth, rooms, history). Real-time traffic is in socket.ts.

export type Session = { token: string; userId: number; username: string };

export type Room = {
  id: number;
  name: string;
  member_count: number;
  is_member: boolean;
  last_seq: number;
  online_count: number;
};

export type ChatMessage = {
  room_id: number;
  seq: number;
  id: number;
  user_id: number;
  username: string;
  body: string;
  client_msg_id: string;
  created_at: string;
  served_by: string;
  delivered_via?: string;
};

export type Member = { user_id: number; username: string; online: boolean; delivered_seq: number; read_seq: number };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, token: string | null, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`/api${path}`, { ...init, headers });
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Request failed";
    throw new ApiError(response.status, detail);
  }
  return data as T;
}

export const api = {
  login: (username: string, password: string, signup: boolean) =>
    request<{ access_token: string; user_id: number; username: string }>(
      signup ? "/auth/signup" : "/auth/login",
      null,
      { method: "POST", body: JSON.stringify({ username, password }) },
    ),
  rooms: (token: string) => request<Room[]>("/rooms", token),
  createRoom: (token: string, name: string) =>
    request<Room>("/rooms", token, { method: "POST", body: JSON.stringify({ name }) }),
  joinRoom: (token: string, roomId: number) => request<void>(`/rooms/${roomId}/join`, token, { method: "POST" }),
  members: (token: string, roomId: number) => request<Member[]>(`/rooms/${roomId}/members`, token),
  // Cursor pagination: before_seq scrolls up, after_seq catches up after a reconnect.
  history: (token: string, roomId: number, cursor: { before_seq?: number; after_seq?: number }, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor.before_seq !== undefined) params.set("before_seq", String(cursor.before_seq));
    if (cursor.after_seq !== undefined) params.set("after_seq", String(cursor.after_seq));
    return request<{ messages: ChatMessage[]; next_before_seq: number | null }>(
      `/rooms/${roomId}/messages?${params}`,
      token,
    );
  },
};

const SESSION_KEY = "pulsewire.session";
const SERVER_KEY = "pulsewire.server";

export function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(session: Session | null) {
  try {
    if (session) localStorage.setItem(SESSION_KEY, JSON.stringify(session));
    else localStorage.removeItem(SESSION_KEY);
  } catch {
    // storage blocked: the session just lasts until reload
  }
}

// Demo helper: "auto" goes through the load balancer, "api-1"/"api-2" pin the
// socket to one instance so cross-server delivery is easy to show.
export type ServerChoice = "auto" | "api-1" | "api-2";

export function loadServerChoice(): ServerChoice {
  try {
    const value = localStorage.getItem(SERVER_KEY);
    return value === "api-1" || value === "api-2" ? value : "auto";
  } catch {
    return "auto";
  }
}

export function saveServerChoice(choice: ServerChoice) {
  try {
    localStorage.setItem(SERVER_KEY, choice);
  } catch {
    // ignore
  }
}
