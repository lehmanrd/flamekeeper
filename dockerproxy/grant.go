package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// importGrant is what the host admin allows the importer to touch for a while:
// $FK_PROXY_STATE_DIR/import.json, written on the host (FlameKeeper can't write there).
//
//	{"until": 1795000000, "paths": ["/srv/enshrouded"], "containers": ["enshrouded"]}
//
// A read-only mount still lets a process connect to the Unix sockets in it (such as
// docker.sock), so "any path while the window is open" isn't safe: only these.
type importGrant struct {
	Until      int64    `json:"until"`
	Paths      []string `json:"paths"`
	Containers []string `json:"containers"`
}

// grant returns the current import grant, or nil when there is none or it expired.
func (p *Policy) grant() *importGrant {
	if p.cfg.StateDir == "" {
		return nil
	}
	b, err := os.ReadFile(filepath.Join(p.cfg.StateDir, "import.json"))
	if err != nil {
		return nil
	}
	var g importGrant
	if json.Unmarshal(b, &g) != nil || p.now().Unix() >= g.Until {
		return nil
	}
	return &g
}

// hasContainer: the grant names this container (by name or ID, or an ID prefix).
func (g *importGrant) hasContainer(c containerInfo) bool {
	for _, want := range g.Containers {
		want = strings.TrimPrefix(want, "/")
		if want != "" && (want == c.Name || strings.HasPrefix(c.ID, want)) {
			return true
		}
	}
	return false
}

// hasPath: src is exactly one of the granted folders. Not a subfolder: the proxy can't
// see the host outside DATA_DIR, and Docker follows a symlink in a mount source, so
// "/srv/old/link -> /run" would get past a prefix check. Symlinks inside a mounted
// folder resolve inside the helper, which is harmless.
func (g *importGrant) hasPath(src string) bool {
	return slices.Contains(g.Paths, src)
}

// Host paths never mounted into a helper, even when granted: they hold the Docker socket,
// other sockets, kernel interfaces or other containers' files. A path is refused if it is
// one of these, inside one, or above one (/ and /var contain them).
var forbiddenHostPaths = []string{
	"/run", "/var/run", "/var/lock", "/proc", "/sys", "/dev", "/var/lib/docker",
	"/var/lib/containerd", "/etc", "/root", "/boot",
}

func forbiddenHostPath(src string) (string, bool) {
	for _, f := range forbiddenHostPaths {
		if src == f || strings.HasPrefix(src, f+"/") || strings.HasPrefix(f, strings.TrimSuffix(src, "/")+"/") {
			return f, true
		}
	}
	return "", false
}

// grantedContainer resolves a container reference and checks the grant names it.
func (p *Policy) grantedContainer(ctx context.Context, g *importGrant, ref string) bool {
	if g == nil {
		return false
	}
	c, err := p.insp.Container(ctx, ref)
	return err == nil && !managed(c.Labels) && g.hasContainer(c)
}
