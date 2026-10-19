import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { ImportCandidate, ImportScan } from "../api/types";
import { useJobFocus } from "../components/JobsDrawer";
import { Toggle } from "../components/Toggle";
import { formatBytes, formatTime } from "../format";
import { useWorlds } from "../hooks/live";
import { SCHEDULE_PRESETS } from "../settings/backups";
import {
  FROM_SOURCE,
  IMPORT_STEPS,
  fromScan,
  importBody,
  importSlug,
  importStepErrors,
  initialImportState,
  needsPlayerConfirm,
  portNeedsCheck,
  type ImportState,
  type SourceKind,
} from "../settings/importWizard";

const SOURCES: [SourceKind, string, string][] = [
  ["container", "Docker container", "A server running in Docker on this host, from any Enshrouded image."],
  ["host_path", "Folder on this host", "A server installed without Docker, or a copy of a server's folder."],
  ["upload", "Backup zip", "A zip with the savegame folder and, ideally, enshrouded_server.json."],
];

/** Import an Enshrouded server FlameKeeper didn't create: find it, review, create a world. */
export function ImportWorld() {
  const { data: app } = useQuery({ queryKey: ["app-settings"], queryFn: api.appSettings, staleTime: 60_000 });
  if (!app) return <p className="muted">Loading…</p>;
  return <ImportWizard defaultTag={String(app.values.default_image_tag)} />;
}

function ImportWizard({ defaultTag }: { defaultTag: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const focusJob = useJobFocus();
  const { data: worlds = [] } = useWorlds();
  const [s, setS] = useState<ImportState>(() => initialImportState(defaultTag));
  const [scan, setScan] = useState<ImportScan | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [step, setStep] = useState(0);
  const [tried, setTried] = useState(false);
  const set = (patch: Partial<ImportState>) => setS((x) => ({ ...x, ...patch }));

  const runScan = useMutation({
    mutationFn: () =>
      s.kind === "upload"
        ? api.uploadImport(file!)
        : s.kind === "container"
          ? api.scanImport({ kind: "container", container: s.container })
          : api.scanImport({ kind: "host_path", path: s.path }),
    onSuccess: (res) => {
      setScan(res);
      setS((x) => fromScan(x, res));
      setStep(1);
    },
  });

  const portNum = s.port ? Number(s.port) : null;
  const { data: portCheck } = useQuery({
    queryKey: ["check-port", portNum],
    queryFn: () => api.checkPort(portNum!),
    enabled: portNeedsCheck(s, scan) && portNum !== null && portNum >= 1024 && portNum <= 65535,
  });
  const errors = importStepErrors(s, step, scan, portCheck ? portCheck.free : null);
  const preview = useQuery({
    queryKey: ["import-preview", scan?.token, JSON.stringify(importBody(s, scan))],
    queryFn: () => api.previewImport(scan!.token, importBody(s, scan)),
    enabled: step === 3 && scan !== null && !errors.length,
    retry: false,
  });
  const create = useMutation({
    mutationFn: (start: boolean) => api.createImport(scan!.token, importBody(s, scan, start)),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["worlds"] });
      focusJob(res.job_id);
      navigate(`/worlds/${res.world.slug}`);
    },
  });

  const canScan = s.kind === "container" ? Boolean(s.container) : s.kind === "host_path" ? s.path.startsWith("/") : file !== null;
  const next = () => {
    setTried(true);
    if (!errors.length) {
      setTried(false);
      setStep(step + 1);
    }
  };
  const installedWorlds = worlds.filter((w) => w.build_id);
  const stopsContainer = scan?.source.kind === "container";

  return (
    <>
      <div className="top">
        <h1>Import a server</h1>
      </div>
      <ol className="wizard-steps" aria-label="Steps">
        {IMPORT_STEPS.map((label, i) => (
          <li key={label} className={i < step ? "done" : i === step ? "cur" : ""}>
            <button type="button" disabled={i > step || (i > 0 && !scan)} onClick={() => setStep(i)}>{i < step ? "✔" : i + 1} · {label}</button>
          </li>
        ))}
      </ol>
      <div className="grid g2">
        <div className="card">
          {step === 0 && (
            <>
              <div className="preset-cards" role="radiogroup" aria-label="Where the server is">
                {SOURCES.map(([kind, label, desc]) => (
                  <button key={kind} type="button" role="radio" aria-checked={s.kind === kind} className={`preset-card${s.kind === kind ? " on" : ""}`} onClick={() => set({ kind })}>
                    <b>{label}</b>
                    <span>{desc}</span>
                  </button>
                ))}
              </div>
              <div style={{ marginTop: 16 }}>
                {s.kind === "container" && <ContainerPicker value={s.container} onChange={(container) => set({ container })} />}
                {s.kind === "host_path" && (
                  <div className="form">
                    <label htmlFor="i-path">Server folder</label>
                    <input id="i-path" className="mono" value={s.path} placeholder="/srv/enshrouded" onChange={(e) => set({ path: e.target.value })} />
                    <span className="hint">
                      The folder with enshrouded_server.json and the savegame folder (or any folder above them). FlameKeeper
                      only reads it. Stop that server before importing so its last changes are saved.
                    </span>
                  </div>
                )}
                {s.kind === "upload" && (
                  <div className="form">
                    <label htmlFor="i-zip">Zip file</label>
                    <input id="i-zip" type="file" accept=".zip,application/zip" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
                    <span className="hint">A FlameKeeper backup, or any zip with the savegame folder (world0.ksd, or 3ad85aea files) and the config.</span>
                  </div>
                )}
              </div>
              <p className="muted" style={{ marginBottom: 0 }}>Scanning only reads the server's files; nothing changes until the last step.</p>
              {runScan.error && <div className="banner bad">{(runScan.error as Error).message}</div>}
            </>
          )}

          {step === 1 && scan && <FoundStep scan={scan} s={s} set={set} />}

          {step === 2 && scan && (
            <div className="form">
              <label htmlFor="i-name">Name</label>
              <input id="i-name" value={s.name} maxLength={100} onChange={(e) => set({ name: e.target.value })} />
              <label htmlFor="i-slug">Slug</label>
              <input id="i-slug" className="mono" value={importSlug(s)} onChange={(e) => set({ slug: e.target.value, slugEdited: true })} />
              <span className="hint">Folder and container name: flamekeeper-{importSlug(s) || "…"}</span>
              <label htmlFor="i-port">Query port (UDP)</label>
              <span>
                <input id="i-port" value={s.port} onChange={(e) => set({ port: e.target.value.replace(/\D/g, "") })} style={{ width: 110 }} />{" "}
                {Number(s.port) === scan.reusable_port ? (
                  <span className="ok-text">the old server's port ✔</span>
                ) : (
                  portCheck && <span className={portCheck.free ? "ok-text" : "error-text"}>{portCheck.free ? "free ✔" : portCheck.reason}</span>
                )}
              </span>
              <span className="hint">
                {scan.reusable_port
                  ? "Keeping the old server's port means players' saved servers and your router's port forward keep working."
                  : "Players join on this port; forward it on your router for people outside your network."}
              </span>
              <label htmlFor="i-tag">Image tag</label>
              <span>
                <select id="i-tag" value={["stable-wine", "latest"].includes(s.imageTag) ? s.imageTag : "custom"} onChange={(e) => set({ imageTag: e.target.value === "custom" ? "" : e.target.value })}>
                  <option value="stable-wine">stable-wine (Wine): recommended</option>
                  <option value="latest">latest (Proton)</option>
                  <option value="custom">pinned version…</option>
                </select>
                {!["stable-wine", "latest"].includes(s.imageTag) && (
                  <input className="mono" value={s.imageTag} placeholder="tag" onChange={(e) => set({ imageTag: e.target.value })} style={{ width: 140, marginLeft: 6 }} />
                )}
              </span>
              <span className="hint">FlameKeeper runs worlds with the mornedhels/enshrouded-server image, whatever image the old server used.</span>
              <label htmlFor="i-game">Game files</label>
              <select id="i-game" value={s.copyFrom} onChange={(e) => set({ copyFrom: e.target.value })}>
                {scan.game_files && (
                  <option value={FROM_SOURCE}>
                    Copy from the old server{scan.game_files.build ? ` (build ${scan.game_files.build})` : ""}
                  </option>
                )}
                <option value="">Download from Steam (about 9 GB)</option>
                {installedWorlds.map((w) => (
                  <option key={w.id} value={w.id}>Copy from {w.display_name} (build {w.build_id})</option>
                ))}
              </select>
              <span className="hint">
                {s.copyFrom === FROM_SOURCE
                  ? `Copied from ${scan.game_files?.path} while the old server keeps running. The first start checks them against Steam and downloads only what's missing or newer.`
                  : stopsContainer && !s.copyFrom
                    ? "The old server is down while the new one downloads; copying is much faster."
                    : ""}
              </span>
              <label htmlFor="i-bak">Backups</label>
              <span>
                <select id="i-bak" value={s.backupCron} onChange={(e) => set({ backupCron: e.target.value })}>
                  {SCHEDULE_PRESETS.map((p) => <option key={p.cron} value={p.cron}>{p.label}</option>)}
                  <option value="">Off</option>
                </select>{" "}
                {s.backupCron && (
                  <>
                    keep <input type="number" min={0} value={Number.isFinite(s.keep) ? s.keep : ""} onChange={(e) => set({ keep: e.target.value === "" ? NaN : Number(e.target.value) })} style={{ width: 70 }} aria-label="Backups to keep" />
                  </>
                )}
              </span>
            </div>
          )}

          {step === 3 && scan && (
            <>
              <h3 style={{ marginTop: 0 }}>What happens</h3>
              <ol>
                {s.copyFrom === FROM_SOURCE && (
                  <li>Its game files are copied into the new world{stopsContainer && scan.running ? " while it keeps running" : ""}.</li>
                )}
                {stopsContainer && scan.running && (
                  <li>
                    <b>{scan.source.label}</b> is stopped (the game saves on shutdown) and its restart policy set to <span className="mono">no</span>, so it
                    doesn't come back after a reboot. Docker Compose would still start it on <span className="mono">docker compose up</span>.
                  </li>
                )}
                {stopsContainer && !scan.running && (
                  <li>Its restart policy is set to <span className="mono">no</span>, so it doesn't start again after a reboot.</li>
                )}
                <li>Its savegame and settings are copied. The copy is kept as the new world's first backup.</li>
                <li>
                  The world <b>{s.name}</b> is created on port <span className="mono">{s.port}/udp</span>
                  {s.copyFrom === FROM_SOURCE
                    ? " and started; its first start checks the copied game files against Steam"
                    : s.copyFrom
                      ? " with game files copied from another world"
                      : ", and its first start downloads the game (about 9 GB)"}
                  .
                </li>
                {stopsContainer && (
                  <>
                    <li>If anything fails, the old server gets its restart policy back{scan.running ? " and is started again" : ""}.</li>
                    <li>Removing the old server is a separate step afterwards, on the new world's page.</li>
                  </>
                )}
              </ol>
              {scan.source.kind === "host_path" && (
                <div className="banner warn">Stop the server in {scan.source.host_path} yourself before importing, so its latest progress is saved.</div>
              )}
              {needsPlayerConfirm(scan) && (
                <div className="banner warn">
                  {scan.players_online
                    ? `${scan.players_online} player${scan.players_online === 1 ? " is" : "s are"} online on ${scan.source.label}.`
                    : `FlameKeeper can't tell from ${scan.source.label}'s log whether anyone is online.`}{" "}
                  <label className="check">
                    <input type="checkbox" checked={s.confirmPlayers} onChange={(e) => set({ confirmPlayers: e.target.checked })} /> Stop it anyway
                  </label>
                </div>
              )}
              {preview.isLoading && <p className="muted">Checking…</p>}
              {preview.error && <div className="banner bad">{(preview.error as Error).message}</div>}
              {preview.data?.capacity.warnings.map((w) => <div key={w} className="banner warn">⚠ {w} You can continue.</div>)}
              {create.error && <div className="banner bad">{(create.error as Error).message}</div>}
            </>
          )}

          {tried && errors.length > 0 && <ul className="error-text">{errors.map((e) => <li key={e}>{e}</li>)}</ul>}
          <div className="top" style={{ margin: "18px 0 0" }}>
            {step > 0 && <button className="btn" onClick={() => setStep(step - 1)}>← Back</button>}
            <span className="spacer" />
            {step === 0 && (
              <button className="btn primary" disabled={!canScan || runScan.isPending} onClick={() => runScan.mutate()}>
                {runScan.isPending ? "Scanning…" : "Scan →"}
              </button>
            )}
            {step > 0 && step < 3 && <button className="btn primary" onClick={next}>Next: {IMPORT_STEPS[step + 1]} →</button>}
            {step === 3 && (
              <>
                <button className="btn" disabled={!preview.data || create.isPending || errors.length > 0} onClick={() => create.mutate(false)}>
                  Import, don't start
                </button>
                <button className="btn primary" disabled={!preview.data || create.isPending || errors.length > 0} onClick={() => create.mutate(true)}>
                  {create.isPending ? "Importing…" : "Import & start"}
                </button>
              </>
            )}
          </div>
        </div>

        <div className="card">
          <h3>About importing</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            FlameKeeper creates a new world and copies the old server's savegame, settings, roles (with their passwords)
            and bans into it. Any Enshrouded image works: FlameKeeper looks for the files, not for a particular image.
          </p>
          <p className="muted">
            Characters live on the players' PCs, so they only need to join the new world. Keep the old port and they find
            it where it was.
          </p>
          {scan && (
            <dl className="kv">
              <dt>Source</dt><dd>{scan.source.label}</dd>
              <dt>Server</dt><dd>{scan.summary.name ?? "—"}</dd>
              <dt>Save</dt><dd>{scan.summary.save_format === "1.0" ? "game 1.0" : "before 1.0"} · {formatBytes(scan.summary.save_bytes)}</dd>
              {step >= 2 && (<><dt>New world</dt><dd>{s.name || "—"} · <span className="mono">{s.port}/udp</span></dd></>)}
            </dl>
          )}
        </div>
      </div>
    </>
  );
}

function ContainerPicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  const [all, setAll] = useState(false);
  const { data, isLoading, error, refetch, isFetching } = useQuery({ queryKey: ["import-candidates"], queryFn: api.importCandidates });
  if (isLoading) return <p className="muted">Looking for containers…</p>;
  if (error) return <div className="banner bad">{(error as Error).message}</div>;
  const shown = (data ?? []).filter((c) => all || c.likely);
  const hidden = (data ?? []).length - shown.length;
  return (
    <>
      {!shown.length && (
        <p className="muted">
          No container looks like an Enshrouded server{hidden ? `; ${hidden} other container${hidden === 1 ? "" : "s"} hidden` : ""}.
        </p>
      )}
      <div className="preset-cards import-list" role="radiogroup" aria-label="Container">
        {shown.map((c) => (
          <button key={c.id} type="button" role="radio" aria-checked={value === c.id} className={`preset-card${value === c.id ? " on" : ""}`} onClick={() => onChange(c.id)}>
            <b>
              {c.name}{" "}
              <span className={`pill ${c.running ? "ok" : "off"}`}>{c.status}</span>
            </b>
            <span className="mono">{c.image}</span>
            <span>{describeContainer(c)}</span>
            {c.imported_into && <span className="field-warn">Already imported into {c.imported_into}</span>}
          </button>
        ))}
      </div>
      <div className="top" style={{ margin: "10px 0 0" }}>
        <Toggle checked={all} onChange={setAll} label="Show all containers" /> <span className="muted">Show all containers</span>
        <span className="spacer" />
        <button className="btn sm" disabled={isFetching} onClick={() => refetch()}>↻ Refresh</button>
      </div>
    </>
  );
}

function describeContainer(c: ImportCandidate): string {
  const ports = Object.entries(c.ports).map(([p, hosts]) => `${hosts.join(", ")}→${p}`);
  const mounts = c.mounts.filter((m) => m.type === "bind" || m.type === "volume").map((m) => m.name ?? m.source);
  return [ports.length ? `ports ${ports.join(" ")}` : "no published ports", mounts.length ? `data in ${mounts.join(", ")}` : "no volumes"].join(" · ");
}

function FoundStep({ scan, s, set }: { scan: ImportScan; s: ImportState; set: (p: Partial<ImportState>) => void }) {
  const sum = scan.summary;
  return (
    <>
      {scan.errors.map((e) => <div key={e} className="banner bad">{e}</div>)}
      <dl className="kv">
        <dt>Server name</dt><dd>{sum.name ?? "—"}</dd>
        <dt>Max players</dt><dd>{sum.slot_count ?? "—"}</dd>
        <dt>Port</dt><dd className="mono">{scan.reusable_port ?? sum.query_port ?? "—"}/udp</dd>
        <dt>Difficulty</dt><dd>{sum.preset ?? "—"}{sum.custom_settings ? ` · ${sum.custom_settings} game settings` : ""}</dd>
        <dt>Roles</dt>
        <dd>
          {sum.roles.length ? sum.roles.map((r) => `${r.name}${r.has_password ? "" : " (open)"}`).join(", ") : "—"}
          {sum.legacy_password && " · server password"}
        </dd>
        <dt>Bans</dt><dd>{sum.bans}</dd>
        <dt>Savegame</dt>
        <dd>
          {sum.save_format === "1.0" ? "game 1.0" : "before 1.0"} · {sum.save_files} file{sum.save_files === 1 ? "" : "s"} · {formatBytes(sum.save_bytes)}
          {sum.saved_at && ` · last saved ${formatTime(sum.saved_at)}`}
        </dd>
        {scan.source.kind === "container" && (
          <>
            <dt>Players online</dt><dd>{scan.players_online ?? "unknown"}</dd>
          </>
        )}
        {scan.config_path && (<><dt>Config</dt><dd className="mono">{scan.config_path}</dd></>)}
      </dl>
      {scan.save_options.length > 1 ? (
        <div className="form" style={{ marginTop: 12 }}>
          <label htmlFor="i-save">Save folder</label>
          <select id="i-save" className="mono" value={s.savePath} onChange={(e) => set({ savePath: e.target.value })}>
            {scan.save_options.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <span className="hint">The summary above describes {scan.save_path}.</span>
        </div>
      ) : (
        scan.save_path && <p className="muted mono" style={{ fontSize: 12 }}>{scan.save_path}</p>
      )}
      {scan.warnings.map((w) => <div key={w} className="banner warn">⚠ {w}</div>)}
      {!scan.errors.length && (
        <p className="muted" style={{ marginBottom: 0 }}>
          Settings, roles and passwords carry over; you can change them after the import. Not what you expected?{" "}
          <Link to="/import" reloadDocument>Start over</Link>.
        </p>
      )}
    </>
  );
}
