import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { UpdatePolicyName } from "../api/types";
import { useJobFocus } from "../components/JobsDrawer";
import { Toggle } from "../components/Toggle";
import { formatBytes } from "../format";
import { useWorlds } from "../hooks/live";
import { SCHEDULE_PRESETS } from "../settings/backups";
import { PERMISSIONS, generatePassword, newRole, uniqueName, validateRoles, type Permission, type RoleDraft } from "../settings/roles";
import { useSchema } from "../settings/schema";
import { STEPS, effectiveSlug, initialState, stepErrors, toBody, type WizardState } from "../settings/wizard";

const PRESETS = ["Default", "Relaxed", "Hard", "Survival"] as const;
const PERM_SHORT: Record<Permission, [string, string]> = {
  canKickBan: ["Kick", "Kick and ban players"],
  canAccessInventories: ["Chests", "Open chests and containers"],
  canEditWorld: ["World", "Change the world outside bases"],
  canEditBase: ["Base", "Build and terraform in bases"],
  canExtendBase: ["Altars", "Create, upgrade and remove Flame Altars"],
};
const POLICIES: [UpdatePolicyName, string][] = [
  ["when_empty", "Auto – when nobody is online"],
  ["immediate", "Auto – immediately"],
  ["window", "Auto – maintenance window (04:00)"],
  ["manual", "Manual"],
];

/** The new-world wizard: basics, difficulty, roles, image & schedules, review. */
export function NewWorld() {
  const { data: schema } = useSchema();
  const { data: app } = useQuery({ queryKey: ["app-settings"], queryFn: api.appSettings, staleTime: 60_000 });
  if (!schema || !app) return <p className="muted">Loading…</p>;
  return <Wizard templates={schema.roles.templates} presetDescriptions={schema.presetDescriptions} defaultTag={String(app.values.default_image_tag)} />;
}

function Wizard({ templates, presetDescriptions, defaultTag }: { templates: Record<string, Record<string, boolean | number>>; presetDescriptions: Record<string, string>; defaultTag: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const focusJob = useJobFocus();
  const { data: worlds = [] } = useWorlds();
  const [s, setS] = useState<WizardState>(() => initialState(templates, () => generatePassword("words"), defaultTag));
  const [step, setStep] = useState(0);
  const [tried, setTried] = useState(false);
  const set = (patch: Partial<WizardState>) => setS((x) => ({ ...x, ...patch }));

  const { data: suggested } = useQuery({ queryKey: ["suggest-port"], queryFn: api.suggestPort });
  const portNum = s.port ? Number(s.port) : null;
  const { data: portCheck } = useQuery({
    queryKey: ["check-port", portNum],
    queryFn: () => api.checkPort(portNum!),
    enabled: portNum !== null && portNum >= 1024 && portNum <= 65535,
  });
  const installedWorlds = worlds.filter((w) => w.build_id);
  // Offer copying game files when another world has them (saves a ~9 GB download).
  useEffect(() => {
    if (!s.copyFrom && installedWorlds.length && step === 0) set({ copyFrom: installedWorlds[0].id });
  }, [installedWorlds.length]);

  const errors = stepErrors(s, step, portCheck ? portCheck.free : null);
  const preview = useQuery({
    queryKey: ["create-preview", JSON.stringify(toBody(s))],
    queryFn: () => api.previewWorld(toBody(s)),
    enabled: step === 4,
    retry: false,
  });
  const create = useMutation({
    mutationFn: (start: boolean) => api.createWorld(toBody(s, start)),
    onSuccess: (res, start) => {
      qc.invalidateQueries({ queryKey: ["worlds"] });
      focusJob(res.job_id);
      navigate(start ? `/worlds/${res.world.slug}` : `/worlds/${res.world.slug}/difficulty`);
    },
  });

  const next = () => {
    setTried(true);
    if (!errors.length) {
      setTried(false);
      setStep(step + 1);
    }
  };
  const roleProblems = useMemo(() => validateRoles(s.roles, s.slots), [s.roles, s.slots]);
  const port = s.port || (suggested ? String(suggested.port) : "…");

  return (
    <>
      <div className="top">
        <h1>New world</h1>
      </div>
      <ol className="wizard-steps" aria-label="Steps">
        {STEPS.map((label, i) => (
          <li key={label} className={i < step ? "done" : i === step ? "cur" : ""}>
            <button type="button" disabled={i > step} onClick={() => setStep(i)}>{i < step ? "✔" : i + 1} · {label}</button>
          </li>
        ))}
      </ol>
      <div className="grid g2">
        <div className="card">
          {step === 0 && (
            <div className="form">
              <label htmlFor="w-name">Name</label>
              <input id="w-name" autoFocus value={s.name} maxLength={100} placeholder="e.g. Frostmere" onChange={(e) => set({ name: e.target.value })} />
              <span className="hint">Shown in the in-game server list.</span>
              <label htmlFor="w-slug">Slug</label>
              <input id="w-slug" className="mono" value={effectiveSlug(s)} onChange={(e) => set({ slug: e.target.value, slugEdited: true })} />
              <span className="hint">Folder and container name: flamekeeper-{effectiveSlug(s) || "…"}</span>
              <label htmlFor="w-slots">Max players</label>
              <input id="w-slots" type="number" min={1} max={16} value={Number.isFinite(s.slots) ? s.slots : ""} onChange={(e) => set({ slots: e.target.value === "" ? NaN : Number(e.target.value) })} style={{ width: 90 }} />
              <label htmlFor="w-port">Query port (UDP)</label>
              <span>
                <input id="w-port" value={s.port} placeholder={suggested ? String(suggested.port) : "auto"} onChange={(e) => set({ port: e.target.value.replace(/\D/g, "") })} style={{ width: 110 }} />{" "}
                {s.port ? (
                  portCheck && <span className={portCheck.free ? "ok-text" : "error-text"}>{portCheck.free ? "free ✔" : portCheck.reason}</span>
                ) : (
                  <span className="muted">next free port</span>
                )}
              </span>
              <span className="hint">Players join on this port; forward it on your router for people outside your network.</span>
            </div>
          )}

          {step === 1 && (
            <>
              <div className="preset-cards" role="radiogroup" aria-label="Difficulty preset">
                {PRESETS.map((p) => (
                  <button key={p} type="button" role="radio" aria-checked={s.preset === p} className={`preset-card${s.preset === p ? " on" : ""}`} onClick={() => set({ preset: p })}>
                    <b>{p}</b>
                    <span>{presetDescriptions[p]}</span>
                  </button>
                ))}
              </div>
              <p className="muted" style={{ marginTop: 14, marginBottom: 0 }}>
                Want to change individual settings? At the end you can create the world without starting it and edit
                every game setting first.
              </p>
            </>
          )}

          {step === 2 && <RolesStep s={s} set={set} templates={templates} problems={roleProblems} />}

          {step === 3 && (
            <div className="form">
              <label htmlFor="w-tag">Image tag</label>
              <span>
                <select id="w-tag" value={["latest", "stable-wine"].includes(s.imageTag) ? s.imageTag : "custom"} onChange={(e) => set({ imageTag: e.target.value === "custom" ? "" : e.target.value })}>
                  <option value="latest">latest (Proton): recommended</option>
                  <option value="stable-wine">stable-wine</option>
                  <option value="custom">pinned version…</option>
                </select>
                {!["latest", "stable-wine"].includes(s.imageTag) && (
                  <input className="mono" value={s.imageTag} placeholder="tag" onChange={(e) => set({ imageTag: e.target.value })} style={{ width: 140, marginLeft: 6 }} />
                )}
              </span>
              <label htmlFor="w-branch">Steam branch</label>
              <input id="w-branch" className="mono" value={s.branch} onChange={(e) => set({ branch: e.target.value })} style={{ width: 140 }} />
              <label htmlFor="w-game">Game files</label>
              <select id="w-game" value={s.copyFrom} onChange={(e) => set({ copyFrom: e.target.value })}>
                <option value="">Download from Steam (about 9 GB)</option>
                {installedWorlds.map((w) => (
                  <option key={w.id} value={w.id}>Copy from {w.display_name} (build {w.build_id})</option>
                ))}
              </select>
              <label htmlFor="w-upd">Game updates</label>
              <select id="w-upd" value={s.updatePolicy} onChange={(e) => set({ updatePolicy: e.target.value as UpdatePolicyName })}>
                {POLICIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <label htmlFor="w-bak">Backups</label>
              <span>
                <select id="w-bak" value={s.backupCron} onChange={(e) => set({ backupCron: e.target.value })}>
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

          {step === 4 && (
            <>
              {preview.isLoading && <p className="muted">Checking…</p>}
              {preview.error && <div className="banner bad">{(preview.error as Error).message}</div>}
              {preview.data && (
                <>
                  {preview.data.capacity.warnings.map((w) => <div key={w} className="banner warn">⚠ {w} You can continue.</div>)}
                  <p className="muted" style={{ marginTop: 0 }}>The server config FlameKeeper will write (passwords hidden):</p>
                  <pre className="json-preview">{JSON.stringify(preview.data.document, null, 2)}</pre>
                  <p className="muted" style={{ marginBottom: 0 }}>
                    <b>Create &amp; start</b> starts the server with these settings. <b>Edit game settings first</b>{" "}
                    creates the world stopped and opens its Difficulty settings; start it when you're ready.
                  </p>
                </>
              )}
              {create.error && <div className="banner bad">{(create.error as Error).message}</div>}
            </>
          )}

          {tried && errors.length > 0 && <ul className="error-text">{errors.map((e) => <li key={e}>{e}</li>)}</ul>}
          <div className="top" style={{ margin: "18px 0 0" }}>
            {step > 0 && <button className="btn" onClick={() => setStep(step - 1)}>← Back</button>}
            <span className="spacer" />
            {step < 4 ? (
              <button className="btn primary" onClick={next}>Next: {STEPS[step + 1]} →</button>
            ) : (
              <>
                <button className="btn" disabled={!preview.data || create.isPending} onClick={() => create.mutate(false)}>
                  Edit game settings first
                </button>
                <button className="btn primary" disabled={!preview.data || create.isPending} onClick={() => create.mutate(true)}>
                  {create.isPending ? "Creating…" : "Create & start"}
                </button>
              </>
            )}
          </div>
        </div>

        <div className="card">
          <h3>So far</h3>
          <dl className="kv">
            <dt>Name</dt><dd>{s.name || "—"}</dd>
            <dt>Slug</dt><dd className="mono">{effectiveSlug(s) || "—"}</dd>
            <dt>Port</dt><dd className="mono">{port}/udp{portCheck?.free ? " (free ✔)" : ""}</dd>
            <dt>Max players</dt><dd>{Number.isFinite(s.slots) ? s.slots : "—"}</dd>
            {step >= 1 && (<><dt>Preset</dt><dd>{s.preset}</dd></>)}
            {step >= 2 && (<><dt>Roles</dt><dd>{s.roles.map((r) => r.name).join(", ") || "—"}</dd></>)}
            {step >= 3 && (
              <>
                <dt>Image</dt><dd className="mono">{s.imageTag || "?"} · {s.branch}</dd>
                <dt>Game files</dt><dd>{s.copyFrom ? `copy from ${worlds.find((w) => w.id === s.copyFrom)?.display_name}` : "download (~9 GB)"}</dd>
                <dt>Updates</dt><dd>{POLICIES.find(([v]) => v === s.updatePolicy)?.[1]}</dd>
                <dt>Backups</dt><dd>{s.backupCron ? `${SCHEDULE_PRESETS.find((p) => p.cron === s.backupCron)?.label}, keep ${s.keep}` : "off"}</dd>
              </>
            )}
          </dl>
          <CapacityNote />
        </div>
      </div>
    </>
  );
}

function CapacityNote() {
  const { data } = useQuery({ queryKey: ["capacity", 1], queryFn: () => api.capacity(1) });
  if (!data) return null;
  return (
    <p className="muted" style={{ fontSize: 12, marginTop: 14 }}>
      With this world: {data.worlds} world{data.worlds === 1 ? "" : "s"} on {formatBytes(data.mem_total)} RAM and {data.cpus} cores ·{" "}
      {formatBytes(data.disk_free)} free disk. Keen recommends 16 GB RAM, 6 cores and 30 GB disk per server.
    </p>
  );
}

function RolesStep({ s, set, templates, problems }: { s: WizardState; set: (p: Partial<WizardState>) => void; templates: Record<string, Record<string, boolean | number>>; problems: ReturnType<typeof validateRoles> }) {
  const update = (id: string, patch: Partial<RoleDraft>) => set({ roles: s.roles.map((r) => (r.id === id ? { ...r, ...patch } : r)) });
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        Players pick a role by the password they enter. These are the server's four default roles with new passwords;
        change anything now or later.
      </p>
      <div className="dg-scroll">
        <table className="roles wizard-roles">
          <thead>
            <tr>
              <th>Role</th><th>Password</th>
              {PERMISSIONS.map((p) => <th key={p} className="perm" title={PERM_SHORT[p][1]}>{PERM_SHORT[p][0]}</th>)}
              <th><span className="sr-only">Remove</span></th>
            </tr>
          </thead>
          <tbody>
            {s.roles.map((r) => {
              const f = problems.fields[r.id] ?? {};
              return (
                <tr key={r.id}>
                  <td>
                    <input value={r.name} onChange={(e) => update(r.id, { name: e.target.value })} aria-label="Role name" aria-invalid={Boolean(f.name)} style={{ width: 100 }} />
                    {f.name && <div className="field-error">{f.name}</div>}
                  </td>
                  <td className="pw">
                    <input className="mono" value={r.password ?? ""} placeholder="empty = open" onChange={(e) => update(r.id, { password: e.target.value })} aria-label={`${r.name} password`} aria-invalid={Boolean(f.password)} style={{ width: 200 }} />{" "}
                    <button className="btn sm" title="New password" aria-label={`New password for ${r.name}`} onClick={() => update(r.id, { password: generatePassword("words") })}>↻</button>
                    {f.password && <div className="field-error">{f.password}</div>}
                  </td>
                  {PERMISSIONS.map((p) => (
                    <td key={p} className="perm"><Toggle checked={r[p]} onChange={(v) => update(r.id, { [p]: v })} label={`${r.name}: ${PERM_SHORT[p][1]}`} /></td>
                  ))}
                  <td><button className="btn sm danger" aria-label={`Remove ${r.name}`} disabled={s.roles.length === 1} onClick={() => set({ roles: s.roles.filter((x) => x.id !== r.id) })}>✕</button></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {problems.warnings.map((w) => <p key={w} className="field-warn">{w}</p>)}
      {problems.general.map((g) => <p key={g} className="error-text">{g}</p>)}
      <div className="top" style={{ margin: "10px 0 0" }}>
        {Object.keys(templates).map((t) => (
          <button key={t} className="btn sm" onClick={() => set({ roles: [...s.roles, newRole(uniqueName(t, s.roles), templates[t], generatePassword("words"))] })}>＋ {t}</button>
        ))}
      </div>
    </>
  );
}
