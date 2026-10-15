import { useMutation } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ConfigView, FieldProblem, WorldSummary } from "../api/types";
import { copyText } from "../clipboard";
import { ConfigBanners, RestartDialog, UnsavedDialog } from "../components/ConfigBanners";
import { ConfigHistory } from "../components/ConfigHistory";
import { useJobFocus } from "../components/JobsDrawer";
import { Toggle } from "../components/Toggle";
import type { When } from "../components/WorldActions";
import { isRunningState } from "../format";
import { saveError, useConfig, useConfigSaved, useUnsavedGuard } from "../hooks/config";
import {
  PERMISSIONS,
  fromView,
  generatePassword,
  hasErrors,
  isOpen,
  joinInfo,
  newRole,
  rolesDirty,
  toPayload,
  uniqueName,
  validateRoles,
  type Permission,
  type RoleDraft,
} from "../settings/roles";
import { useSchema, type SettingsSchema } from "../settings/schema";
import { NotFound } from "./NotFound";
import { useWorldBySlug } from "./WorldOverview";

const PERM_LABEL: Record<Permission, [string, string]> = {
  canKickBan: ["Kick / ban", "Kick and ban players"],
  canAccessInventories: ["Inventories", "Open chests and containers"],
  canEditWorld: ["Edit world", "Change the world outside bases"],
  canEditBase: ["Edit base", "Add, remove and terraform blocks and props in bases"],
  canExtendBase: ["Extend base", "Create, upgrade and remove Flame Altars"],
};

export function WorldRoles() {
  const { world, loading } = useWorldBySlug();
  const { data: schema } = useSchema();
  const { data: config, error } = useConfig(world?.id);
  if (loading) return <p className="muted">Loading…</p>;
  if (!world) return <NotFound what="World" />;
  if (error) return <p className="error-text">Could not read the config: {(error as Error).message}</p>;
  if (!schema || !config) return <p className="muted">Loading…</p>;
  return <RolesEditor key={world.id} world={world} schema={schema} config={config} />;
}

type GenKind = "words" | "chars";

function RolesEditor({ world, schema, config }: { world: WorldSummary; schema: SettingsSchema; config: ConfigView }) {
  const saved = useConfigSaved(world.id);
  const focusJob = useJobFocus();
  const [drafts, setDrafts] = useState<RoleDraft[]>(() => fromView(config.roles));
  const [baseRevision, setBaseRevision] = useState(config.revision);
  const [visible, setVisible] = useState<Set<string>>(new Set());
  const [genKind, setGenKind] = useState<GenKind>("words");
  const [serverErrors, setServerErrors] = useState<FieldProblem[]>([]);
  const [dialog, setDialog] = useState<null | "history" | "restart">(null);
  const [addOpen, setAddOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [joinRole, setJoinRole] = useState<string>("");

  const slotCount = config.server.slotCount;
  const dirty = rolesDirty(drafts, config.roles);
  const blocker = useUnsavedGuard(dirty);
  const problems = useMemo(() => validateRoles(drafts, slotCount), [drafts, slotCount]);
  const reserved = drafts.reduce((n, d) => n + (Number.isInteger(d.reservedSlots) ? d.reservedSlots : 0), 0);

  useEffect(() => {
    if (config.revision !== baseRevision && !dirty) {
      setDrafts(fromView(config.roles));
      setBaseRevision(config.revision);
    }
  }, [config, baseRevision, dirty]);

  const serverError = (i: number, field: string) =>
    serverErrors.find((e) => e.loc[0] === "roles" && e.loc[1] === i && e.loc[2] === field)?.msg;
  const generalServerErrors = serverErrors.filter((e) => e.loc[0] === "roles" && e.loc.length === 1);

  const update = (id: string, patch: Partial<RoleDraft>) => {
    setDrafts((ds) => ds.map((d) => (d.id === id ? { ...d, ...patch } : d)));
    setNotice(null);
    setServerErrors([]);
  };
  const move = (i: number, delta: number) =>
    setDrafts((ds) => {
      const j = i + delta;
      if (j < 0 || j >= ds.length) return ds;
      const out = [...ds];
      [out[i], out[j]] = [out[j], out[i]];
      return out;
    });

  const reveal = useMutation({
    mutationFn: (d: RoleDraft) => api.revealPassword(world.id, d.source!).then((r) => ({ d, password: r.password })),
    onSuccess: ({ d, password }) => {
      update(d.id, { password, savedPassword: password });
      setVisible((v) => new Set(v).add(d.id));
    },
  });

  /** The role's password, revealing the saved one if needed. */
  const passwordOf = async (d: RoleDraft): Promise<string> => {
    if (d.password !== undefined) return d.password;
    if (d.source === null) return "";
    const r = await api.revealPassword(world.id, d.source);
    update(d.id, { password: r.password, savedPassword: r.password });
    return r.password;
  };

  const copy = async (text: string, what: string) => {
    setNotice((await copyText(text)) ? `Copied ${what}.` : `Couldn't copy ${what}; select and copy it by hand.`);
  };

  const save = useMutation({
    mutationFn: () => api.saveConfig(world.id, { base_revision: baseRevision, roles: toPayload(drafts) }),
    onSuccess: (res) => {
      setServerErrors([]);
      setDrafts(fromView(res.config.roles));
      setVisible(new Set());
      setBaseRevision(res.config.revision);
      saved(res.config);
      setNotice(
        !res.changes.length
          ? "Nothing changed."
          : "Saved." + (isRunningState(world.state) ? "" : " It applies when the world starts."),
      );
    },
    onError: (e) => setServerErrors(saveError(e).fields),
  });
  const saveAndRestart = useMutation({
    mutationFn: async (when: When) => {
      if (dirty) await save.mutateAsync();
      return api.applyConfig(world.id, when);
    },
    onSuccess: (r) => {
      setDialog(null);
      focusJob(r.job_id);
    },
  });
  const legacy = useMutation({
    mutationFn: () => api.convertLegacyPassword(world.id, baseRevision),
    onSuccess: (res) => {
      setDrafts(fromView(res.config.roles));
      setBaseRevision(res.config.revision);
      saved(res.config);
      setNotice("The legacy password is now the role “default”. Restart to apply.");
    },
  });
  const err = save.error ? saveError(save.error) : null;

  const revert = () => {
    save.reset();
    setServerErrors([]);
    setDrafts(fromView(config.roles));
    setVisible(new Set());
    setBaseRevision(config.revision);
    saved();
  };

  const addRole = (template: string | null) => {
    const name = uniqueName(template ?? "New role", drafts);
    const t = template ? schema.roles.templates[template] : undefined;
    const d = newRole(name, t, generatePassword(genKind));
    setDrafts((ds) => [...ds, d]);
    setVisible((v) => new Set(v).add(d.id));
    setAddOpen(false);
  };

  const running = isRunningState(world.state);
  const invalid = hasErrors(problems);
  const joinDraft = drafts.find((d) => d.id === joinRole) ?? drafts.find((d) => d.name === "Friend") ?? drafts[0];

  return (
    <>
      <div className="top">
        <h1>Roles &amp; passwords</h1>
        <span className="muted">Players get a role by the password they enter when joining.</span>
        <span className="spacer" />
        <div className="menu">
          <button className="btn" aria-expanded={addOpen} onClick={() => setAddOpen(!addOpen)}>＋ Add role ▾</button>
          {addOpen && (
            <div className="menu-list" role="menu">
              {Object.keys(schema.roles.templates).map((t) => (
                <button key={t} role="menuitem" onClick={() => addRole(t)}>Like {t}</button>
              ))}
              <button role="menuitem" onClick={() => addRole(null)}>Blank (no permissions)</button>
            </div>
          )}
        </div>
        <button className="btn" onClick={() => setDialog("history")}>History</button>
        <button className="btn" disabled={!dirty} onClick={revert}>Revert</button>
        <button className="btn" disabled={!dirty || invalid || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {running && (
          <button
            className="btn primary"
            disabled={invalid || save.isPending || world.busy || (!dirty && !world.pending_restart)}
            onClick={() => setDialog("restart")}
          >
            Save &amp; restart
          </button>
        )}
      </div>

      <ConfigBanners world={world} offerStart />
      {config.legacy_password && (
        <div className="banner warn">
          <span>
            This world's file has a legacy top-level <span className="mono">password</span>. The server turns it into a
            role named “default”. Convert it to a normal role you can edit here?
          </span>
          <span className="spacer" />
          <button className="btn sm" disabled={legacy.isPending || dirty} title={dirty ? "Save or revert your edits first" : undefined} onClick={() => legacy.mutate()}>
            Convert to role
          </button>
        </div>
      )}
      {err && (
        <div className="banner bad" role="alert">
          <span>{err.message}</span>
          <span className="spacer" />
          {err.stale && <button className="btn sm" onClick={revert}>Reload (discards your edits)</button>}
        </div>
      )}
      {notice && !err && <div className="banner info" role="status">{notice}</div>}

      <div className="card">
        <div className="dg-scroll">
          <table className="roles">
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Role</th>
                <th scope="col">Password</th>
                {PERMISSIONS.map((p) => (
                  <th key={p} scope="col" title={PERM_LABEL[p][1]} className="perm">{PERM_LABEL[p][0]}</th>
                ))}
                <th scope="col" title="Slots held open for this role; the server looks full to other roles">Reserved</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {drafts.map((d, i) => {
                const f = problems.fields[d.id] ?? {};
                const nameErr = f.name ?? serverError(i, "name");
                const pwErr = f.password ?? serverError(i, "password");
                const slotErr = f.reservedSlots ?? serverError(i, "reservedSlots");
                const open = isOpen(d);
                const shown = visible.has(d.id);
                return (
                  <tr key={d.id}>
                    <td className="muted order">
                      <button className="btn sm" aria-label={`Move ${d.name} up`} disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                      <button className="btn sm" aria-label={`Move ${d.name} down`} disabled={i === drafts.length - 1} onClick={() => move(i, 1)}>↓</button>
                    </td>
                    <td>
                      <input value={d.name} onChange={(e) => update(d.id, { name: e.target.value })} aria-label={`Role ${i + 1} name`} aria-invalid={Boolean(nameErr)} style={{ width: 130 }} />
                      {nameErr && <div className="field-error">{nameErr}</div>}
                    </td>
                    <td className="pw">
                      {d.password === undefined && open ? (
                        <>
                          <span className="pill warn">Open: anyone can join as {d.name || "this role"}</span>{" "}
                          <button className="btn sm" onClick={() => { update(d.id, { password: generatePassword(genKind) }); setVisible((v) => new Set(v).add(d.id)); }}>Set password</button>
                        </>
                      ) : d.password === undefined ? (
                        <>
                          <span className="mono masked">••••••••••</span>{" "}
                          <button className="btn sm" disabled={reveal.isPending} onClick={() => reveal.mutate(d)}>Show</button>{" "}
                          <button className="btn sm" onClick={async () => copy(await passwordOf(d), `${d.name}'s password`)}>Copy</button>{" "}
                          <button className="btn sm" title="Generate a new password" aria-label={`New password for ${d.name}`} onClick={() => { update(d.id, { password: generatePassword(genKind) }); setVisible((v) => new Set(v).add(d.id)); }}>↻</button>
                        </>
                      ) : (
                        <>
                          <input
                            className="mono"
                            type={shown ? "text" : "password"}
                            value={d.password}
                            placeholder="empty = open role"
                            onChange={(e) => update(d.id, { password: e.target.value })}
                            aria-label={`${d.name} password`}
                            aria-invalid={Boolean(pwErr)}
                            autoComplete="off"
                            style={{ width: 210 }}
                          />{" "}
                          <button className="btn sm" onClick={() => setVisible((v) => { const n = new Set(v); if (shown) n.delete(d.id); else n.add(d.id); return n; })}>
                            {shown ? "Hide" : "Show"}
                          </button>{" "}
                          <button className="btn sm" disabled={!d.password} onClick={() => copy(d.password ?? "", `${d.name}'s password`)}>Copy</button>{" "}
                          <button className="btn sm" title="Generate a new password" aria-label={`New password for ${d.name}`} onClick={() => update(d.id, { password: generatePassword(genKind) })}>↻</button>
                          {open && <div className="field-warn">Open: anyone can join as {d.name || "this role"}</div>}
                        </>
                      )}
                      {pwErr && <div className="field-error">{pwErr}</div>}
                    </td>
                    {PERMISSIONS.map((p) => (
                      <td key={p} className="perm">
                        <Toggle checked={d[p]} onChange={(v) => update(d.id, { [p]: v })} label={`${d.name}: ${PERM_LABEL[p][0]}`} />
                      </td>
                    ))}
                    <td>
                      <input type="number" min={0} max={16} value={Number.isFinite(d.reservedSlots) ? d.reservedSlots : ""} onChange={(e) => update(d.id, { reservedSlots: e.target.value === "" ? NaN : Number(e.target.value) })} aria-label={`${d.name} reserved slots`} aria-invalid={Boolean(slotErr)} style={{ width: 60 }} />
                      {slotErr && <div className="field-error">{slotErr}</div>}
                    </td>
                    <td>
                      <button className="btn sm danger" aria-label={`Delete role ${d.name}`} disabled={drafts.length === 1} onClick={() => setDrafts((ds) => ds.filter((x) => x.id !== d.id))}>✕</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {[...problems.general, ...generalServerErrors.map((e) => e.msg)].map((m) => (
          <p key={m} className="error-text">{m}</p>
        ))}
        <div className="top" style={{ margin: "12px 0 0" }}>
          <span className="muted">
            Reserved slots: {reserved} of {slotCount} · Hover a column for what it allows.
          </span>
          <span className="spacer" />
          <label className="muted">
            New passwords:{" "}
            <select value={genKind} onChange={(e) => setGenKind(e.target.value as GenKind)}>
              <option value="words">4 words</option>
              <option value="chars">16 characters</option>
            </select>
          </label>
          {joinDraft && (
            <span className="join">
              <select value={joinDraft.id} onChange={(e) => setJoinRole(e.target.value)} aria-label="Role for join info">
                {drafts.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
              <button
                className="btn sm"
                title="Server name, address and this role's password, to paste to friends"
                onClick={async () => copy(joinInfo(config.server.name, window.location.hostname, world.query_port, joinDraft.name, await passwordOf(joinDraft)), "join info")}
              >
                Copy join info
              </button>
            </span>
          )}
        </div>
      </div>
      <div className="note">
        Names must be unique; so must passwords (players join by password). At most one role can be open (no
        password). Reserved slots can't add up to more than max players. Role changes apply when the server restarts.
        The address in the join info is the one you opened FlameKeeper on; players outside your network need your
        public IP and a forwarded UDP port.
      </div>

      <ConfigHistory worldId={world.id} config={config} schema={schema} open={dialog === "history"} onClose={() => setDialog(null)} dirty={dirty} />
      <RestartDialog
        world={world}
        open={dialog === "restart"}
        title={dirty ? "Save and restart?" : "Restart to apply changes?"}
        onClose={() => setDialog(null)}
        pending={saveAndRestart.isPending}
        error={saveAndRestart.error ? saveError(saveAndRestart.error).message : null}
        onConfirm={(when) => saveAndRestart.mutate(when)}
      />
      <UnsavedDialog blocker={blocker} />
    </>
  );
}
