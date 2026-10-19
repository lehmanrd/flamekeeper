export type WorldState =
  | "stopped"
  | "starting"
  | "updating"
  | "online"
  | "unresponsive"
  | "crashed"
  | "missing";

export interface Failure {
  title: string;
  hint: string;
  line?: string;
  ts?: string;
}

export interface WorldSummary {
  id: string;
  slug: string;
  display_name: string;
  state: WorldState;
  desired_state: "running" | "stopped";
  container_name: string;
  container_status: string | null;
  query_port: number;
  bind_ip: string;
  image: string;
  players: number | null;
  max_players: number | null;
  player_names: string[];
  server_version: string | null;
  build_id: string | null;
  online_since: string | null;
  update_progress: number | null;
  last_failure: Failure | null;
  busy: boolean;
  pending_restart: boolean;
  recreate_required: boolean;
  metrics: LatestMetrics | null;
  latest_build: string | null;
  game_update_available: boolean;
  image_update_available: boolean;
  config_revision: number;
  applied_revision: number;
}

export interface WorldDetail extends WorldSummary {
  infra: {
    bind_ip: string;
    query_port: number;
    image_repo: string;
    image_tag: string;
    puid: number;
    pgid: number;
    game_branch: string;
    steamcmd_args: string;
    use_ntsync: boolean;
    stop_timeout_s: number;
    cpu_limit: number | null;
    mem_limit_mb: number | null;
  };
  data_dir: string;
  created_at: string;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Job {
  id: string;
  world_id: string | null;
  type: string;
  status: JobStatus;
  progress: number | null;
  message: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  log?: string;
}

export interface WorldEvent {
  id: number;
  world_id: string;
  ts: string;
  kind: string;
  data: Record<string, unknown>;
}

export interface LogLine {
  ts: string | null;
  source: string;
  level: "info" | "warn" | "error";
  text: string;
  player: boolean;
}

export interface SelfCheck {
  id: string;
  label: string;
  status: "ok" | "warn" | "fail" | "skip";
  detail: string;
  fatal: boolean;
}

export interface SystemInfo {
  app: string;
  version: string;
  data_dir: string;
  base_path: string;
  in_container: boolean;
  fake_docker: boolean;
  a2s_mode: string;
  host: {
    cpus: number | null;
    mem_total: number | null;
    kernel: string;
    disk_total: number | null;
    disk_free: number | null;
  };
  selfcheck: { checks: SelfCheck[]; fatal_error: string | null } | null;
  ntsync_available: boolean;
  docker_version: string | null;
}

export interface PlayersInfo {
  count: number | null;
  max: number | null;
  a2s_names: string[];
  online: { name: string; joined_at: string }[];
}

export interface CreateWorldBody {
  display_name: string;
  slug?: string;
  slot_count: number;
  query_port?: number;
  image_tag?: string;
  preset: string;
  roles?: RoleInput[];
  game_branch?: string;
  update_policy?: UpdatePolicyName;
  backup?: { enabled: boolean; cron: string; retention_count: number };
  start?: boolean;
  copy_game_from?: string;
}

export interface Capacity {
  worlds: number;
  mem_total: number | null;
  cpus: number | null;
  disk_free: number | null;
  disk_total: number | null;
  recommended: { ram: number; cores: number; disk: number };
  warnings: string[];
  download_bytes: number;
}

export interface CreatePreview {
  slug: string;
  query_port: number;
  image_tag: string;
  game_branch: string;
  document: Record<string, unknown>;
  capacity: Capacity;
}

export interface PortCheck {
  port: number;
  free: boolean;
  reason: string | null;
}

export interface AppSettingsView {
  values: Record<string, string | number | boolean>;
  defaults: Record<string, string | number | boolean>;
  overridden: string[];
  fields: Record<string, { type: "int" | "bool" | "enum" | "str"; label: string; min?: number; max?: number; options?: string[] }>;
}

/* ---------- config editor ---------- */

export type Preset = "Default" | "Relaxed" | "Hard" | "Survival" | "Custom";
export type SettingValue = number | boolean | string;

export interface RoleView {
  index: number;
  name: string;
  has_password: boolean;
  canKickBan: boolean;
  canAccessInventories: boolean;
  canEditWorld: boolean;
  canEditBase: boolean;
  canExtendBase: boolean;
  reservedSlots: number;
}

export interface ServerFields {
  name: string;
  slotCount: number;
  queryPort: number;
  ip: string;
  voiceChatMode: string;
  enableVoiceChat: boolean;
  enableTextChat: boolean;
  tags: string[];
}

export interface FieldProblem {
  loc: (string | number)[];
  msg: string;
}

export interface ConfigView {
  revision: number;
  applied_revision: number;
  file_exists: boolean;
  preset: Preset;
  gameSettings: Record<string, SettingValue>;
  /** What the server last started with; differs from the above while a revision is pending. */
  applied: { preset: Preset; gameSettings: Record<string, SettingValue> };
  missing_game_settings: string[];
  unknown_game_settings: string[];
  server: ServerFields;
  roles: RoleView[];
  legacy_password: boolean;
  warnings: FieldProblem[];
}

/** A role as sent to PUT /config: a new password, or the index of the role to keep it from. */
export interface RoleInput {
  name: string;
  password?: string;
  keep_password_from?: number;
  canKickBan: boolean;
  canAccessInventories: boolean;
  canEditWorld: boolean;
  canEditBase: boolean;
  canExtendBase: boolean;
  reservedSlots: number;
}

export interface ConfigPut {
  base_revision: number;
  preset?: Preset;
  gameSettings?: Record<string, SettingValue>;
  server?: Partial<ServerFields>;
  roles?: RoleInput[];
  note?: string;
}

export interface ConfigChange {
  section: "preset" | "gameSettings" | "server" | "roles" | "legacy" | "other";
  path: (string | number)[];
  label: string;
  before: unknown;
  after: unknown;
  key?: string;
  secret?: boolean;
  kind?: "added" | "removed" | "opened" | "changed";
}

export interface SaveResponse {
  revision: number;
  changes: ConfigChange[];
  warnings: FieldProblem[];
  config: ConfigView;
}

export interface DryRunResponse {
  revision: number;
  changes: ConfigChange[];
  warnings: FieldProblem[];
}

export interface RevisionMeta {
  revision: number;
  saved_at: string | null;
  source: "created" | "editor" | "restore" | "import" | "server" | "legacy" | string;
  note: string;
  changes: number | null;
  current?: boolean;
  applied?: boolean;
}

export interface RevisionDetail extends Omit<RevisionMeta, "changes"> {
  previous_revision: number | null;
  /** Changes from the previous revision. */
  changes: ConfigChange[];
  /** What restoring this revision would change in the current file. */
  restore_changes: ConfigChange[];
  document: Record<string, unknown>;
}

export type InfraPatch = Partial<{
  image_tag: string;
  query_port: number;
  bind_ip: string;
  puid: number;
  pgid: number;
  game_branch: string;
  steamcmd_args: string;
  use_ntsync: boolean;
  stop_timeout_s: number;
  cpu_limit: number | null;
  mem_limit_mb: number | null;
}>;

export interface ReadmeDrift {
  kind: "new_setting" | "missing_setting" | "range" | "options" | "default" | "tags" | "top_level";
  key: string;
  message: string;
  readme: unknown;
  schema: unknown;
}

export interface SettingsReadmeReport {
  readmes: { version: string | null; sha: string; worlds: string[]; settings: number; drift: ReadmeDrift[] }[];
  not_installed: string[];
  schema_settings: number;
}

/* ---------- backups ---------- */

export type BackupKind = "scheduled" | "manual" | "pre-update" | "pre-restore" | "pre-image-update" | "uploaded" | "image" | "imported";

export interface BackupItem {
  id: string;
  filename: string;
  path: string;
  kind: BackupKind;
  note: string;
  size_bytes: number;
  created_at: string;
  game_build_id: string | null;
  /** When the server wrote the savegame in this backup (can be before created_at). */
  saved_at: string | null;
  exists: boolean;
}

export interface BackupSchedule {
  kind: "backup";
  cron: string;
  enabled: boolean;
  skip_if_idle: boolean;
  retention_count: number;
  max_age_days: number;
  last_run_at: string | null;
  next_run_at: string | null;
  upcoming: string[];
  timezone: string;
}

export interface DiskInfo {
  game_bytes: number;
  savegame_bytes: number;
  backups_bytes: number;
  image_backups_bytes: number;
  free_bytes: number;
  total_bytes: number;
  warnings: string[];
}

export interface BackupsResponse {
  backups: BackupItem[];
  schedule: BackupSchedule;
  disk: DiskInfo;
  /** When the server last wrote the world's savegame. */
  savegame_saved_at: string | null;
}

export interface BackupDetail {
  save_files: string[];
  has_config: boolean;
  manifest: Record<string, unknown> | null;
  warnings: string[];
  uncompressed_bytes: number;
}

export type BackupMode = "next_save" | "last_save" | "stop";

export type ScheduleUpdate = Partial<Pick<BackupSchedule, "cron" | "enabled" | "skip_if_idle" | "retention_count" | "max_age_days">>;

/* ---------- updates, metrics, players ---------- */

export interface LatestMetrics {
  ts: string;
  cpu_pct: number | null;
  mem_mb: number | null;
  mem_limit_mb: number | null;
  net_rx_bps: number | null;
  net_tx_bps: number | null;
  players: number | null;
}

export type UpdatePolicyName = "manual" | "immediate" | "when_empty" | "window";

export interface UpdatePolicy {
  update_policy: UpdatePolicyName;
  update_window_cron: string;
  next_window: string | null;
  image_update_policy: "notify" | "when_empty";
  pre_update_backup: boolean;
}

export interface UpdateStatus {
  game: {
    branch: string;
    installed: string | null;
    latest: string | null;
    checked_at: string | null;
    source: string | null;
    error: string | null;
    update_available: boolean;
  };
  image: {
    image: string;
    local_digest: string | null;
    remote_digest: string | null;
    checked_at: string | null;
    error: string | null;
    update_available: boolean;
  };
  policy: UpdatePolicy;
  pending_job: { id: string; type: string; status: JobStatus; message: string } | null;
  auto_blocked_build: string | null;
}

export interface UpdateHistoryItem {
  id: string;
  type: "update" | "image_update";
  status: JobStatus;
  message: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

export type MetricsRange = "1h" | "24h" | "7d";

export interface MetricPoint {
  ts: string;
  cpu_pct: number | null;
  mem_mb: number | null;
  net_rx_bps: number | null;
  net_tx_bps: number | null;
  players: number | null;
}

export interface MetricsResponse {
  range: MetricsRange;
  step_s: number;
  points: MetricPoint[];
}

export interface PlayersHistory {
  players: { player: string; sessions: number; playtime_s: number; last_seen: string; online: boolean }[];
  sessions: { player: string; joined_at: string; left_at: string | null; duration_s: number }[];
}

export interface HostInfo {
  cpu_pct: number | null;
  cpus: number | null;
  mem_total: number | null;
  mem_used: number | null;
  disk_total: number | null;
  disk_free: number | null;
  data_dir: string;
  worlds: number;
  players: number;
  slots: number;
}

/* ---------- Login and notifications ---------- */

export interface AuthStatus {
  setup_required: boolean;
  /** No admin yet, and the browser's setup window (after FlameKeeper starts) has passed. */
  setup_locked: boolean;
  user: { username: string } | null;
}

/** FlameKeeper's own updates (GET /api/system/update). */
export interface AppUpdateStatus {
  current: string;
  latest: { version: string; name: string; url: string | null; published_at: string | null } | null;
  update_available: boolean;
  checked_at: string | null;
  check_error: string | null;
  checks_enabled: boolean;
  can_update: boolean;
  cannot_update_reason: string | null;
  /** What to run on the server instead, e.g. "sudo flamekeeper update". */
  command: string;
  run: {
    status: "running" | "succeeded" | "failed";
    from: string;
    to: string;
    started_at: string;
    finished_at: string | null;
    error: string | null;
    log_tail: string | null;
  } | null;
}

export interface NotificationSettings {
  discord_configured: boolean;
  /** Masked: the webhook's id with its token hidden. */
  discord_webhook: string;
  events: string[];
  available: { id: string; label: string; default: boolean }[];
}

export interface ImportCandidate {
  id: string;
  name: string;
  image: string;
  status: string;
  running: boolean;
  /** Looks like an Enshrouded server (image name, ports or mounts). */
  likely: boolean;
  ports: Record<string, number[]>;
  mounts: { type: string; name: string | null; source: string; destination: string }[];
  restart_policy: string;
  /** Slug of the world it was already imported into. */
  imported_into: string | null;
}

export interface ImportScan {
  token: string;
  created_at: string;
  source:
    | { kind: "container"; container_id: string; container_name: string; image: string; label: string }
    | { kind: "host_path"; host_path: string; label: string }
    | { kind: "upload"; filename: string; label: string };
  config_path: string | null;
  save_path: string | null;
  save_options: string[];
  summary: {
    has_config: boolean;
    name: string | null;
    slot_count: number | null;
    query_port: number | null;
    preset: string | null;
    custom_settings: number;
    roles: { name: string; has_password: boolean; reserved_slots: number }[];
    legacy_password: boolean;
    bans: number;
    save_format: "1.0" | "pre-1.0";
    save_files: number;
    save_bytes: number;
    saved_at: string | null;
  };
  warnings: string[];
  /** Problems that block the import. */
  errors: string[];
  port: number;
  /** The old server holds this port now; the import stops it first. */
  reusable_port: number | null;
  running: boolean;
  players_online: number | null;
  auto_remove?: boolean;
  restart_policy?: string;
  volumes?: string[];
  bind_sources?: string[];
  /** The old server's install (enshrouded_server.exe), if found. */
  game_files: { path: string; build: string | null } | null;
}

export interface ImportCreateBody {
  display_name: string;
  slug?: string;
  query_port?: number;
  image_tag?: string;
  backup?: { enabled: boolean; cron: string; retention_count: number };
  start?: boolean;
  copy_game_from?: string;
  copy_game_from_source?: boolean;
  save_path?: string;
  confirm_players?: boolean;
}

export interface WorldImportInfo {
  state: "importing" | "imported" | "rolled_back" | "container_removed" | "volumes_removed" | "dismissed";
  source_kind: "container" | "host_path" | "upload";
  source_label: string;
  image: string;
  volumes: string[];
  bind_sources: string[];
  container: { name: string; status: string; running: boolean; restart_policy: string } | null;
  created_at: string;
}
