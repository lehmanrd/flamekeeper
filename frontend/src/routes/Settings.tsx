import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useState } from "react";
import { Toggle } from "../components/Toggle";
import { api } from "../api/client";
import type { SelfCheck } from "../api/types";
import { formatBytes } from "../format";

const TONE: Record<SelfCheck["status"], string> = { ok: "ok", warn: "warn", fail: "bad", skip: "off" };

export function Settings() {
  const qc = useQueryClient();
  const { data: system, isLoading } = useQuery({ queryKey: ["system"], queryFn: api.system });
  const rerun = useMutation({
    mutationFn: api.rerunSelfcheck,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["system"] }),
  });

  return (
    <>
      <div className="top">
        <h1>Settings</h1>
      </div>
      {isLoading && <p className="muted">Loading…</p>}
      <AppSettingsCard />
      {system && (
        <div className="grid g2">
          <div className="card">
            <div className="top" style={{ margin: "0 0 10px" }}>
              <h3 style={{ margin: 0 }}>Self-check</h3>
              <span className="spacer" />
              <button className="btn sm" disabled={rerun.isPending} onClick={() => rerun.mutate()}>
                {rerun.isPending ? "Checking…" : "Run again"}
              </button>
            </div>
            <table>
              <tbody>
                {system.selfcheck?.checks.map((c) => (
                  <tr key={c.id}>
                    <td>{c.label}</td>
                    <td><span className={`pill ${TONE[c.status]}`}>{c.status}</span></td>
                    <td className="muted" style={{ overflowWrap: "anywhere" }}>{c.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card">
            <h3>Host</h3>
            <dl className="kv">
              <dt>Version</dt><dd>{system.version}</dd>
              <dt>Docker</dt><dd>{system.docker_version ?? "—"}</dd>
              <dt>Kernel</dt><dd className="mono">{system.host.kernel}</dd>
              <dt>CPU cores</dt><dd>{system.host.cpus ?? "—"}</dd>
              <dt>Memory</dt><dd>{formatBytes(system.host.mem_total)}</dd>
              <dt>Data dir</dt><dd className="mono">{system.data_dir}</dd>
              <dt>Disk free</dt><dd>{formatBytes(system.host.disk_free)} of {formatBytes(system.host.disk_total)}</dd>
              <dt>ntsync</dt><dd>{system.ntsync_available ? "available" : "not available"}</dd>
              <dt>Base path</dt><dd className="mono">{system.base_path}</dd>
              <dt>Server queries</dt><dd>{system.a2s_mode === "network" ? "via Docker network" : "via host port"}</dd>
            </dl>
            <SettingsReadme />
            <div className="note">
              FlameKeeper has no login. Anyone who can reach this page can control your servers, so keep it on a
              trusted network or behind a reverse proxy with authentication.
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** Keen's enshrouded_server_readme.txt from the installed game, compared with FlameKeeper's settings. */
function SettingsReadme() {
  const { data } = useQuery({ queryKey: ["settings-readme"], queryFn: api.settingsReadme, staleTime: 60_000 });
  if (!data) return null;
  return (
    <>
      <h3 style={{ marginTop: 18 }}>Game settings readme</h3>
      {data.readmes.length === 0 ? (
        <p className="muted">No world has the game installed yet.</p>
      ) : (
        data.readmes.map((r) => (
          <div key={r.sha} style={{ marginBottom: 10 }}>
            <p style={{ margin: "0 0 6px" }}>
              Version <b>{r.version ?? "unknown"}</b> · {r.settings} settings ·{" "}
              <span className="muted">{r.worlds.join(", ")}</span>{" "}
              {r.drift.length === 0 ? (
                <span className="pill ok">matches FlameKeeper</span>
              ) : (
                <span className="pill warn">{r.drift.length} difference{r.drift.length === 1 ? "" : "s"}</span>
              )}
            </p>
            {r.drift.length > 0 && (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {r.drift.map((d) => <li key={d.kind + d.key}>{d.message}</li>)}
              </ul>
            )}
          </div>
        ))
      )}
      <p className="muted" style={{ fontSize: 12 }}>
        Keen ships <span className="mono">enshrouded_server_readme.txt</span> with the server and updates it when
        settings change. FlameKeeper checks it after each start and adds a warning to the world's events if a new
        version lists settings it doesn't know yet.
      </p>
    </>
  );
}

/** Global settings stored by FlameKeeper (override the environment defaults). */
function AppSettingsCard() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["app-settings"], queryFn: api.appSettings });
  const [draft, setDraft] = useState<Record<string, string | number | boolean>>({});
  useEffect(() => {
    if (data) setDraft(data.values);
  }, [data]);
  const save = useMutation({
    mutationFn: () => {
      const changes: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(draft)) if (data && v !== data.values[k]) changes[k] = v;
      return api.saveAppSettings(changes);
    },
    onSuccess: (v) => qc.setQueryData(["app-settings"], v),
  });
  if (!data) return null;
  const dirty = Object.entries(draft).some(([k, v]) => v !== data.values[k]);
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="top" style={{ margin: "0 0 10px" }}>
        <h3 style={{ margin: 0 }}>Defaults &amp; checks</h3>
        <span className="spacer" />
        <button className="btn sm primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>Save</button>
      </div>
      <div className="form" style={{ gridTemplateColumns: "240px 1fr" }}>
        {Object.entries(data.fields).map(([key, f]) => {
          const v = draft[key];
          const id = `s-${key}`;
          const changed = data.overridden.includes(key);
          return (
            <Fragment key={key}>
              <label htmlFor={id}>{f.label}</label>
              <span className="inline">
                {f.type === "bool" ? (
                  <Toggle checked={Boolean(v)} onChange={(nv) => setDraft({ ...draft, [key]: nv })} label={f.label} />
                ) : f.type === "enum" ? (
                  <select id={id} value={String(v)} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}>
                    {f.options?.map((o) => <option key={o} value={o}>{o === "steamcmd" ? "steamcmd helper container" : o === "api" ? "api.steamcmd.net" : o}</option>)}
                  </select>
                ) : (
                  <input
                    id={id}
                    className={f.type === "str" ? "mono" : undefined}
                    type={f.type === "int" ? "number" : "text"}
                    min={f.min}
                    max={f.max}
                    value={String(v ?? "")}
                    onChange={(e) => setDraft({ ...draft, [key]: f.type === "int" ? Number(e.target.value) : e.target.value })}
                    style={{ width: f.type === "int" ? 110 : 200 }}
                  />
                )}
                {changed && (
                  <span className="muted small">
                    default {String(data.defaults[key])}{" "}
                    <button className="btn sm" onClick={() => setDraft({ ...draft, [key]: data.defaults[key] })}>Reset</button>
                  </span>
                )}
              </span>
            </Fragment>
          );
        })}
      </div>
      {save.error && <p className="error-text">{(save.error as Error).message}</p>}
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
        Saved here, these override the environment variables FlameKeeper was started with.
      </p>
    </div>
  );
}
