package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
)

const (
	labelManaged = "flamekeeper.managed"
	labelHelper  = "flamekeeper.managed.helper"
	labelWorldID = "flamekeeper.world_id"
	labelRole    = "flamekeeper.role"

	ntsyncDevice = "/dev/ntsync"
)

// The one host-network helper: reading the host's UDP sockets (backend/app/services/ports.py).
var hostPortsCmd = []string{"cat", "/proc/net/udp", "/proc/net/udp6"}

var errNotFound = errors.New("not found")

type containerInfo struct {
	ID     string
	Name   string // without the leading /
	Labels map[string]string
}

// Inspector looks things up on the real Docker daemon.
type Inspector interface {
	Container(ctx context.Context, ref string) (containerInfo, error)
	NetworkName(ctx context.Context, id string) (string, error)
}

type Decision struct {
	Allow  bool
	Rule   string // which rule matched, for the log
	Reason string // why it was denied
}

func allow(rule string) Decision { return Decision{Allow: true, Rule: rule} }

func deny(rule, format string, args ...any) Decision {
	return Decision{Rule: rule, Reason: fmt.Sprintf(format, args...)}
}

type Policy struct {
	cfg  Config
	insp Inspector
	now  func() time.Time
}

func NewPolicy(cfg Config, insp Inspector) *Policy {
	return &Policy{cfg: cfg, insp: insp, now: time.Now}
}

var versionPrefix = regexp.MustCompile(`^/v[0-9]+(\.[0-9]+)*/`)

// Decide checks one Docker API request. body is only read for the requests whose
// body matters (see needsBody).
func (p *Policy) Decide(ctx context.Context, method, path string, q url.Values, body []byte) Decision {
	path = versionPrefix.ReplaceAllString(path, "/")
	seg := strings.Split(strings.Trim(path, "/"), "/")
	read := method == "GET" || method == "HEAD"
	n := len(seg)

	switch seg[0] {
	case "_ping", "version", "info":
		if read && n == 1 {
			return allow("read")
		}
	case "containers":
		return p.containers(ctx, method, seg, body)
	case "images":
		if read && seg[n-1] == "json" {
			return allow("read")
		}
		if method == "POST" && n == 2 && seg[1] == "create" {
			return p.pull(q)
		}
	case "distribution":
		if read && seg[n-1] == "json" {
			return allow("read")
		}
	case "networks":
		if read {
			return allow("read")
		}
		if method == "POST" && n == 2 && seg[1] == "create" {
			return p.networkCreate(body)
		}
		if method == "POST" && n == 3 && seg[2] == "connect" {
			return p.networkConnect(ctx, seg[1], body)
		}
	case "volumes":
		if read {
			return allow("read")
		}
		if method == "DELETE" && n == 2 {
			if p.grant() != nil {
				return allow("import-volume-remove")
			}
			return deny("volume-remove", "removing volumes is only allowed during an import grant")
		}
	case "exec":
		if read && n == 3 && seg[2] == "json" {
			return allow("read")
		}
		if method == "POST" && n == 3 && (seg[2] == "start" || seg[2] == "resize") {
			return allow("exec-start") // checked when it was created
		}
	}
	return deny("default", "%s %s isn't something FlameKeeper needs", method, path)
}

func (p *Policy) containers(ctx context.Context, method string, seg []string, body []byte) Decision {
	n := len(seg)
	read := method == "GET" || method == "HEAD"
	switch {
	case n == 2 && seg[1] == "json" && read:
		return allow("read")
	case n == 2 && seg[1] == "create" && method == "POST":
		return p.create(ctx, body)
	case n == 3 && seg[2] == "json" && read:
		return allow("read")
	case n == 2 && method == "DELETE":
		return p.target(ctx, seg[1], "remove")
	case n == 3 && method == "POST" && slices.Contains([]string{"start", "stop", "restart", "kill", "wait"}, seg[2]):
		return p.target(ctx, seg[1], seg[2])
	case n == 3 && read && slices.Contains([]string{"logs", "stats", "archive"}, seg[2]):
		return p.target(ctx, seg[1], seg[2])
	case n == 3 && method == "POST" && seg[2] == "update":
		if d := onlyKeys(body, "update", "RestartPolicy"); !d.Allow {
			return d
		}
		return p.target(ctx, seg[1], "update")
	case n == 3 && method == "POST" && seg[2] == "exec":
		return p.execCreate(ctx, seg[1], body)
	}
	return deny("default", "%s /%s isn't something FlameKeeper needs", method, strings.Join(seg, "/"))
}

func managed(l map[string]string) bool {
	return l[labelManaged] == "true" || l[labelHelper] == "true" || l[labelRole] == "self-update"
}

func isWorld(l map[string]string) bool {
	return l[labelManaged] == "true" && l[labelWorldID] != "" && l[labelHelper] != "true"
}

// target allows an operation on an existing container FlameKeeper made, or on a
// container the current import grant names.
func (p *Policy) target(ctx context.Context, id, rule string) Decision {
	c, err := p.insp.Container(ctx, id)
	if errors.Is(err, errNotFound) {
		return allow(rule) // Docker answers 404 itself
	}
	if err != nil {
		return deny(rule, "couldn't inspect container %s: %v", id, err)
	}
	if managed(c.Labels) {
		return allow(rule)
	}
	if g := p.grant(); g != nil && g.hasContainer(c) {
		return allow("import-" + rule)
	}
	return deny(rule, "container %s isn't a FlameKeeper container (allowed only when an import grant names it)", id)
}

func (p *Policy) pull(q url.Values) Decision {
	if q.Get("fromSrc") != "" || q.Get("repo") != "" {
		return deny("pull", "importing images isn't allowed")
	}
	ref := q.Get("fromImage")
	if tag := q.Get("tag"); tag != "" {
		if strings.HasPrefix(tag, "sha256:") {
			ref += "@" + tag
		} else {
			ref += ":" + tag
		}
	}
	if p.gameImageAllowed(ref) || p.helperImageAllowed(ref) {
		return allow("pull")
	}
	return deny("pull", "image %q isn't on the allowlist", ref)
}

func (p *Policy) execCreate(ctx context.Context, id string, body []byte) Decision {
	var e struct {
		Privileged bool
		User       string
	}
	if err := json.Unmarshal(body, &e); err != nil {
		return deny("exec", "bad exec body: %v", err)
	}
	if e.Privileged || e.User != "" {
		return deny("exec", "exec may not be privileged or change user")
	}
	c, err := p.insp.Container(ctx, id)
	if errors.Is(err, errNotFound) {
		return allow("exec")
	}
	if err != nil {
		return deny("exec", "couldn't inspect container %s: %v", id, err)
	}
	if !isWorld(c.Labels) {
		return deny("exec", "exec is only allowed in world containers")
	}
	return allow("exec")
}

func (p *Policy) networkCreate(body []byte) Decision {
	if d := onlyKeys(body, "network-create", "Name", "Driver", "Labels", "CheckDuplicate"); !d.Allow {
		return d
	}
	var n struct{ Name, Driver string }
	if err := json.Unmarshal(body, &n); err != nil {
		return deny("network-create", "bad body: %v", err)
	}
	if n.Name != p.cfg.Network || (n.Driver != "" && n.Driver != "bridge") {
		return deny("network-create", "only the %q bridge network may be created", p.cfg.Network)
	}
	return allow("network-create")
}

func (p *Policy) networkConnect(ctx context.Context, id string, body []byte) Decision {
	name, err := p.insp.NetworkName(ctx, id)
	if err != nil {
		return deny("network-connect", "couldn't inspect network %s: %v", id, err)
	}
	if name != p.cfg.Network {
		return deny("network-connect", "only the %q network may be joined", p.cfg.Network)
	}
	var c struct{ Container string }
	if err := json.Unmarshal(body, &c); err != nil || c.Container == "" {
		return deny("network-connect", "bad body")
	}
	ctr, err := p.insp.Container(ctx, c.Container)
	if err != nil {
		return deny("network-connect", "couldn't inspect container %s: %v", c.Container, err)
	}
	if isWorld(ctr.Labels) || ctr.Labels[labelRole] == "controller" {
		return allow("network-connect")
	}
	return deny("network-connect", "only world containers and the controller (label %s=controller) may join %q",
		labelRole, p.cfg.Network)
}

// -- container create -------------------------------------------------------------------

type createBody struct {
	Image            string
	Cmd              json.RawMessage
	Labels           map[string]string
	HostConfig       map[string]json.RawMessage
	NetworkingConfig *struct {
		EndpointsConfig map[string]json.RawMessage
	}
}

func (p *Policy) create(ctx context.Context, body []byte) Decision {
	var c createBody
	if err := json.Unmarshal(body, &c); err != nil {
		return deny("create", "bad create body: %v", err)
	}
	switch {
	case c.Labels[labelHelper] == "true":
		return p.createHelper(ctx, &c)
	case c.Labels[labelManaged] == "true":
		return p.createWorld(&c)
	case c.Labels[labelRole] == "self-update":
		return deny("create-self-update", "the privileged self-update helper isn't allowed (replaced by the host-side update)")
	}
	return deny("create", "only world containers and helpers may be created (missing %s or %s label)",
		labelManaged, labelHelper)
}

func (p *Policy) createWorld(c *createBody) Decision {
	const rule = "create-world"
	if !p.gameImageAllowed(c.Image) {
		return deny(rule, "image %q isn't an allowed game image", c.Image)
	}
	if c.Labels[labelWorldID] == "" {
		return deny(rule, "missing %s label", labelWorldID)
	}
	hc := c.HostConfig
	if d := onlyHostConfig(rule, hc, "Binds", "PortBindings", "RestartPolicy", "Devices",
		"NanoCpus", "Memory", "LogConfig", "NetworkMode"); !d.Allow {
		return d
	}
	if mode := str(hc["NetworkMode"]); !slices.Contains([]string{"", "default", "bridge", p.cfg.Network}, mode) {
		return deny(rule, "network mode %q isn't allowed", mode)
	}
	if d := p.endpoints(rule, c, p.cfg.Network); !d.Allow {
		return d
	}
	var binds []string
	if err := unmarshalOpt(hc["Binds"], &binds); err != nil {
		return deny(rule, "bad Binds: %v", err)
	}
	worlds := filepath.Join(p.cfg.DataDir, "worlds")
	for _, b := range binds {
		src, _, err := parseBind(b)
		if err != nil {
			return deny(rule, "%v", err)
		}
		if err := p.checkPath(src, worlds); err != nil {
			return deny(rule, "bind %q: %v", b, err)
		}
	}
	if d := devicesOnly(rule, hc, ntsyncDevice); !d.Allow {
		return d
	}
	var ports map[string][]struct{ HostIp, HostPort string }
	if err := unmarshalOpt(hc["PortBindings"], &ports); err != nil {
		return deny(rule, "bad PortBindings: %v", err)
	}
	for k, bs := range ports {
		if !strings.HasSuffix(k, "/udp") {
			return deny(rule, "port %s: only UDP ports may be published", k)
		}
		for _, b := range bs {
			if port, err := strconv.Atoi(b.HostPort); b.HostPort != "" && (err != nil || port < 1024) {
				return deny(rule, "host port %q: must be 1024 or higher", b.HostPort)
			}
		}
	}
	if d := logConfig(rule, hc); !d.Allow {
		return d
	}
	return allow(rule)
}

func (p *Policy) createHelper(ctx context.Context, c *createBody) Decision {
	const rule = "create-helper"
	if !p.helperImageAllowed(c.Image) {
		return deny(rule, "image %q isn't an allowed helper image", c.Image)
	}
	hc := c.HostConfig
	if d := onlyHostConfig(rule, hc, "Binds", "Mounts", "Devices", "NetworkMode", "VolumesFrom",
		"LogConfig", "RestartPolicy", "AutoRemove"); !d.Allow {
		return d
	}
	if d := p.endpoints(rule, c); !d.Allow {
		return d
	}
	if d := logConfig(rule, hc); !d.Allow {
		return d
	}
	if rp := str(hc["RestartPolicy"]); rp != "" {
		return deny(rule, "bad RestartPolicy")
	}
	var binds, volumesFrom []string
	if err := unmarshalOpt(hc["Binds"], &binds); err != nil {
		return deny(rule, "bad Binds: %v", err)
	}
	if err := unmarshalOpt(hc["VolumesFrom"], &volumesFrom); err != nil {
		return deny(rule, "bad VolumesFrom: %v", err)
	}
	var mounts []map[string]json.RawMessage
	if err := unmarshalOpt(hc["Mounts"], &mounts); err != nil {
		return deny(rule, "bad Mounts: %v", err)
	}
	if d := devicesOnly(rule, hc, ntsyncDevice); !d.Allow {
		return d
	}

	switch mode := str(hc["NetworkMode"]); mode {
	case "", "default", "bridge":
	case "host":
		var cmd []string
		if err := unmarshalOpt(c.Cmd, &cmd); err != nil || !slices.Equal(cmd, hostPortsCmd) ||
			len(binds) > 0 || len(volumesFrom) > 0 || len(mounts) > 0 {
			return deny(rule, "host networking is only allowed for %q with no mounts", strings.Join(hostPortsCmd, " "))
		}
	default:
		return deny(rule, "network mode %q isn't allowed", mode)
	}

	// Writable binds: only DATA_DIR (scratch and staging folders).
	for _, b := range binds {
		src, _, err := parseBind(b)
		if err != nil {
			return deny(rule, "%v", err)
		}
		if err := p.checkPath(src, p.cfg.DataDir); err != nil {
			return deny(rule, "bind %q: %v", b, err)
		}
	}
	// Host folders: read-only mounts (Docker doesn't create a missing source), from
	// DATA_DIR or a folder the import grant names.
	g := p.grant()
	granted := false // a grant was needed: logged as import-create-helper
	for _, m := range mounts {
		d := p.helperMount(rule, m, g)
		if !d.Allow {
			return d
		}
		granted = granted || d.Rule != rule
	}
	for _, v := range volumesFrom {
		ref, ro := strings.CutSuffix(v, ":ro")
		if !ro {
			return deny(rule, "volumes_from %q must be read-only (:ro)", v)
		}
		if !p.grantedContainer(ctx, g, ref) {
			return deny(rule, "volumes_from %q: allowed only when an import grant names the container", ref)
		}
		granted = true
	}
	if granted {
		return allow("import-" + rule)
	}
	return allow(rule)
}

func (p *Policy) helperMount(rule string, m map[string]json.RawMessage, g *importGrant) Decision {
	for k, v := range m {
		if !slices.Contains([]string{"Type", "Source", "Target", "ReadOnly", "BindOptions"}, k) && !isZeroJSON(v) {
			return deny(rule, "mount option %s isn't allowed", k)
		}
	}
	var mt struct {
		Type, Source, Target string
		ReadOnly             bool
		BindOptions          map[string]json.RawMessage
	}
	raw, _ := json.Marshal(m)
	if err := json.Unmarshal(raw, &mt); err != nil {
		return deny(rule, "bad mount: %v", err)
	}
	if mt.Type != "bind" || !mt.ReadOnly {
		return deny(rule, "mount of %q: only read-only bind mounts are allowed", mt.Source)
	}
	for k, v := range mt.BindOptions {
		switch k {
		case "Propagation":
			if prop := str(v); prop != "" && prop != "private" && prop != "rprivate" {
				return deny(rule, "mount of %q: propagation %q isn't allowed", mt.Source, prop)
			}
		case "NonRecursive", "ReadOnlyForceRecursive":
		default:
			if !isZeroJSON(v) {
				return deny(rule, "mount of %q: bind option %s isn't allowed", mt.Source, k)
			}
		}
	}
	if !filepath.IsAbs(mt.Source) || filepath.Clean(mt.Source) != mt.Source {
		return deny(rule, "mount of %q: the source must be a clean absolute path", mt.Source)
	}
	if err := p.checkPath(mt.Source, p.cfg.DataDir); err == nil {
		return allow(rule)
	}
	if f, bad := forbiddenHostPath(mt.Source); bad {
		return deny(rule, "mount of %q: %s and the folders around it are never mounted", mt.Source, f)
	}
	if g == nil || !g.hasPath(mt.Source) {
		return deny(rule, "mount of %q: host folders outside DATA_DIR are allowed only when an import grant names them", mt.Source)
	}
	return allow("import-" + rule)
}

// devicesOnly allows HostConfig.Devices to hold only the given device.
func devicesOnly(rule string, hc map[string]json.RawMessage, device string) Decision {
	var devices []struct{ PathOnHost, PathInContainer, CgroupPermissions string }
	if err := unmarshalOpt(hc["Devices"], &devices); err != nil {
		return deny(rule, "bad Devices: %v", err)
	}
	for _, d := range devices {
		if d.PathOnHost != device || (d.PathInContainer != "" && d.PathInContainer != device) ||
			strings.Trim(d.CgroupPermissions, "rwm") != "" {
			return deny(rule, "device %q isn't allowed (only %s)", d.PathOnHost, device)
		}
	}
	return allow(rule)
}

// endpoints allows NetworkingConfig to name only the given networks.
func (p *Policy) endpoints(rule string, c *createBody, allowed ...string) Decision {
	if c.NetworkingConfig == nil {
		return allow(rule)
	}
	for name := range c.NetworkingConfig.EndpointsConfig {
		if !slices.Contains(allowed, name) {
			return deny(rule, "network %q isn't allowed", name)
		}
	}
	return allow(rule)
}

func logConfig(rule string, hc map[string]json.RawMessage) Decision {
	var lc struct{ Type string }
	if err := unmarshalOpt(hc["LogConfig"], &lc); err != nil {
		return deny(rule, "bad LogConfig: %v", err)
	}
	if !slices.Contains([]string{"", "json-file", "local"}, lc.Type) {
		return deny(rule, "log driver %q isn't allowed", lc.Type)
	}
	return allow(rule)
}

// parseBind splits "src:dst[:opts]" and reports whether it is read-only. Mount
// propagation and other options are refused.
func parseBind(b string) (src string, ro bool, err error) {
	parts := strings.Split(b, ":")
	if len(parts) < 2 || len(parts) > 3 {
		return "", false, fmt.Errorf("bind %q: expected src:dst[:opts]", b)
	}
	if len(parts) == 3 {
		for _, o := range strings.Split(parts[2], ",") {
			switch o {
			case "ro":
				ro = true
			case "rw", "private", "rprivate", "nocopy":
			default:
				return "", false, fmt.Errorf("bind %q: option %q isn't allowed", b, o)
			}
		}
	}
	return parts[0], ro, nil
}

// checkPath requires src to be a clean absolute path at or under root (itself at or
// under DATA_DIR), with no symlink in any existing component below DATA_DIR. Docker
// follows symlinks in bind sources, and FlameKeeper can write to DATA_DIR.
func (p *Policy) checkPath(src, root string) error {
	if !filepath.IsAbs(src) {
		return errors.New("named volumes aren't allowed")
	}
	if filepath.Clean(src) != src {
		return errors.New("path isn't clean")
	}
	rel, err := filepath.Rel(root, src)
	if err != nil || rel == ".." || strings.HasPrefix(rel, "../") {
		return fmt.Errorf("outside %s", root)
	}
	under, _ := filepath.Rel(p.cfg.DataDir, src)
	cur := p.cfg.DataDir
	for _, part := range strings.Split(under, "/") {
		if part == "." {
			break
		}
		cur = filepath.Join(cur, part)
		fi, err := os.Lstat(cur)
		if errors.Is(err, os.ErrNotExist) {
			return nil // Docker creates the rest
		}
		if err != nil {
			return fmt.Errorf("couldn't check %s: %v", cur, err)
		}
		if fi.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("%s is a symlink", cur)
		}
	}
	return nil
}

// -- JSON helpers -------------------------------------------------------------------------

func onlyHostConfig(rule string, hc map[string]json.RawMessage, keys ...string) Decision {
	for k, v := range hc {
		if !slices.Contains(keys, k) && !isZeroJSON(v) {
			return deny(rule, "HostConfig.%s isn't allowed", k)
		}
	}
	return allow(rule)
}

// onlyKeys allows a JSON object body whose other keys are all empty.
func onlyKeys(body []byte, rule string, keys ...string) Decision {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(body, &m); err != nil {
		return deny(rule, "bad body: %v", err)
	}
	for k, v := range m {
		if !slices.Contains(keys, k) && !isZeroJSON(v) {
			return deny(rule, "%s isn't allowed", k)
		}
	}
	return allow(rule)
}

// isZeroJSON: null, false, 0, "", [], {} or an object of those.
func isZeroJSON(raw json.RawMessage) bool {
	var v any
	if len(raw) == 0 || json.Unmarshal(raw, &v) != nil {
		return len(raw) == 0
	}
	return isZero(v)
}

func isZero(v any) bool {
	switch t := v.(type) {
	case nil:
		return true
	case bool:
		return !t
	case float64:
		return t == 0
	case string:
		return t == ""
	case []any:
		return len(t) == 0
	case map[string]any:
		for _, x := range t {
			if !isZero(x) {
				return false
			}
		}
		return true
	}
	return false
}

func unmarshalOpt(raw json.RawMessage, v any) error {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	return json.Unmarshal(raw, v)
}

// str reads a JSON string, or an object's "Name" (RestartPolicy), as a string.
func str(raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var o struct{ Name string }
	if json.Unmarshal(raw, &o) == nil {
		return o.Name
	}
	return ""
}
