import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { BackupItem, BackupMode, BackupSchedule, DiskInfo, WorldSummary } from "../api/types";
import { CloneDialog } from "../components/CloneDialog";
import { useJobFocus } from "../components/JobsDrawer";
import { Modal } from "../components/Modal";
import { Toggle } from "../components/Toggle";
import { WhenChoice, playersText, type When } from "../components/WorldActions";
import { formatBytes, formatTime, isRunningState, since } from "../format";
import { useJobs } from "../hooks/live";
import { KIND_LABEL, KIND_TONE, SCHEDULE_PRESETS, presetFor, retentionText, totalSize } from "../settings/backups";
import { NotFound } from "./NotFound";
import { useWorldBySlug } from "./WorldOverview";

export function WorldBackups() {
  const { world, loading } = useWorldBySlug();
  if (loading) return <p className="muted">Loading…</p>;
  if (!world) return <NotFound what="World" />;
  return <Backups key={world.id} world={world} />;
}

type Dialog =
  | null
  | { kind: "create" }
  | { kind: "restore" | "delete" | "note" | "clone"; backup: BackupItem };

function Backups({ world }: { world: WorldSummary }) {
  const qc = useQueryClient();
  const { data: jobs = [] } = useJobs();
  const { data, error, refetch } = useQuery({ queryKey: ["backups", world.id], queryFn: () => api.backups(world.id) });
  const [dialog, setDialog] = useState<Dialog>(null);
  const [menu, setMenu] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Refresh the list when a backup/restore job for this world finishes.
  const doneJobs = jobs.filter((j) => j.world_id === world.id && ["backup", "restore"].includes(j.type) && j.status !== "running" && j.status !== "queued").map((j) => j.id).join();
  useEffect(() => {
    if (doneJobs) refetch();
  }, [doneJobs, refetch]);

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadBackup(world.id, file),
    onSuccess: (b) => {
      qc.invalidateQueries({ queryKey: ["backups", world.id] });
      setNotice(
        `Uploaded ${b.save_files} save file${b.save_files === 1 ? "" : "s"}${b.has_config ? " and a config" : ""}.` +
          (b.warnings.length ? ` Warning: ${b.warnings.join(" ")}` : ""),
      );
    },
  });

  if (error) return <p className="error-text">{(error as Error).message}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const { backups, schedule, disk, savegame_saved_at: lastSave } = data;

  return (
    <>
      <div className="top">
        <h1>Backups</h1>
        <span className="muted">
          {world.display_name} · {backups.length} backup{backups.length === 1 ? "" : "s"} · {formatBytes(totalSize(backups))}
        </span>
        <span className="spacer" />
        <input
          ref={fileRef}
          type="file"
          accept=".zip,application/zip"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload.mutate(f);
            e.target.value = "";
          }}
        />
        <button className="btn" disabled={upload.isPending} onClick={() => fileRef.current?.click()}>
          {upload.isPending ? "Uploading…" : "Upload…"}
        </button>
        <button className="btn primary" disabled={world.busy} onClick={() => setDialog({ kind: "create" })}>
          Back up now
        </button>
      </div>
      {upload.error && <div className="banner bad" role="alert">{(upload.error as Error).message}</div>}
      {notice && <div className="banner info" role="status">{notice}</div>}
      {disk.warnings.map((w) => (
        <div key={w} className="banner warn">{w}</div>
      ))}

      <div className="grid g2">
        <div className="card">
          {backups.length === 0 ? (
            <div className="empty">
              <h2>No backups yet</h2>
              <p>
                {schedule.enabled
                  ? `The first scheduled backup runs ${schedule.next_run_at ? formatTime(schedule.next_run_at) : "soon"}${schedule.skip_if_idle ? " if someone has played" : ""}.`
                  : "Scheduled backups are off."}{" "}
                Or back up now.
              </p>
            </div>
          ) : (
            <div className="dg-scroll">
              <table className="backups">
                <thead>
                  <tr>
                    <th>Created</th><th>Kind</th><th>Size</th><th>Build</th><th>Note</th><th><span className="sr-only">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {backups.map((b) => (
                    <tr key={b.id}>
                      <td style={{ whiteSpace: "nowrap" }} title={b.filename}>
                        {formatTime(b.created_at)}
                        {savedEarlier(b) && <div className="muted small">world saved {formatTime(b.saved_at)}</div>}
                      </td>
                      <td><span className={`pill ${KIND_TONE[b.kind]}`}>{KIND_LABEL[b.kind]}</span></td>
                      <td style={{ whiteSpace: "nowrap" }}>{formatBytes(b.size_bytes)}</td>
                      <td className="mono">{b.game_build_id ?? "—"}</td>
                      <td className={b.note ? "" : "muted"}>{b.note || "—"}</td>
                      <td className="actions-cell">
                        <button className="btn sm" disabled={world.busy} onClick={() => setDialog({ kind: "restore", backup: b })}>Restore</button>{" "}
                        <a className="btn sm" href={api.backupDownloadUrl(world.id, b.id)} download title="Download (includes role passwords)" aria-label={`Download ${b.filename}`}>⤓</a>{" "}
                        <span className="menu">
                          <button className="btn sm" aria-label={`More for ${b.filename}`} aria-expanded={menu === b.id} onClick={() => setMenu(menu === b.id ? null : b.id)}>⋯</button>
                          {menu === b.id && (
                            <div className="menu-list" role="menu" onClick={() => setMenu(null)}>
                              <button role="menuitem" onClick={() => setDialog({ kind: "note", backup: b })}>Edit note</button>
                              <button role="menuitem" onClick={() => setDialog({ kind: "clone", backup: b })}>Clone to new world…</button>
                              <button role="menuitem" className="danger" onClick={() => setDialog({ kind: "delete", backup: b })}>Delete…</button>
                            </div>
                          )}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="note">
            Each backup holds the savegame and <span className="mono">enshrouded_server.json</span>, including role
            passwords: keep downloads private. Restoring stops the server, saves a “before restore” backup, puts the
            backup's savegame in place and starts it again.
          </div>
        </div>
        <div>
          <ScheduleCard world={world} schedule={schedule} backups={backups} />
          <DiskCard disk={disk} worldId={world.id} />
        </div>
      </div>

      <CreateDialog world={world} lastSave={lastSave} open={dialog?.kind === "create"} onClose={() => setDialog(null)} />
      {dialog && dialog.kind === "restore" && <RestoreDialog world={world} backup={dialog.backup} onClose={() => setDialog(null)} />}
      {dialog && dialog.kind === "delete" && <DeleteDialog world={world} backup={dialog.backup} onClose={() => setDialog(null)} />}
      {dialog && dialog.kind === "note" && <NoteDialog world={world} backup={dialog.backup} onClose={() => setDialog(null)} />}
      {dialog && dialog.kind === "clone" && (
        <CloneDialog world={world} source={dialog.backup.id} sourceLabel={`the backup from ${formatTime(dialog.backup.created_at)}`} open onClose={() => setDialog(null)} />
      )}
    </>
  );
}

/** True when the backup's savegame is noticeably older than the backup itself. */
function savedEarlier(b: BackupItem): boolean {
  return Boolean(b.saved_at && new Date(b.created_at).getTime() - new Date(b.saved_at).getTime() > 60_000);
}

function CreateDialog({ world, lastSave, open, onClose }: { world: WorldSummary; lastSave: string | null; open: boolean; onClose: () => void }) {
  const focusJob = useJobFocus();
  const [note, setNote] = useState("");
  const [mode, setMode] = useState<BackupMode>("next_save");
  useEffect(() => {
    if (open) {
      setNote("");
      setMode("next_save");
    }
  }, [open]);
  const create = useMutation({
    mutationFn: () => api.createBackup(world.id, note.trim(), mode),
    onSuccess: (r) => {
      onClose();
      focusJob(r.job_id);
    },
  });
  const running = isRunningState(world.state);
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Back up now"
      actions={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={create.isPending} onClick={() => create.mutate()}>Back up</button>
        </>
      }
    >
      <label className="muted" htmlFor="b-note">Note (optional)</label>
      <input id="b-note" value={note} maxLength={500} placeholder="e.g. Before the raid on the Kindlewastes" onChange={(e) => setNote(e.target.value)} style={{ width: "100%", marginTop: 6 }} />
      {running ? (
        <>
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
            The server writes the world to disk every 5 minutes, when a player logs out, and when it stops. Changes
            made since its last save are only in its memory.
          </p>
          <div className="radio-row">
            <label>
              <input type="radio" checked={mode === "next_save"} onChange={() => setMode("next_save")} />
              <span>
                <b>Wait for the next save</b> <span className="muted">(within 5 minutes; includes recent changes; nobody is disconnected)</span>
              </span>
            </label>
            <label>
              <input type="radio" checked={mode === "stop"} onChange={() => setMode("stop")} />
              <span>
                <b>Stop the server now</b> <span className="muted">(it saves everything; players are disconnected; it starts again after)</span>
              </span>
            </label>
            <label>
              <input type="radio" checked={mode === "last_save"} onChange={() => setMode("last_save")} />
              <span>
                <b>Use the last save</b>{" "}
                <span className="muted">
                  {lastSave ? `(from ${formatTime(lastSave)}, ${since(lastSave)} ago; later changes are not included)` : ""}
                </span>
              </span>
            </label>
          </div>
        </>
      ) : (
        lastSave && <p className="muted" style={{ fontSize: 12 }}>The server is stopped; its last save is from {formatTime(lastSave)}.</p>
      )}
      {create.error && <p className="error-text">{(create.error as Error).message}</p>}
    </Modal>
  );
}

function RestoreDialog({ world, backup, onClose }: { world: WorldSummary; backup: BackupItem; onClose: () => void }) {
  const focusJob = useJobFocus();
  const { data: detail, error: inspectError } = useQuery({
    queryKey: ["backup", world.id, backup.id],
    queryFn: () => api.backup(world.id, backup.id),
  });
  const [restoreConfig, setRestoreConfig] = useState(false);
  const [when, setWhen] = useState<When>("now");
  const [confirm, setConfirm] = useState("");
  const restore = useMutation({
    mutationFn: () => api.restoreBackup(world.id, backup.id, restoreConfig, when),
    onSuccess: (r) => {
      onClose();
      focusJob(r.job_id);
    },
  });
  const running = isRunningState(world.state);
  const players = playersText(world);
  return (
    <Modal
      open
      onClose={onClose}
      title="Restore this backup?"
      wide
      actions={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={confirm !== world.slug || restore.isPending || !detail} onClick={() => restore.mutate()}>
            Restore
          </button>
        </>
      }
    >
      <dl className="kv backup-summary">
        <dt>Backup</dt>
        <dd>
          <span className={`pill ${KIND_TONE[backup.kind]}`}>{KIND_LABEL[backup.kind]}</span> {backup.note ? <b>{backup.note}</b> : <span className="muted">no note</span>}
        </dd>
        <dt>Created</dt><dd>{formatTime(backup.created_at)}</dd>
        <dt>World saved</dt>
        <dd>
          {backup.saved_at ? formatTime(backup.saved_at) : "unknown"}
          {savedEarlier(backup) && <span className="muted"> (changes after this weren't saved yet, so they aren't in this backup)</span>}
        </dd>
        <dt>File</dt><dd className="mono">{backup.filename}</dd>
      </dl>
      <ol className="steps">
        {running && <li>Stop the server{when === "empty" ? " once nobody is online" : ""}.</li>}
        <li>Save the current savegame as a “before restore” backup.</li>
        <li>Replace the savegame with this backup's ({detail ? `${detail.save_files.length} files` : "…"}).</li>
        {restoreConfig && <li>Restore difficulty, server settings and roles from the backup.</li>}
        {(running || world.desired_state === "running") && <li>Start the server.</li>}
      </ol>
      {inspectError && <p className="error-text">{(inspectError as Error).message}</p>}
      {detail?.warnings.map((w) => <p key={w} className="banner warn">{w}</p>)}
      {players && <p className="banner warn">{players}</p>}
      <label className="check" style={{ marginTop: 8 }}>
        <input type="checkbox" checked={restoreConfig} disabled={!detail?.has_config} onChange={(e) => setRestoreConfig(e.target.checked)} />
        <span>
          Also restore settings and roles from the backup{" "}
          <span className="muted">{detail && !detail.has_config ? "(this backup has no config)" : "(port and ban list stay)"}</span>
        </span>
      </label>
      {running && <WhenChoice when={when} setWhen={setWhen} verb="Restore" />}
      <p>
        Type <b className="mono">{world.slug}</b> to confirm:
      </p>
      <input value={confirm} onChange={(e) => setConfirm(e.target.value)} className="mono" style={{ width: "100%" }} aria-label="World slug" autoComplete="off" />
      {restore.error && <p className="error-text">{(restore.error as Error).message}</p>}
    </Modal>
  );
}

function DeleteDialog({ world, backup, onClose }: { world: WorldSummary; backup: BackupItem; onClose: () => void }) {
  const qc = useQueryClient();
  const del = useMutation({
    mutationFn: () => api.deleteBackup(world.id, backup.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["backups", world.id] });
      onClose();
    },
  });
  return (
    <Modal
      open
      onClose={onClose}
      title="Delete this backup?"
      actions={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" style={{ background: "var(--bad)", borderColor: "var(--bad)" }} disabled={del.isPending} onClick={() => del.mutate()}>
            Delete
          </button>
        </>
      }
    >
      <p>
        <span className="mono">{backup.filename}</span> ({formatBytes(backup.size_bytes)}, {KIND_LABEL[backup.kind]}) is deleted from disk. This
        can't be undone.
      </p>
      {del.error && <p className="error-text">{(del.error as Error).message}</p>}
    </Modal>
  );
}

function NoteDialog({ world, backup, onClose }: { world: WorldSummary; backup: BackupItem; onClose: () => void }) {
  const qc = useQueryClient();
  const [note, setNote] = useState(backup.note);
  const save = useMutation({
    mutationFn: () => api.setBackupNote(world.id, backup.id, note.trim()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["backups", world.id] });
      onClose();
    },
  });
  return (
    <Modal
      open
      onClose={onClose}
      title="Edit note"
      actions={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>Save</button>
        </>
      }
    >
      <input value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} style={{ width: "100%" }} aria-label="Note" autoFocus />
      {save.error && <p className="error-text">{(save.error as Error).message}</p>}
    </Modal>
  );
}

function ScheduleCard({ world, schedule, backups }: { world: WorldSummary; schedule: BackupSchedule; backups: BackupItem[] }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState(schedule);
  const [preset, setPreset] = useState(presetFor(schedule.cron));
  useEffect(() => {
    setDraft(schedule);
    setPreset(presetFor(schedule.cron));
  }, [schedule]);
  const dirty =
    draft.cron.trim() !== schedule.cron ||
    draft.enabled !== schedule.enabled ||
    draft.skip_if_idle !== schedule.skip_if_idle ||
    draft.retention_count !== schedule.retention_count ||
    draft.max_age_days !== schedule.max_age_days;
  const save = useMutation({
    mutationFn: () =>
      api.updateBackupSchedule(world.id, {
        cron: draft.cron.trim(),
        enabled: draft.enabled,
        skip_if_idle: draft.skip_if_idle,
        retention_count: draft.retention_count,
        max_age_days: draft.max_age_days,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backups", world.id] }),
  });
  const set = <K extends keyof BackupSchedule>(k: K, v: BackupSchedule[K]) => {
    save.reset();
    setDraft((d) => ({ ...d, [k]: v }));
  };
  const num = (s: string) => (s === "" ? NaN : Number(s));
  const invalid = !Number.isInteger(draft.retention_count) || draft.retention_count < 0 || !Number.isInteger(draft.max_age_days) || draft.max_age_days < 0 || !draft.cron.trim();

  return (
    <div className="card">
      <h3>Schedule &amp; retention</h3>
      <div className="form" style={{ gridTemplateColumns: "120px 1fr" }}>
        <label>Enabled</label>
        <Toggle checked={draft.enabled} onChange={(v) => set("enabled", v)} label="Scheduled backups" />
        <label htmlFor="s-every">Every</label>
        <span>
          <select
            id="s-every"
            value={preset}
            disabled={!draft.enabled}
            onChange={(e) => {
              setPreset(e.target.value);
              if (e.target.value !== "custom") set("cron", e.target.value);
            }}
          >
            {SCHEDULE_PRESETS.map((p) => <option key={p.cron} value={p.cron}>{p.label}</option>)}
            <option value="custom">Custom cron…</option>
          </select>
          {preset === "custom" && (
            <input className="mono" value={draft.cron} onChange={(e) => set("cron", e.target.value)} aria-label="Cron expression" placeholder="m h dom mon dow" style={{ width: 150, marginTop: 6, display: "block" }} />
          )}
        </span>
        <label>Only if played</label>
        <span className="inline">
          <Toggle checked={draft.skip_if_idle} onChange={(v) => set("skip_if_idle", v)} label="Skip when nobody played" disabled={!draft.enabled} />
          <span className="muted">skip when nobody played since the last one</span>
        </span>
        <label htmlFor="s-keep">Keep</label>
        <span>
          <input id="s-keep" type="number" min={0} value={Number.isFinite(draft.retention_count) ? draft.retention_count : ""} onChange={(e) => set("retention_count", num(e.target.value))} style={{ width: 70 }} /> scheduled backups <span className="muted">(0 = all)</span>
        </span>
        <label htmlFor="s-age">Other backups</label>
        <span>
          delete after <input id="s-age" type="number" min={0} value={Number.isFinite(draft.max_age_days) ? draft.max_age_days : ""} onChange={(e) => set("max_age_days", num(e.target.value))} style={{ width: 70 }} /> days <span className="muted">(0 = keep)</span>
        </span>
      </div>
      <div className="top" style={{ margin: "14px 0 0" }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {schedule.enabled && schedule.next_run_at ? `Next: ${formatTime(schedule.next_run_at)}` : "Off"} · {retentionText(backups, schedule.retention_count)}
          {" · "}times in {schedule.timezone}
        </span>
        <span className="spacer" />
        <button className="btn sm" disabled={!dirty || invalid || save.isPending} onClick={() => save.mutate()}>Save schedule</button>
      </div>
      {save.error && <p className="error-text">{(save.error as Error).message}</p>}
      <h3 style={{ marginTop: 18 }}>Automatic</h3>
      <dl className="kv">
        <dt>Before restore</dt><dd>✔</dd>
        <dt>Before game update</dt><dd className="muted">"Back up first" on the Updates page</dd>
        <dt>Before image update</dt><dd className="muted">"Back up first" on the Updates page</dd>
        <dt>Config changes</dt><dd className="muted">kept in config history instead</dd>
      </dl>
    </div>
  );
}

function DiskCard({ disk, worldId }: { disk: DiskInfo; worldId: string }) {
  const qc = useQueryClient();
  const used = disk.total_bytes - disk.free_bytes;
  const pct = disk.total_bytes ? Math.round((used / disk.total_bytes) * 100) : 0;
  const refresh = useMutation({
    mutationFn: () => api.backups(worldId, true),
    onSuccess: (d) => qc.setQueryData(["backups", worldId], d),
  });
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="top" style={{ margin: "0 0 10px" }}>
        <h3 style={{ margin: 0 }}>Disk</h3>
        <span className="spacer" />
        <button className="btn sm" disabled={refresh.isPending} onClick={() => refresh.mutate()}>Refresh</button>
      </div>
      <div className={`bar${disk.warnings.length ? " warn" : ""}`}><i style={{ width: `${pct}%` }} /></div>
      <div className="muted" style={{ fontSize: 12, margin: "6px 0 10px" }}>
        {formatBytes(disk.free_bytes)} free of {formatBytes(disk.total_bytes)}
      </div>
      <dl className="kv">
        <dt>Game files</dt><dd>{formatBytes(disk.game_bytes)}</dd>
        <dt>Savegame</dt><dd>{formatBytes(disk.savegame_bytes)}</dd>
        <dt>Backups</dt><dd>{formatBytes(disk.backups_bytes)}</dd>
        {disk.image_backups_bytes > 0 && (<><dt>Image backups</dt><dd>{formatBytes(disk.image_backups_bytes)}</dd></>)}
      </dl>
    </div>
  );
}
