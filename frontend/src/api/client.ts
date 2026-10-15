import type { SettingsSchema } from "../settings/schema";
import type {
  AppSettingsView,
  AuthStatus,
  NotificationSettings,
  BackupDetail,
  Capacity,
  CreatePreview,
  PortCheck,
  BackupItem,
  BackupMode,
  BackupSchedule,
  BackupsResponse,
  ConfigPut,
  ConfigView,
  CreateWorldBody,
  DryRunResponse,
  FieldProblem,
  HostInfo,
  InfraPatch,
  MetricsRange,
  MetricsResponse,
  PlayersHistory,
  UpdateHistoryItem,
  UpdatePolicy,
  UpdateStatus,
  Job,
  LogLine,
  PlayersInfo,
  RevisionDetail,
  RevisionMeta,
  SaveResponse,
  ScheduleUpdate,
  SettingsReadmeReport,
  SystemInfo,
  WorldDetail,
  WorldEvent,
  WorldSummary,
} from "./types";

declare global {
  interface Window {
    __FK_BASE__?: string;
  }
}

/** Base path the backend serves the app under ("" at the root). */
export const BASE = window.__FK_BASE__ ?? "";

export function apiUrl(path: string): string {
  return `${BASE}/api${path}`;
}

/** An RFC 7807 problem response. `errors` lists per-field problems (validation). */
export class ApiError extends Error {
  constructor(
    public status: number,
    public title: string,
    detail: string,
    public errors: FieldProblem[] = [],
  ) {
    super(detail || title);
  }
}

/** Fired when the server says we're not signed in; the auth gate shows the login page. */
export const UNAUTHORIZED_EVENT = "fk:unauthorized";

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const form = body instanceof FormData;
  // The server refuses changes without this header (cross-site request protection).
  const headers: Record<string, string> = { "X-FlameKeeper": "1" };
  if (body !== undefined && !form) headers["Content-Type"] = "application/json";
  const res = await fetch(apiUrl(path), {
    method,
    headers,
    body: body === undefined ? undefined : form ? body : JSON.stringify(body),
  });
  if (res.status === 401 && !path.startsWith("/auth/")) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!res.ok) {
    let title = res.statusText;
    let detail = "";
    let errors: FieldProblem[] = [];
    try {
      const problem = await res.json();
      title = problem.title ?? title;
      detail = problem.detail ?? "";
      errors = Array.isArray(problem.errors) ? problem.errors : [];
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, title, detail, errors);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  authStatus: () => request<AuthStatus>("GET", "/auth/status"),
  setup: (body: { username: string; password: string }) =>
    request<{ user: { username: string } }>("POST", "/auth/setup", body),
  login: (username: string, password: string) =>
    request<{ user: { username: string } }>("POST", "/auth/login", { username, password }),
  logout: () => request<void>("POST", "/auth/logout"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("POST", "/auth/password", { current_password, new_password }),
  notifications: () => request<NotificationSettings>("GET", "/settings/notifications"),
  saveNotifications: (body: { discord_webhook?: string; events?: string[] }) =>
    request<NotificationSettings>("PUT", "/settings/notifications", body),
  testNotification: (discord_webhook?: string) =>
    request<void>("POST", "/settings/notifications/test", { discord_webhook }),
  system: () => request<SystemInfo>("GET", "/system"),
  rerunSelfcheck: () => request<unknown>("POST", "/system/selfcheck"),
  settingsReadme: () => request<SettingsReadmeReport>("GET", "/system/settings-readme"),
  settingsSchema: () => request<SettingsSchema>("GET", "/settings-schema"),
  worlds: () => request<WorldSummary[]>("GET", "/worlds"),
  world: (id: string) => request<WorldDetail>("GET", `/worlds/${id}`),
  suggestPort: () => request<{ port: number }>("GET", "/worlds/suggest-port"),
  checkPort: (port: number, worldId?: string) =>
    request<PortCheck>("GET", `/worlds/check-port?port=${port}${worldId ? `&world_id=${worldId}` : ""}`),
  previewWorld: (body: CreateWorldBody) => request<CreatePreview>("POST", "/worlds?dry_run=true", body),
  capacity: (extra = 0) => request<Capacity>("GET", `/system/capacity?extra=${extra}`),
  appSettings: () => request<AppSettingsView>("GET", "/settings/app"),
  saveAppSettings: (changes: Record<string, unknown>) => request<AppSettingsView>("PUT", "/settings/app", changes),
  createWorld: (body: CreateWorldBody) =>
    request<{ world: WorldDetail; job_id: string }>("POST", "/worlds", body),
  deleteWorld: (id: string, deleteData: boolean) =>
    request<{ job_id: string }>("DELETE", `/worlds/${id}?delete_data=${deleteData}`),
  start: (id: string) => request<{ job_id: string }>("POST", `/worlds/${id}/start`),
  stop: (id: string, when: "now" | "empty" = "now") =>
    request<{ job_id: string }>("POST", `/worlds/${id}/stop`, { when }),
  restart: (id: string, mode: "process" | "container", when: "now" | "empty" = "now") =>
    request<{ job_id: string }>("POST", `/worlds/${id}/restart`, { mode, when }),
  recreate: (id: string) => request<{ job_id: string }>("POST", `/worlds/${id}/recreate`),
  patchWorld: (id: string, body: InfraPatch) =>
    request<{ world: WorldDetail; changed: string[]; recreate_required: boolean }>(
      "PATCH",
      `/worlds/${id}`,
      body,
    ),
  config: (id: string) => request<ConfigView>("GET", `/worlds/${id}/config`),
  saveConfig: (id: string, body: ConfigPut) => request<SaveResponse>("PUT", `/worlds/${id}/config`, body),
  previewConfig: (id: string, body: ConfigPut) =>
    request<DryRunResponse>("PUT", `/worlds/${id}/config?dry_run=true`, body),
  applyConfig: (id: string, when: "now" | "empty" = "now") =>
    request<{ job_id: string }>("POST", `/worlds/${id}/config/apply`, { when }),
  revisions: (id: string) => request<RevisionMeta[]>("GET", `/worlds/${id}/config/revisions`),
  revision: (id: string, rev: number) =>
    request<RevisionDetail>("GET", `/worlds/${id}/config/revisions/${rev}`),
  restoreRevision: (id: string, rev: number, baseRevision: number) =>
    request<SaveResponse>("POST", `/worlds/${id}/config/revisions/${rev}/restore`, {
      base_revision: baseRevision,
    }),
  exportUrl: (id: string, includePasswords: boolean) =>
    apiUrl(`/worlds/${id}/config/export?include_passwords=${includePasswords}`),
  importConfig: (
    id: string,
    body: { document: unknown; base_revision: number; convert_legacy_password: boolean },
    dryRun: boolean,
  ) =>
    request<DryRunResponse | SaveResponse>("POST", `/worlds/${id}/config/import?dry_run=${dryRun}`, body),
  convertLegacyPassword: (id: string, baseRevision: number) =>
    request<SaveResponse>("POST", `/worlds/${id}/config/legacy-password/convert`, {
      base_revision: baseRevision,
    }),
  host: () => request<HostInfo>("GET", "/system/host"),
  updateStatus: (id: string) => request<UpdateStatus>("GET", `/worlds/${id}/update`),
  checkUpdates: (id: string) => request<UpdateStatus>("POST", `/worlds/${id}/update/check`),
  startUpdate: (id: string, body: { target: "game" | "image"; when: "now" | "empty"; force?: boolean }) =>
    request<{ job_id: string }>("POST", `/worlds/${id}/update`, body),
  setUpdatePolicy: (id: string, body: Partial<Omit<UpdatePolicy, "next_window">>) =>
    request<UpdatePolicy>("PUT", `/worlds/${id}/update-policy`, body),
  updateHistory: (id: string) => request<UpdateHistoryItem[]>("GET", `/worlds/${id}/update/history`),
  metrics: (id: string, range: MetricsRange) =>
    request<MetricsResponse>("GET", `/worlds/${id}/metrics?range=${range}`),
  playersHistory: (id: string) => request<PlayersHistory>("GET", `/worlds/${id}/players/history`),
  backups: (id: string, refreshDisk = false) =>
    request<BackupsResponse>("GET", `/worlds/${id}/backups${refreshDisk ? "?refresh_disk=true" : ""}`),
  backup: (id: string, backupId: string) => request<BackupDetail>("GET", `/worlds/${id}/backups/${backupId}`),
  createBackup: (id: string, note: string, mode: BackupMode) =>
    request<{ job_id: string }>("POST", `/worlds/${id}/backups`, { note, mode }),
  uploadBackup: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<BackupItem & { warnings: string[]; save_files: number; has_config: boolean }>(
      "POST",
      `/worlds/${id}/backups/upload`,
      form,
    );
  },
  backupDownloadUrl: (id: string, backupId: string) => apiUrl(`/worlds/${id}/backups/${backupId}/download`),
  setBackupNote: (id: string, backupId: string, note: string) =>
    request<BackupItem>("PATCH", `/worlds/${id}/backups/${backupId}`, { note }),
  deleteBackup: (id: string, backupId: string) => request<void>("DELETE", `/worlds/${id}/backups/${backupId}`),
  restoreBackup: (id: string, backupId: string, restoreConfig: boolean, when: "now" | "empty") =>
    request<{ job_id: string }>("POST", `/worlds/${id}/backups/${backupId}/restore`, {
      restore_config: restoreConfig,
      when,
    }),
  cloneWorld: (id: string, body: { source: string; display_name: string; slug?: string; query_port?: number }) =>
    request<{ world: WorldDetail; job_id: string }>("POST", `/worlds/${id}/clone`, body),
  updateBackupSchedule: (id: string, body: ScheduleUpdate) =>
    request<BackupSchedule>("PUT", `/worlds/${id}/schedules/backup`, body),
  revealPassword: (id: string, index: number) =>
    request<{ index: number; name: string; password: string }>("POST", `/worlds/${id}/roles/${index}/reveal`),
  events: (id: string, limit = 30, kinds: string[] = [], before?: number) => {
    const q = new URLSearchParams({ limit: String(limit) });
    kinds.forEach((k) => q.append("kind", k));
    if (before !== undefined) q.set("before", String(before));
    return request<WorldEvent[]>("GET", `/worlds/${id}/events?${q}`);
  },
  players: (id: string) => request<PlayersInfo>("GET", `/worlds/${id}/players`),
  logs: (id: string, tail = 500) =>
    request<{ lines: LogLine[] }>("GET", `/worlds/${id}/logs?tail=${tail}`),
  jobs: (params: { world_id?: string; status?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return request<Job[]>("GET", `/jobs?${q}`);
  },
  job: (id: string) => request<Job>("GET", `/jobs/${id}`),
  cancelJob: (id: string) => request<unknown>("POST", `/jobs/${id}/cancel`),
};
