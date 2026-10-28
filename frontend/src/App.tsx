import { useState } from "react";
import { loadServerChoice, loadSession, saveServerChoice, saveSession, type ServerChoice, type Session } from "./api";
import Login from "./components/Login";
import ChatApp from "./components/ChatApp";

export default function App() {
  const [session, setSession] = useState<Session | null>(loadSession());
  const [server, setServer] = useState<ServerChoice>(loadServerChoice());

  function login(next: Session, choice: ServerChoice) {
    saveSession(next);
    saveServerChoice(choice);
    setServer(choice);
    setSession(next);
  }

  function logout() {
    saveSession(null);
    setSession(null);
  }

  if (!session) return <Login onLogin={login} initialServer={server} />;
  // key: a new socket (and fresh state) per user + server choice.
  return <ChatApp key={`${session.userId}-${server}`} session={session} server={server} onLogout={logout} />;
}
