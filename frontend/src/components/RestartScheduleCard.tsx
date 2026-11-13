import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { RestartSchedule, WorldSummary } from "../api/types";
import { formatTime } from "../format";
import { PLAYERS_MODES, RESTART_PRESETS, modeFields, playersMode, restartPresetFor, type PlayersMode } from "../settings/restarts";
import { Toggle } from "./Toggle";

export function RestartScheduleCard({ world }: { world: WorldSummary }) {
  const { data: schedule } = useQuery({
    queryKey: ["restart-schedule", world.id],
    queryFn: () => api.restartSchedule(world.id),
    refetchInterval: 30_000,
  });
  if (!schedule) return <div className="card"><h3>Scheduled restart</h3><p className="muted">Loading…</p></div>;
  return <Editor world={world} schedule={schedule} />;
}

function Editor({ world, schedule }: { world: WorldSummary; schedule: RestartSchedule }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState(schedule);
  const [preset, setPreset] = useState(restartPresetFor(schedule.cron));
  useEffect(() => {
    setDraft(schedule);
    setPreset(restartPresetFor(schedule.cron));
  }, [schedule]);
  const fields = ["enabled", "only_when_empty", "max_wait_min", "if_still_busy", "min_uptime_h"] as const;
  const dirty = draft.cron.trim() !== schedule.cron || fields.some((k) => draft[k] !== schedule[k]);
  const save = useMutation({
    mutationFn: () =>
      api.updateRestartSchedule(world.id, {
        cron: draft.cron.trim(),
        enabled: draft.enabled,
        only_when_empty: draft.only_when_empty,
        max_wait_min: draft.max_wait_min,
        if_still_busy: draft.if_still_busy,
        min_uptime_h: draft.min_uptime_h,
      }),
    onSuccess: (data) => qc.setQueryData(["restart-schedule", world.id], data),
  });
  const set = (patch: Partial<RestartSchedule>) => {
    save.reset();
    setDraft((d) => ({ ...d, ...patch }));
  };
  const num = (s: string) => (s === "" ? NaN : Number(s));
  const okInt = (n: number, max: number) => Number.isInteger(n) && n >= 0 && n <= max;
  const invalid = !draft.cron.trim() || !okInt(draft.max_wait_min, 1440) || !okInt(draft.min_uptime_h, 168);
  const mode = playersMode(draft);
  const off = !draft.enabled;

  return (
    <div className="card">
      <h3>Scheduled restart <span className="muted small">· keeps long-running servers stable</span></h3>
      <div className="form">
        <label>Enabled</label>
        <Toggle checked={draft.enabled} onChange={(v) => set({ enabled: v })} label="Scheduled restart" />
        <label htmlFor="r-when">When</label>
        <span>
          <select
            id="r-when"
            value={preset}
            disabled={off}
            onChange={(e) => {
              setPreset(e.target.value);
              if (e.target.value !== "custom") set({ cron: e.target.value });
            }}
          >
            {RESTART_PRESETS.map((p) => <option key={p.cron} value={p.cron}>{p.label}</option>)}
            <option value="custom">Custom cron…</option>
          </select>
          {preset === "custom" && (
            <input className="mono" value={draft.cron} disabled={off} onChange={(e) => set({ cron: e.target.value })} aria-label="Cron expression" placeholder="m h dom mon dow" style={{ width: 150, marginTop: 6, display: "block" }} />
          )}
        </span>
        <label htmlFor="r-players">Players online</label>
        <select id="r-players" value={mode} disabled={off} onChange={(e) => set(modeFields(e.target.value as PlayersMode))}>
          {PLAYERS_MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
        </select>
        <label htmlFor="r-wait">Wait up to</label>
        <span>
          <input id="r-wait" type="number" min={0} max={1440} disabled={off || mode === "restart"} value={Number.isFinite(draft.max_wait_min) ? draft.max_wait_min : ""} onChange={(e) => set({ max_wait_min: num(e.target.value) })} aria-invalid={!okInt(draft.max_wait_min, 1440)} style={{ width: 80 }} /> minutes
        </span>
        <label htmlFor="r-uptime">Skip if up less than</label>
        <span>
          <input id="r-uptime" type="number" min={0} max={168} disabled={off} value={Number.isFinite(draft.min_uptime_h) ? draft.min_uptime_h : ""} onChange={(e) => set({ min_uptime_h: num(e.target.value) })} aria-invalid={!okInt(draft.min_uptime_h, 168)} style={{ width: 80 }} /> hours <span className="muted">(0 = never skip)</span>
        </span>
        <span className="hint">A server that just started, updated or recovered from a crash doesn't need restarting yet.</span>
      </div>
      {schedule.waiting_since && (
        <div className="banner info" role="status" style={{ margin: "14px 0 0" }}>
          A restart is due and waiting for players to leave (since {formatTime(schedule.waiting_since)}).
        </div>
      )}
      <div className="top" style={{ margin: "14px 0 0" }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {schedule.enabled && schedule.next_run_at ? `Next: ${formatTime(schedule.next_run_at)}` : "Off"}
          {" · "}schedule runs on {schedule.timezone} time
        </span>
        <span className="spacer" />
        <button className="btn sm" disabled={!dirty || invalid || save.isPending} onClick={() => save.mutate()}>Save schedule</button>
      </div>
      {save.error && <p className="error-text">{(save.error as Error).message}</p>}
      <p className="note">
        Enshrouded can't warn players in game. To give them notice, turn on "Scheduled restart" under
        Settings → Notifications and post it to your Discord.
      </p>
    </div>
  );
}
