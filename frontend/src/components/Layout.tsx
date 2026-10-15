import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useMatch } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "./AuthGate";
import { useLiveUpdates, useWorlds } from "../hooks/live";

const WORLD_PAGES = [
  ["difficulty", "⚔", "Difficulty"],
  ["server", "⚙", "Server"],
  ["roles", "⚿", "Roles & passwords"],
  ["backups", "⛁", "Backups"],
  ["updates", "⟳", "Updates"],
  ["players", "☺", "Players & activity"],
] as const;

function Brand() {
  return (
    <Link to="/" className="brand">
      <svg width="18" height="18" viewBox="0 0 32 32" aria-hidden="true">
        <path d="M16 3c2 5 8 8 8 15a8 8 0 0 1-16 0c0-4 2-6 4-8 0 3 1 5 3 5-1-4 0-8 1-12z" fill="currentColor" />
      </svg>
      FlameKeeper
    </Link>
  );
}

export function Layout() {
  const { connected } = useLiveUpdates();
  const { data: worlds = [] } = useWorlds();
  const { data: system } = useQuery({ queryKey: ["system"], queryFn: api.system, staleTime: 60_000 });
  const auth = useAuth();
  const match = useMatch("/worlds/:slug/*");
  const world = match ? worlds.find((w) => w.slug === match.params.slug) : undefined;
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setNavOpen(false), [location.pathname]);

  const setupError = system?.selfcheck?.fatal_error;

  return (
    <div className="app">
      <aside className={`side${navOpen ? " open" : ""}`}>
        <Brand />
        <nav className="nav" aria-label="Main">
          <NavLink to="/" end>
            <span className="ico">◫</span> Worlds
          </NavLink>
          <NavLink to="/new">
            <span className="ico">＋</span> New world
          </NavLink>
          {world && (
            <>
              <div className="navgroup">World · {world.display_name}</div>
              <NavLink to={`/worlds/${world.slug}`} end>
                <span className="ico">◉</span> Overview
              </NavLink>
              {WORLD_PAGES.map(([path, ico, label]) => (
                <NavLink key={path} to={`/worlds/${world.slug}/${path}`}>
                  <span className="ico">{ico}</span> {label}
                </NavLink>
              ))}
              <NavLink to={`/worlds/${world.slug}/logs`}>
                <span className="ico">☰</span> Logs
              </NavLink>
            </>
          )}
          <div className="navgroup">App</div>
          <NavLink to="/settings">
            <span className="ico">⚒</span> Settings
          </NavLink>
        </nav>
        <div className="foot">
          {auth && (
            <>
              Signed in as <b>{auth.username}</b> ·{" "}
              <button className="linklike" onClick={auth.signOut}>Sign out</button>
              <br />
            </>
          )}
          {system ? `v${system.version}` : ""}
          {!connected && <span style={{ color: "var(--warn)" }}> · reconnecting…</span>}
        </div>
      </aside>
      <div style={{ minWidth: 0 }}>
        <div className="mobilebar">
          <button className="btn sm" onClick={() => setNavOpen(!navOpen)} aria-label="Menu">
            ☰
          </button>
          <Brand />
        </div>
        <main>
          {setupError && (
            <div className="banner bad" role="alert">
              <b>Setup problem:</b> {setupError}
              <span className="spacer" />
              <Link className="btn sm" to="/settings">Self-check</Link>
            </div>
          )}
          <Outlet />
        </main>
      </div>
    </div>
  );
}
