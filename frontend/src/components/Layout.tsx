import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useMatch } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "./AuthGate";
import { useLiveUpdates, useWorlds } from "../hooks/live";
import { updateTooltip } from "../settings/appUpdate";

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
  const { data: appUpdate } = useQuery({
    queryKey: ["app-update"],
    queryFn: api.appUpdate,
    staleTime: 60_000,
    refetchInterval: 30 * 60_000,
  });
  const updateTip = updateTooltip(appUpdate);
  const auth = useAuth();
  const match = useMatch("/worlds/:slug/*");
  const world = match ? worlds.find((w) => w.slug === match.params.slug) : undefined;
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setNavOpen(false), [location.pathname]);
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setNavOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  const setupError = system?.selfcheck?.fatal_error;

  return (
    <div className="app">
      {/* A link to the page already shown doesn't change the path: close the menu anyway. */}
      <aside
        id="side-nav"
        className={`side${navOpen ? " open" : ""}`}
        onClick={(e) => (e.target as HTMLElement).closest("a") && setNavOpen(false)}
      >
        <div className="side-head">
          <Brand />
          <button className="btn sm side-close" onClick={() => setNavOpen(false)} aria-label="Close menu">
            ←
          </button>
        </div>
        <nav className="nav" aria-label="Main">
          <NavLink to="/" end>
            <span className="ico">◫</span> Worlds
          </NavLink>
          <NavLink to="/new">
            <span className="ico">＋</span> New world
          </NavLink>
          <NavLink to="/import">
            <span className="ico">⇥</span> Import server
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
            {updateTip && (
              <span className="nav-alert" tabIndex={0} role="img" aria-label={updateTip} data-tip={updateTip}>
                !
              </span>
            )}
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
      {navOpen && <div className="side-backdrop" onClick={() => setNavOpen(false)} aria-hidden="true" />}
      <div style={{ minWidth: 0 }}>
        <div className="mobilebar">
          <button
            className="btn sm"
            onClick={() => setNavOpen(!navOpen)}
            aria-label="Menu"
            aria-expanded={navOpen}
            aria-controls="side-nav"
          >
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
