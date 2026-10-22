import { useEffect, useState } from "react";
import { bootstrap } from "./api/client";
import AgentsPage from "./pages/AgentsPage";
import DebateRoom from "./pages/DebateRoom";
import FilesPage from "./pages/FilesPage";
import ProfilesPage from "./pages/ProfilesPage";
import RolesPage from "./pages/RolesPage";
import SettingsPage from "./pages/SettingsPage";

type Page = "room" | "agents" | "profiles" | "roles" | "files" | "settings";
const NAV: { id: Page; label: string; icon: string }[] = [
  { id: "room", label: "討論ルーム", icon: "💬" },
  { id: "agents", label: "編成", icon: "👥" },
  { id: "profiles", label: "API接続", icon: "🔌" },
  { id: "roles", label: "役割", icon: "📝" },
  { id: "files", label: "ファイル", icon: "📁" },
  { id: "settings", label: "設定", icon: "⚙️" },
];

function parseHash(): { page: Page; session: string | null } {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [p, s] = h.split("/");
  const page = (NAV.find((n) => n.id === p)?.id || "room") as Page;
  return { page, session: page === "room" && s ? s : null };
}

export default function App() {
  const [{ page, session }, setRoute] = useState(parseHash());
  const [demo, setDemo] = useState(false);
  const [bootErr, setBootErr] = useState("");
  useEffect(() => {
    bootstrap().then((r) => setDemo(r.demo_mode)).catch((e) => setBootErr(String(e.message || e)));
    const on = () => setRoute(parseHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = (p: Page, s: string | null = null) => {
    window.location.hash = p === "room" && s ? `/room/${s}` : `/${p}`;
  };
  return (
    <div className="app">
      <nav className="nav">
        <div className="brand">Debate<br />Studio</div>
        {NAV.map((n) => (
          <button key={n.id} className={`nav-item ${page === n.id ? "active" : ""}`} onClick={() => go(n.id, n.id === "room" ? session : null)}>
            <span className="nav-icon">{n.icon}</span>
            <span className="nav-label">{n.label}</span>
          </button>
        ))}
        {demo && <div className="demo-badge" title="FakeProviderが有効です（課金なし）">DEMO</div>}
      </nav>
      <div className="content">
        {bootErr && <div className="err pad">サーバーに接続できません: {bootErr}</div>}
        {page === "room" && <DebateRoom sessionId={session} setSessionId={(id) => go("room", id)} />}
        {page === "agents" && <AgentsPage />}
        {page === "profiles" && <ProfilesPage />}
        {page === "roles" && <RolesPage />}
        {page === "files" && <FilesPage />}
        {page === "settings" && <SettingsPage />}
      </div>
    </div>
  );
}
