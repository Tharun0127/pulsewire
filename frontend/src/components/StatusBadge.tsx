import type { ConnectionStatus } from "../socket";

const STYLES: Record<ConnectionStatus, string> = {
  connected: "bg-emerald-100 text-emerald-800",
  connecting: "bg-slate-200 text-slate-700",
  reconnecting: "bg-amber-100 text-amber-800",
  offline: "bg-rose-100 text-rose-800",
};

export default function StatusBadge({ status, instance }: { status: ConnectionStatus; instance: string | null }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${STYLES[status]}`}>
      <span
        className={`h-2 w-2 rounded-full ${
          status === "connected" ? "bg-emerald-500" : status === "offline" ? "bg-rose-500" : "animate-pulse bg-amber-500"
        }`}
      />
      {status}
      {status === "connected" && instance && <span className="font-mono opacity-70">· {instance}</span>}
    </span>
  );
}
