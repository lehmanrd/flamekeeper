import { useMutation } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ConfigView, FieldProblem, Preset, SettingValue, WorldSummary } from "../api/types";
import { ConfigBanners, RestartDialog, UnsavedDialog } from "../components/ConfigBanners";
import { ConfigHistory } from "../components/ConfigHistory";
import { ImportExport } from "../components/ImportExport";
import { useJobFocus } from "../components/JobsDrawer";
import { Modal } from "../components/Modal";
import { Toggle } from "../components/Toggle";
import type { When } from "../components/WorldActions";
import { isRunningState } from "../format";
import { saveError, useConfig, useConfigSaved, useUnsavedGuard } from "../hooks/config";
import { useWorlds } from "../hooks/live";
import {
  NAMED_PRESETS,
  clamp,
  customize,
  dependencyOff,
  difficultyPending,
  draftChanges,
  editCustom,
  formatValue,
  matchingPreset,
  pendingState,
  fromInput,
  inputStep,
  presetValue,
  rangeText,
  toInput,
  validateValue,
  type DifficultyDraft,
  type PendingState,
} from "../settings/difficulty";
import { useSchema, type SettingDef, type SettingsSchema } from "../settings/schema";
import { NotFound } from "./NotFound";
import { useWorldBySlug } from "./WorldOverview";

export function WorldDifficulty() {
  const { world, loading } = useWorldBySlug();
  const { data: schema } = useSchema();
  const { data: config, error } = useConfig(world?.id);
  if (loading) return <p className="muted">Loading…</p>;
  if (!world) return <NotFound what="World" />;
  if (error) return <p className="error-text">Could not read the config: {(error as Error).message}</p>;
  if (!schema || !config) return <p className="muted">Loading…</p>;
  return <DifficultyEditor key={world.id} world={world} schema={schema} config={config} />;
}

const fromConfig = (c: ConfigView): DifficultyDraft => ({ preset: c.preset, custom: { ...c.gameSettings } });

/** Gold: changed but not saved. Blue: saved, waiting for a restart to reach the server. */
function PendingDot({ state, running, inline = false }: { state: PendingState; running: string; inline?: boolean }) {
  if (!state) return null;
  const text = state === "unsaved" ? "Changed, not saved yet" : `Saved, applies when the server restarts (running ${running})`;
  return <span className={`dot ${state}${inline ? " inline" : ""}`} title={text} aria-label={text} />;
}

function isTyping(el: Element | null): boolean {
  return Boolean(el && (["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) || el.closest("dialog[open]")));
}

function DifficultyEditor({ world, schema, config }: { world: WorldSummary; schema: SettingsSchema; config: ConfigView }) {
  const saved = useConfigSaved(world.id);
  const focusJob = useJobFocus();
  const [draft, setDraft] = useState<DifficultyDraft>(() => fromConfig(config));
  const [baseRevision, setBaseRevision] = useState(config.revision);
  const [view, setView] = useState<"presets" | "custom">("presets");
  const [hovered, setHovered] = useState<Preset | null>(null);
  const [tab, setTab] = useState(schema.tabs[0].id);
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [serverErrors, setServerErrors] = useState<FieldProblem[]>([]);
  const [dialog, setDialog] = useState<null | "history" | "io" | "copy" | "restart" | "baseline" | "details">(null);
  const [notice, setNotice] = useState<string | null>(null);

  const baseline = useMemo(() => fromConfig(config), [config]);
  const changes = draftChanges(baseline, draft);
  const dirty = changes > 0;
  const blocker = useUnsavedGuard(dirty);

  // A save elsewhere (history restore, import, another tab): follow it unless editing.
  useEffect(() => {
    if (config.revision !== baseRevision && !dirty) {
      setDraft(fromConfig(config));
      setBaseRevision(config.revision);
    }
  }, [config, baseRevision, dirty]);

  const byKey = useMemo(() => Object.fromEntries(schema.settings.map((s) => [s.key, s])), [schema]);
  const rows = schema.settings.filter((s) => s.tab === tab);
  const focused = byKey[focusKey ?? ""] ?? rows[0];

  const clientErrors = useMemo(() => {
    const out: Record<string, string> = {};
    for (const s of schema.settings) {
      const msg = validateValue(s, draft.custom[s.key]);
      if (msg) out[s.key] = msg;
    }
    return out;
  }, [schema, draft]);
  const errorFor = (key: string) =>
    clientErrors[key] ?? serverErrors.find((e) => e.loc[0] === "gameSettings" && e.loc[1] === key)?.msg;
  const invalidCount = Object.keys(clientErrors).length;

  const save = useMutation({
    mutationFn: () =>
      api.saveConfig(world.id, { base_revision: baseRevision, preset: draft.preset, gameSettings: draft.custom }),
    onSuccess: (res) => {
      setServerErrors([]);
      setBaseRevision(res.config.revision);
      setDraft(fromConfig(res.config));
      saved(res.config);
      setNotice(
        !res.changes.length
          ? "Nothing changed."
          : "Saved." +
              (isRunningState(world.state) ? "" : " It applies when the world starts."),
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
  const err = save.error ? saveError(save.error) : null;

  const reload = () => {
    save.reset();
    setServerErrors([]);
    setDraft(fromConfig(config));
    setBaseRevision(config.revision);
    saved();
  };

  const setValue = useCallback((key: string, value: SettingValue) => {
    setDraft((d) => editCustom(d, key, value));
    setFocusKey(key);
    setNotice(null);
  }, []);

  const running = isRunningState(world.state);
  const presetPending = difficultyPending(draft, baseline, { preset: config.applied.preset, custom: config.applied.gameSettings });
  // Offer "my previous custom values" as a baseline when they aren't just a preset's values.
  const hasOwnCustomValues = matchingPreset(schema.settings, draft.custom) === null;
  const presetDescription = (p: Preset) => (p === "Custom" ? CUSTOM_DESCRIPTION : schema.presetDescriptions[p]);
  const pickPreset = (p: Preset) => {
    setNotice(null);
    if (p !== "Custom") setDraft((d) => ({ ...d, preset: p }));
    else if (draft.preset === "Custom") setView("custom");
    else setDialog("baseline");
  };

  return (
    <>
      <div className="top">
        <h1>Difficulty</h1>
        <span className="muted">
          {world.display_name}
          {dirty && (
            <>
              {" "}· <b style={{ color: "var(--gold2)" }}>{changes} unsaved change{changes === 1 ? "" : "s"}</b>
            </>
          )}
        </span>
        <span className="spacer" />
        <button className="btn" onClick={() => setDialog("history")}>History</button>
        <button className="btn" onClick={() => setDialog("io")}>Import / Export</button>
        <button className="btn" onClick={() => setDialog("copy")}>Copy from world…</button>
        <button className="btn" disabled={!dirty} onClick={reload}>Revert</button>
        <button className="btn" disabled={!dirty || invalidCount > 0 || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {running && (
          <button
            className="btn primary"
            disabled={invalidCount > 0 || save.isPending || world.busy || (!dirty && !world.pending_restart)}
            onClick={() => setDialog("restart")}
          >
            Save &amp; restart
          </button>
        )}
      </div>

      <ConfigBanners world={world} offerStart />
      {err && (
        <div className="banner bad" role="alert">
          <span>{err.message}</span>
          <span className="spacer" />
          {err.stale && <button className="btn sm" onClick={reload}>Reload (discards your edits)</button>}
        </div>
      )}
      {notice && !err && (
        <div className="banner info" role="status">
          {notice}
        </div>
      )}
      {config.unknown_game_settings.length > 0 && (
        <div className="note" style={{ marginTop: 0, marginBottom: 14 }}>
          The file also has settings FlameKeeper doesn't know ({config.unknown_game_settings.join(", ")}). They are
          kept, but the server drops keys it doesn't recognise when it starts.
        </div>
      )}

      {view === "presets" ? (
        <div className="preset-pick">
          <div className="preset-stack" role="radiogroup" aria-label="Difficulty preset" onMouseLeave={() => setHovered(null)}>
            {[...NAMED_PRESETS, "Custom" as Preset].map((p) => (
              <button
                key={p}
                type="button"
                role="radio"
                aria-checked={p === draft.preset}
                className={`preset-btn${p === draft.preset ? " on" : ""}${p === "Custom" ? " custom" : ""}`}
                onMouseEnter={() => setHovered(p)}
                onFocus={() => setHovered(p)}
                onClick={() => pickPreset(p)}
              >
                <span className="name">{p}</span>
                {p === draft.preset && <PendingDot state={presetPending} inline running={config.applied.preset} />}
                {p === config.applied.preset && p !== draft.preset && <span className="running">running now</span>}
                {p === draft.preset && <span className="check" aria-hidden>✔</span>}
              </button>
            ))}
            <button type="button" className="btn details-btn" onClick={() => setDialog("details")}>
              Details: compare all presets
            </button>
          </div>
          <PresetInfo
            preset={hovered ?? draft.preset}
            selected={(hovered ?? draft.preset) === draft.preset}
            description={presetDescription(hovered ?? draft.preset)}
            onEdit={() => setView("custom")}
          />
        </div>
      ) : (
        <>
          <div className="top" style={{ margin: "0 0 10px" }}>
            <button className="btn sm" onClick={() => setView("presets")}>← Presets</button>
            <span className="muted">Custom difficulty: set each value yourself.</span>
            <span className="spacer" />
            <button className="btn sm" onClick={() => setDialog("baseline")}>Start over from a preset…</button>
          </div>
          <div className="gtabs" role="tablist" aria-label="Difficulty tabs">
            {schema.tabs.map((t) => (
              <button
                key={t.id}
                role="tab"
                aria-selected={t.id === tab}
                className={t.id === tab ? "active" : ""}
                onClick={() => {
                  setTab(t.id);
                  setFocusKey(null);
                }}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="diff-wrap">
            <div className="card dg-card">
              <div className="dg-scroll">
                <table className="dg dg-custom">
                  <tbody>
                    {rows.map((s) => {
                      const v = draft.custom[s.key];
                      const off = dependencyOff(s, "Custom", draft.custom, byKey);
                      return (
                        <tr
                          key={s.key}
                          className={s.key === focused?.key ? "focus" : ""}
                          onMouseEnter={() => !isTyping(document.activeElement) && setFocusKey(s.key)}
                          onClick={() => setFocusKey(s.key)}
                        >
                          <th scope="row">{s.label}</th>
                          <td className="cust">
                            {off ? (
                              <span className="muted" title={`Off while ${byKey[s.dependsOn!.key]?.label} is off; the value is kept`}>—</span>
                            ) : (
                              <CustomEditor setting={s} value={v} error={errorFor(s.key)} onChange={(nv) => setValue(s.key, nv)} onFocus={() => setFocusKey(s.key)} />
                            )}
                            <PendingDot
                              state={pendingState(v, config.gameSettings[s.key], config.applied.gameSettings[s.key])}
                              running={formatValue(s, config.applied.gameSettings[s.key] ?? s.default)}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="legend">
                <span className="dot static unsaved" /> changed, not saved yet
                <span className="dot static restart" /> saved, applies when the server restarts
              </div>
            </div>
            {focused && (
              <Description
                setting={focused}
                value={draft.custom[focused.key]}
                disabled={dependencyOff(focused, "Custom", draft.custom, byKey)}
                error={errorFor(focused.key)}
                onChange={(nv) => setValue(focused.key, nv)}
              />
            )}
          </div>
        </>
      )}

      <BaselineDialog
        open={dialog === "baseline"}
        current={draft.preset}
        previousCustom={hasOwnCustomValues}
        onClose={() => setDialog(null)}
        onPick={(baseline) => {
          setDraft((d) => customize(schema.settings, d, baseline));
          setNotice(null);
          setDialog(null);
          setView("custom");
        }}
      />
      <PresetDetails schema={schema} byKey={byKey} active={draft.preset} open={dialog === "details"} onClose={() => setDialog(null)} />
      <ConfigHistory worldId={world.id} config={config} schema={schema} open={dialog === "history"} onClose={() => setDialog(null)} dirty={dirty} />
      <ImportExport worldId={world.id} slug={world.slug} config={config} schema={schema} open={dialog === "io"} onClose={() => setDialog(null)} dirty={dirty} />
      <CopyFromWorld
        world={world}
        open={dialog === "copy"}
        onClose={() => setDialog(null)}
        onCopy={(from, name) => {
          setDraft((d) => ({ preset: from.preset, custom: { ...d.custom, ...from.gameSettings } }));
          setDialog(null);
          setNotice(`Loaded the difficulty from ${name}. Review it, then Save.`);
        }}
      />
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

const CUSTOM_DESCRIPTION =
  "Set your own value for every difficulty option: enemies, player, survival and resources. " +
  "You pick a preset to start from, then adjust whatever you like.";

/** The right-hand panel on the presets screen: the hovered (or selected) preset. */
function PresetInfo({ preset, selected, description, onEdit }: { preset: Preset; selected: boolean; description: string; onEdit: () => void }) {
  return (
    <aside className="card preset-info" aria-live="polite">
      <h2>{preset}</h2>
      <p>{description}</p>
      {preset === "Custom" && selected && (
        <button className="btn primary" onClick={onEdit}>Edit custom values</button>
      )}
    </aside>
  );
}

/** Choosing Custom: which preset's values to start from. */
function BaselineDialog({
  open,
  current,
  previousCustom,
  onClose,
  onPick,
}: {
  open: boolean;
  current: Preset;
  previousCustom: boolean;
  onClose: () => void;
  onPick: (p: Preset) => void;
}) {
  return (
    <Modal open={open} onClose={onClose} title="Start custom difficulty from…" actions={<button className="btn" onClick={onClose}>Cancel</button>}>
      <p className="muted" style={{ marginTop: 0 }}>
        Every setting starts with this preset's value; then you adjust the ones you want.
      </p>
      <div className="baseline-list">
        {NAMED_PRESETS.map((p) => (
          <button key={p} className="btn" onClick={() => onPick(p)}>
            {p}
            {p === current && <span className="muted"> (current)</span>}
          </button>
        ))}
        {previousCustom && (
          <button className="btn" onClick={() => onPick("Custom")}>
            My previous custom values
          </button>
        )}
      </div>
    </Modal>
  );
}

/** Details: every setting across the four built-in presets, for comparing them. */
function PresetDetails({
  schema,
  byKey,
  active,
  open,
  onClose,
}: {
  schema: SettingsSchema;
  byKey: Record<string, SettingDef>;
  active: Preset;
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState(schema.tabs[0].id);
  const [hover, setHover] = useState<SettingDef | null>(null);
  return (
    <Modal open={open} onClose={onClose} title="Compare presets" wide actions={<button className="btn" onClick={onClose}>Close</button>}>
      <div className="gtabs" role="tablist" aria-label="Difficulty tabs">
        {schema.tabs.map((t) => (
          <button key={t.id} role="tab" aria-selected={t.id === tab} className={t.id === tab ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      {/* Every tab's table sits in the same grid cell, inactive ones hidden, so the dialog is
          always as tall as the longest tab and the tabs don't move when you switch. */}
      <div className="dg-scroll compare-stack" onMouseLeave={() => setHover(null)}>
        {schema.tabs.map((t) => (
          <table key={t.id} className="dg dg-compare" aria-hidden={t.id !== tab} style={{ visibility: t.id === tab ? "visible" : "hidden" }}>
            <thead>
              <tr>
                <th scope="col"><span className="sr-only">Setting</span></th>
                {NAMED_PRESETS.map((p) => (
                  <th key={p} scope="col" className={p === active ? "sel" : ""}>
                    <span className="col-name">{p}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {schema.settings.filter((s) => s.tab === t.id).map((s) => (
                <tr key={s.key} onMouseEnter={() => setHover(s)} className={hover?.key === s.key ? "focus" : ""}>
                  <th scope="row">{s.label}</th>
                  {NAMED_PRESETS.map((p) => {
                    const v = presetValue(s, p, {});
                    return (
                      <td key={p} className={p === active ? "sel" : ""}>
                        {dependencyOff(s, p, {}, byKey) || v === null ? "—" : formatValue(s, v)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        ))}
      </div>
      <p className="muted compare-desc">{hover ? <><b>{hover.label}:</b> {hover.description}</> : "Point at a setting to see what it does."}</p>
    </Modal>
  );
}

/** The editable Custom cell: toggle, dropdown, or a number with − / + steppers. */
function CustomEditor({
  setting: s,
  value,
  error,
  onChange,
  onFocus,
}: {
  setting: SettingDef;
  value: SettingValue | undefined;
  error?: string;
  onChange: (v: SettingValue) => void;
  onFocus: () => void;
}) {
  if (s.type === "bool") {
    return <Toggle checked={Boolean(value)} onChange={onChange} label={s.label} />;
  }
  if (s.type === "enum") {
    return (
      <select value={String(value)} onChange={(e) => onChange(e.target.value)} onFocus={onFocus} aria-label={s.label} aria-invalid={Boolean(error)}>
        {!s.options?.some((o) => o.value === value) && <option value={String(value)}>{String(value)} (invalid)</option>}
        {s.options?.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    );
  }
  return <NumberEditor setting={s} value={value as number} error={error} onChange={onChange} onFocus={onFocus} />;
}

function NumberEditor({
  setting: s,
  value,
  error,
  onChange,
  onFocus,
}: {
  setting: SettingDef;
  value: number;
  error?: string;
  onChange: (v: number) => void;
  onFocus: () => void;
}) {
  const shown = toInput(s, value);
  const [text, setText] = useState(String(shown));
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (!editing) setText(Number.isFinite(shown) ? String(shown) : "");
  }, [shown, editing]);
  const step = inputStep(s);
  const unit = s.display === "percent" ? "%" : s.type === "duration_ns" ? "min" : "";
  const nudge = (dir: number) => {
    const base = Number.isFinite(shown) ? shown : toInput(s, s.default);
    onChange(clamp(s, fromInput(s, Math.round((base + dir * step) / step) * step)));
  };
  return (
    <span className="num">
      <button type="button" tabIndex={-1} aria-label={`Decrease ${s.label}`} onClick={() => nudge(-1)}>−</button>
      <input
        inputMode="decimal"
        value={text}
        aria-label={unit ? `${s.label} (${unit})` : s.label}
        aria-invalid={Boolean(error)}
        title={error}
        onFocus={() => {
          setEditing(true);
          onFocus();
        }}
        onChange={(e) => {
          setText(e.target.value);
          const n = Number(e.target.value.replace(",", "."));
          onChange(e.target.value.trim() === "" || !Number.isFinite(n) ? NaN : fromInput(s, n));
        }}
        onBlur={() => setEditing(false)}
        onKeyDown={(e) => {
          if (e.key === "ArrowUp" || e.key === "ArrowDown") {
            nudge(e.key === "ArrowUp" ? 1 : -1);
            e.preventDefault();
          } else if (e.key === "Escape" || e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
      />
      {unit && <span className="unit">{unit}</span>}
      <button type="button" tabIndex={-1} aria-label={`Increase ${s.label}`} onClick={() => nudge(1)}>+</button>
    </span>
  );
}

function Description({
  setting: s,
  value,
  disabled,
  error,
  onChange,
}: {
  setting: SettingDef;
  value: SettingValue | undefined;
  disabled: boolean;
  error?: string;
  onChange: (v: SettingValue) => void;
}) {
  const numeric = s.type !== "bool" && s.type !== "enum";
  return (
    <aside className="card desc" aria-live="polite">
      <h2>{s.label}</h2>
      <p>{s.description}</p>
      {numeric && s.min !== undefined && s.max !== undefined && (
        <div className="slider">
          <input
            type="range"
            min={toInput(s, s.min)}
            max={toInput(s, s.max)}
            step={inputStep(s)}
            value={Number.isFinite(value as number) ? toInput(s, value as number) : toInput(s, s.default)}
            disabled={disabled}
            onChange={(e) => onChange(fromInput(s, Number(e.target.value)))}
            aria-label={`${s.label} slider`}
          />
          <span>{formatValue(s, value ?? null)}</span>
        </div>
      )}
      {error && <p className="error-text">{error}</p>}
      <dl className="kv">
        <dt>Range</dt><dd>{rangeText(s)}</dd>
        <dt>Default</dt><dd>{formatValue(s, s.presets.Default ?? s.default)}</dd>
      </dl>
    </aside>
  );
}

function CopyFromWorld({
  world,
  open,
  onClose,
  onCopy,
}: {
  world: WorldSummary;
  open: boolean;
  onClose: () => void;
  onCopy: (config: ConfigView, name: string) => void;
}) {
  const { data: worlds = [] } = useWorlds();
  const others = worlds.filter((w) => w.id !== world.id);
  const load = useMutation({
    mutationFn: (w: WorldSummary) => api.config(w.id).then((c) => ({ c, name: w.display_name })),
    onSuccess: ({ c, name }) => onCopy(c, name),
  });
  return (
    <Modal open={open} onClose={onClose} title="Copy difficulty from another world">
      <p className="muted" style={{ marginTop: 0 }}>
        Loads that world's preset and Custom values into this editor. Nothing is saved until you click Save.
        Roles and server settings are not copied.
      </p>
      {others.length === 0 ? (
        <p>There are no other worlds.</p>
      ) : (
        <div className="radio-row">
          {others.map((w) => (
            <button key={w.id} className="btn" disabled={load.isPending} onClick={() => load.mutate(w)}>
              {w.display_name} <span className="muted mono">{w.slug}</span>
            </button>
          ))}
        </div>
      )}
      {load.error && <p className="error-text">{(load.error as Error).message}</p>}
    </Modal>
  );
}
