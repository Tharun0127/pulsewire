import { FormEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ChatMessage, Member, Room, Session } from "../api";
import { lastSeq, type useChat } from "../useChat";

type Chat = ReturnType<typeof useChat>;
type Props = { room: Room; session: Session; chat: Chat };

type Tick = "pending" | "sent" | "delivered" | "read";

// Group rule (like WhatsApp groups): delivered/read once EVERY other member got there.
function tickFor(seq: number, myId: number, members: Member[] | undefined): Tick {
  const others = (members ?? []).filter((m) => m.user_id !== myId);
  if (!others.length) return "sent";
  if (others.every((m) => m.read_seq >= seq)) return "read";
  if (others.every((m) => m.delivered_seq >= seq)) return "delivered";
  return "sent";
}

function Ticks({ tick }: { tick: Tick }) {
  if (tick === "pending") return <span title="sending">🕓</span>;
  const label = { sent: "✓", delivered: "✓✓", read: "✓✓" }[tick];
  return (
    <span title={tick} className={tick === "read" ? "font-bold text-sky-500" : "text-slate-400"}>
      {label}
    </span>
  );
}

export default function ChatWindow({ room, session, chat }: Props) {
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const loadingOlder = useRef(false);
  const stickToBottom = useRef(true);
  const heightBeforeLoad = useRef<number | null>(null);

  const timeline = chat.timelines[room.id];
  const messages: ChatMessage[] = useMemo(
    () => Object.values(timeline ?? {}).sort((a, b) => a.seq - b.seq),
    [timeline],
  );
  const pending = chat.pending.filter((p) => p.room_id === room.id);
  const members = chat.members[room.id];
  const typers = Object.values(chat.typing[room.id] ?? {});
  const onlineIds = chat.online[room.id];

  useEffect(() => {
    void chat.openRoom(room.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room.id]);

  // Mark as read whenever new messages show up while this room is open.
  const newest = lastSeq(timeline);
  useEffect(() => {
    if (document.visibilityState === "visible") chat.markRead(room.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newest, room.id]);

  // Keep the view pinned to the bottom for new messages, and keep the
  // reading position stable when older messages are added on top.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (heightBeforeLoad.current !== null) {
      el.scrollTop += el.scrollHeight - heightBeforeLoad.current;
      heightBeforeLoad.current = null;
    } else if (stickToBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages.length, pending.length]);

  async function onScroll() {
    const el = scroller.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (el.scrollTop < 60 && !loadingOlder.current && chat.olderCursor[room.id]) {
      loadingOlder.current = true;
      heightBeforeLoad.current = el.scrollHeight;
      await chat.loadOlder(room.id);
      loadingOlder.current = false;
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const body = draft.trim();
    if (!body) return;
    chat.sendMessage(room.id, body);
    setDraft("");
    stickToBottom.current = true;
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2">
        <div>
          <h1 className="font-semibold"># {room.name}</h1>
          <p className="text-xs text-slate-500">
            {onlineIds?.length ?? room.online_count} online · {room.member_count} members
          </p>
        </div>
        <div className="flex -space-x-1">
          {(members ?? []).map((m) => {
            const isOnline = onlineIds ? onlineIds.includes(m.user_id) : m.online;
            return (
              <span
                key={m.user_id}
                title={`${m.username} · ${isOnline ? "online" : "offline"}`}
                className={`flex h-7 w-7 items-center justify-center rounded-full border-2 text-xs font-semibold uppercase ${
                  isOnline ? "border-emerald-400 bg-emerald-50" : "border-slate-200 bg-slate-100 text-slate-400"
                }`}
              >
                {m.username[0]}
              </span>
            );
          })}
        </div>
      </div>

      <div ref={scroller} onScroll={onScroll} className="flex-1 space-y-2 overflow-y-auto px-4 py-3">
        {chat.olderCursor[room.id] === null && (
          <p className="py-2 text-center text-xs text-slate-400">Beginning of #{room.name}</p>
        )}
        {messages.map((m) => {
          const mine = m.user_id === session.userId;
          return (
            <div key={m.seq} className={`flex ${mine ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[75%] rounded-2xl px-3 py-2 shadow-sm ${
                  mine ? "bg-violet-600 text-white" : "bg-white"
                }`}
              >
                {!mine && <p className="text-xs font-semibold text-violet-700">{m.username}</p>}
                <p className="whitespace-pre-wrap break-words">{m.body}</p>
                <p className={`mt-1 flex items-center justify-end gap-2 text-[10px] ${mine ? "text-violet-200" : "text-slate-400"}`}>
                  {/* Which server accepted the message, and which pushed it to us. */}
                  <span className="font-mono" title="served by → delivered via">
                    {m.served_by}
                    {m.delivered_via && m.delivered_via !== m.served_by ? ` → ${m.delivered_via}` : ""}
                  </span>
                  <span>#{m.seq}</span>
                  <span>{new Date(m.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
                  {mine && <Ticks tick={tickFor(m.seq, session.userId, members)} />}
                </p>
              </div>
            </div>
          );
        })}
        {pending.map((p) => (
          <div key={p.client_msg_id} className="flex justify-end">
            <div className="max-w-[75%] rounded-2xl bg-violet-400 px-3 py-2 text-white shadow-sm">
              <p className="whitespace-pre-wrap break-words">{p.body}</p>
              <p className="mt-1 text-right text-[10px]">
                <Ticks tick="pending" />
              </p>
            </div>
          </div>
        ))}
      </div>

      <div className="h-5 px-4 text-xs italic text-slate-500">
        {typers.length > 0 && `${typers.map((t) => t.username).join(", ")} ${typers.length > 1 ? "are" : "is"} typing…`}
      </div>
      <form onSubmit={submit} className="flex gap-2 border-t border-slate-200 bg-white p-3">
        <input
          className="flex-1 rounded-full border border-slate-300 px-4 py-2 focus:border-violet-500 focus:outline-none"
          placeholder={`Message #${room.name}`}
          value={draft}
          maxLength={4000}
          onChange={(e) => {
            setDraft(e.target.value);
            chat.notifyTyping(room.id);
          }}
        />
        <button className="rounded-full bg-violet-600 px-5 font-semibold text-white hover:bg-violet-700">Send</button>
      </form>
    </div>
  );
}
