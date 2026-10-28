import { FormEvent, useEffect, useState } from "react";
import type { ServerChoice, Session } from "../api";
import { useChat } from "../useChat";
import ChatWindow from "./ChatWindow";
import StatusBadge from "./StatusBadge";

type Props = { session: Session; server: ServerChoice; onLogout: () => void };

export default function ChatApp({ session, server, onLogout }: Props) {
  const chat = useChat(session, server, onLogout);
  const [activeRoomId, setActiveRoomId] = useState<number | null>(null);
  const [newRoom, setNewRoom] = useState("");
  const [error, setError] = useState<string | null>(null);

  const myRooms = chat.rooms.filter((r) => r.is_member);
  const otherRooms = chat.rooms.filter((r) => !r.is_member);
  const activeRoom = chat.rooms.find((r) => r.id === activeRoomId) ?? null;

  // Open the first room automatically.
  useEffect(() => {
    if (activeRoomId === null && myRooms.length) setActiveRoomId(myRooms[0].id);
  }, [activeRoomId, myRooms]);

  async function create(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const room = await chat.createRoom(newRoom.trim().toLowerCase());
      setNewRoom("");
      setActiveRoomId(room.id);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2">
        <span className="text-lg font-bold">
          Pulse<span className="text-violet-600">wire</span>
        </span>
        <div className="flex items-center gap-3 text-sm">
          <StatusBadge status={chat.status} instance={chat.instance} />
          <span className="text-slate-600">@{session.username}</span>
          <button onClick={onLogout} className="text-slate-500 hover:text-slate-900">
            Log out
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-64 flex-col gap-4 overflow-y-auto border-r border-slate-200 bg-white p-3">
          <section>
            <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Your rooms</h2>
            {myRooms.map((room) => (
              <button
                key={room.id}
                onClick={() => setActiveRoomId(room.id)}
                className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm ${
                  room.id === activeRoomId ? "bg-violet-100 font-semibold text-violet-900" : "hover:bg-slate-100"
                }`}
              >
                <span># {room.name}</span>
                <span className="flex items-center gap-1 text-xs text-slate-500" title="online now">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                  {room.online_count}
                </span>
              </button>
            ))}
          </section>

          {otherRooms.length > 0 && (
            <section>
              <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Other rooms</h2>
              {otherRooms.map((room) => (
                <div key={room.id} className="flex items-center justify-between px-2 py-1 text-sm">
                  <span className="text-slate-600"># {room.name}</span>
                  <button
                    onClick={async () => {
                      await chat.joinRoom(room.id);
                      setActiveRoomId(room.id);
                    }}
                    className="rounded border border-violet-300 px-2 text-xs text-violet-700 hover:bg-violet-50"
                  >
                    Join
                  </button>
                </div>
              ))}
            </section>
          )}

          <form onSubmit={create} className="mt-auto space-y-1">
            <input
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              placeholder="new-room-name"
              value={newRoom}
              pattern="[a-z0-9-]{2,64}"
              onChange={(e) => setNewRoom(e.target.value)}
            />
            <button className="w-full rounded-md bg-slate-900 py-1.5 text-sm text-white">Create room</button>
            {error && <p className="text-xs text-rose-600">{error}</p>}
          </form>
        </aside>

        <main className="min-w-0 flex-1">
          {activeRoom ? (
            <ChatWindow key={activeRoom.id} room={activeRoom} session={session} chat={chat} />
          ) : (
            <p className="p-6 text-slate-500">Join or create a room to start chatting.</p>
          )}
        </main>
      </div>
    </div>
  );
}
