import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ChatMessage, type Member, type Room, type ServerChoice, type Session } from "./api";
import { ChatSocket, type ConnectionStatus, type ServerEvent } from "./socket";

// Messages are stored per room KEYED BY SEQ. A message that arrives both
// live and from a history fetch is stored once (no duplicates), and any
// missing seq is visible as a gap we can fetch (no silent holes).
type Timeline = Record<number, ChatMessage>;
export type Pending = { client_msg_id: string; body: string; room_id: number };
export type TypingUser = { username: string; until: number };

const RECEIPT_BATCH_MS = 500;
const TYPING_THROTTLE_MS = 2000;

function newId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // Fallback for non-secure origins (e.g. a LAN IP); still a v4-shaped UUID.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export function lastSeq(timeline: Timeline | undefined): number {
  if (!timeline) return 0;
  let max = 0;
  for (const key in timeline) max = Math.max(max, Number(key));
  return max;
}

export function useChat(session: Session, server: ServerChoice, onUnauthorized: () => void) {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [instance, setInstance] = useState<string | null>(null);
  const [rooms, setRooms] = useState<Room[]>([]);
  const [timelines, setTimelines] = useState<Record<number, Timeline>>({});
  const [pending, setPending] = useState<Pending[]>([]);
  const [olderCursor, setOlderCursor] = useState<Record<number, number | null>>({});
  const [members, setMembers] = useState<Record<number, Member[]>>({});
  const [online, setOnline] = useState<Record<number, number[]>>({});
  const [typing, setTyping] = useState<Record<number, Record<number, TypingUser>>>({});

  const socket = useRef<ChatSocket | null>(null);
  const timelinesRef = useRef(timelines);
  timelinesRef.current = timelines;
  const receiptQueue = useRef<Record<string, { room_id: number; seq: number; type: string }>>({});
  const lastTypingSent = useRef<Record<number, number>>({});

  const addMessages = useCallback((roomId: number, messages: ChatMessage[]) => {
    if (!messages.length) return;
    setTimelines((all) => {
      const timeline = { ...(all[roomId] ?? {}) };
      for (const m of messages) timeline[m.seq] = { ...timeline[m.seq], ...m };
      return { ...all, [roomId]: timeline };
    });
    const ids = new Set(messages.map((m) => m.client_msg_id));
    setPending((list) => list.filter((p) => !ids.has(p.client_msg_id)));
  }, []);

  // Fetch everything after our last seq (after a reconnect or a detected gap).
  const catchUp = useCallback(
    async (roomId: number) => {
      let after = lastSeq(timelinesRef.current[roomId]);
      for (;;) {
        const page = await api.history(session.token, roomId, { after_seq: after }, 200);
        addMessages(roomId, page.messages);
        if (page.messages.length < 200) break;
        after = page.messages[page.messages.length - 1].seq;
      }
    },
    [session.token, addMessages],
  );

  const refreshRooms = useCallback(async () => {
    setRooms(await api.rooms(session.token));
  }, [session.token]);

  const refreshMembers = useCallback(
    async (roomId: number) => {
      const list = await api.members(session.token, roomId);
      setMembers((all) => ({ ...all, [roomId]: list }));
    },
    [session.token],
  );

  // Receipts are batched: only the highest seq per room per 500 ms is sent.
  const queueReceipt = useCallback((type: "delivered" | "read", roomId: number, seq: number) => {
    const key = `${type}:${roomId}`;
    const current = receiptQueue.current[key];
    if (!current || current.seq < seq) receiptQueue.current[key] = { type, room_id: roomId, seq };
  }, []);

  useEffect(() => {
    const timer = setInterval(() => {
      const batch = Object.values(receiptQueue.current);
      receiptQueue.current = {};
      batch.forEach((r) => socket.current?.send(r));
    }, RECEIPT_BATCH_MS);
    return () => clearInterval(timer);
  }, []);

  const handleEvent = useCallback(
    (event: ServerEvent) => {
      switch (event.type) {
        case "hello": {
          setInstance(event.instance as string);
          // Compare the server's latest seq per room with ours; fetch gaps.
          const seqs = event.room_seqs as Record<string, number>;
          for (const [roomId, seq] of Object.entries(seqs)) {
            if (seq > lastSeq(timelinesRef.current[Number(roomId)])) void catchUp(Number(roomId));
          }
          void refreshRooms();
          break;
        }
        case "message": {
          const message = event as unknown as ChatMessage;
          const known = lastSeq(timelinesRef.current[message.room_id]);
          addMessages(message.room_id, [message]);
          // A jump in seq means we missed something (e.g. a dropped publish).
          if (known > 0 && message.seq > known + 1) void catchUp(message.room_id);
          if (message.user_id !== session.userId) queueReceipt("delivered", message.room_id, message.seq);
          setTyping((all) => {
            const room = { ...(all[message.room_id] ?? {}) };
            delete room[message.user_id];
            return { ...all, [message.room_id]: room };
          });
          break;
        }
        case "presence":
          setOnline((all) => ({ ...all, [event.room_id as number]: event.online_user_ids as number[] }));
          setRooms((list) =>
            list.map((r) => (r.id === event.room_id ? { ...r, online_count: event.online_count as number } : r)),
          );
          break;
        case "typing":
          if (event.user_id === session.userId) break;
          setTyping((all) => ({
            ...all,
            [event.room_id as number]: {
              ...(all[event.room_id as number] ?? {}),
              [event.user_id as number]: {
                username: event.username as string,
                until: Date.now() + (event.expires_in_ms as number),
              },
            },
          }));
          break;
        case "receipt":
          setMembers((all) => {
            const list = all[event.room_id as number];
            if (!list) return all;
            return {
              ...all,
              [event.room_id as number]: list.map((m) =>
                m.user_id === event.user_id
                  ? { ...m, delivered_seq: event.delivered_seq as number, read_seq: event.read_seq as number }
                  : m,
              ),
            };
          });
          break;
      }
    },
    [addMessages, catchUp, queueReceipt, refreshRooms, session.userId],
  );

  useEffect(() => {
    const s = new ChatSocket(session.token, server, onUnauthorized);
    socket.current = s;
    const offEvent = s.onEvent(handleEvent);
    const offStatus = s.onStatus(setStatus);
    return () => {
      offEvent();
      offStatus();
      s.close();
    };
    // One socket per session/server choice; handlers are stable callbacks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.token, server]);

  // Room list (with online counts) every 10 s as a safety net.
  useEffect(() => {
    void refreshRooms();
    const timer = setInterval(() => void refreshRooms(), 10_000);
    return () => clearInterval(timer);
  }, [refreshRooms]);

  // Drop expired typing indicators.
  useEffect(() => {
    const timer = setInterval(() => {
      const now = Date.now();
      setTyping((all) => {
        let changed = false;
        const next: typeof all = {};
        for (const [roomId, users] of Object.entries(all)) {
          const kept = Object.fromEntries(Object.entries(users).filter(([, t]) => t.until > now));
          if (Object.keys(kept).length !== Object.keys(users).length) changed = true;
          next[Number(roomId)] = kept;
        }
        return changed ? next : all;
      });
    }, 500);
    return () => clearInterval(timer);
  }, []);

  const openRoom = useCallback(
    async (roomId: number) => {
      void refreshMembers(roomId);
      if (timelinesRef.current[roomId] && Object.keys(timelinesRef.current[roomId]).length) return;
      const page = await api.history(session.token, roomId, {});
      addMessages(roomId, page.messages);
      setOlderCursor((all) => ({ ...all, [roomId]: page.next_before_seq }));
    },
    [session.token, addMessages, refreshMembers],
  );

  // Infinite scroll upwards: the oldest seq we have is the cursor.
  const loadOlder = useCallback(
    async (roomId: number) => {
      const cursor = olderCursor[roomId];
      if (cursor === null || cursor === undefined) return false;
      const page = await api.history(session.token, roomId, { before_seq: cursor });
      addMessages(roomId, page.messages);
      setOlderCursor((all) => ({ ...all, [roomId]: page.next_before_seq }));
      return page.messages.length > 0;
    },
    [olderCursor, session.token, addMessages],
  );

  const sendMessage = useCallback((roomId: number, body: string) => {
    const message = { type: "send", room_id: roomId, client_msg_id: newId(), body };
    setPending((list) => [...list, { client_msg_id: message.client_msg_id, body, room_id: roomId }]);
    socket.current?.send(message);
  }, []);

  // Client-side throttle: at most one typing event per 2 s.
  const notifyTyping = useCallback((roomId: number) => {
    const now = Date.now();
    if (now - (lastTypingSent.current[roomId] ?? 0) < TYPING_THROTTLE_MS) return;
    lastTypingSent.current[roomId] = now;
    socket.current?.send({ type: "typing", room_id: roomId });
  }, []);

  const markRead = useCallback(
    (roomId: number) => {
      const seq = lastSeq(timelinesRef.current[roomId]);
      if (seq > 0) queueReceipt("read", roomId, seq);
    },
    [queueReceipt],
  );

  const joinRoom = useCallback(
    async (roomId: number) => {
      await api.joinRoom(session.token, roomId);
      socket.current?.send({ type: "subscribe", room_id: roomId });
      await refreshRooms();
    },
    [session.token, refreshRooms],
  );

  const createRoom = useCallback(
    async (name: string) => {
      const room = await api.createRoom(session.token, name);
      socket.current?.send({ type: "subscribe", room_id: room.id });
      await refreshRooms();
      return room;
    },
    [session.token, refreshRooms],
  );

  return {
    status, instance, rooms, timelines, pending, members, online, typing, olderCursor,
    openRoom, loadOlder, sendMessage, notifyTyping, markRead, joinRoom, createRoom,
  };
}
