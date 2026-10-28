import { FormEvent, useState } from "react";
import { api, type ServerChoice, type Session } from "../api";

type Props = { onLogin: (session: Session, server: ServerChoice) => void; initialServer: ServerChoice };

export default function Login({ onLogin, initialServer }: Props) {
  const [signup, setSignup] = useState(false);
  const [username, setUsername] = useState("alice");
  const [password, setPassword] = useState("password123");
  const [server, setServer] = useState<ServerChoice>(initialServer);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.login(username, password, signup);
      onLogin({ token: result.access_token, userId: result.user_id, username: result.username }, server);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-3 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-2xl font-bold">
          Pulse<span className="text-violet-600">wire</span>
        </h1>
        <p className="text-sm text-slate-500">{signup ? "Create an account" : "Log in to chat"}</p>
        <input
          className="w-full rounded-md border border-slate-300 px-3 py-2"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
        />
        <input
          className="w-full rounded-md border border-slate-300 px-3 py-2"
          type="password"
          placeholder="Password (min 8)"
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <label className="block text-sm">
          <span className="text-slate-600">Connect through</span>
          <select
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
            value={server}
            onChange={(e) => setServer(e.target.value as ServerChoice)}
          >
            <option value="auto">Load balancer (auto)</option>
            <option value="api-1">api-1 only (demo)</option>
            <option value="api-2">api-2 only (demo)</option>
          </select>
        </label>
        {error && <p className="text-sm text-rose-600">{error}</p>}
        <button
          disabled={busy}
          className="w-full rounded-md bg-violet-600 py-2 font-semibold text-white hover:bg-violet-700 disabled:opacity-50"
        >
          {busy ? "..." : signup ? "Sign up" : "Log in"}
        </button>
        <button type="button" onClick={() => setSignup(!signup)} className="text-sm text-violet-600 hover:underline">
          {signup ? "Have an account? Log in" : "No account? Sign up"}
        </button>
        <p className="text-xs text-slate-500">
          Demo users: alice / bob, password <span className="font-mono">password123</span>. Open two windows, put
          alice on api-1 and bob on api-2.
        </p>
      </form>
    </div>
  );
}
