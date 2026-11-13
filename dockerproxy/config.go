package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const (
	ModeAudit   = "audit"
	ModeEnforce = "enforce"
)

// Config comes from environment variables (see docs/socket-proxy.md).
type Config struct {
	Listen       string   // FK_PROXY_LISTEN: unix socket FlameKeeper talks to
	Upstream     string   // FK_PROXY_UPSTREAM: the real Docker socket
	Mode         string   // FK_PROXY_MODE: audit | enforce
	DataDir      string   // DATA_DIR: mounted read-only at the same path
	StateDir     string   // FK_PROXY_STATE_DIR: admin-only; holds import-until
	Network      string   // FK_NETWORK: the bridge shared by worlds and the controller
	GameRepos    []string // FK_GAME_REPOS: world images (any tag)
	HelperImages []string // FK_HELPER_IMAGES: throwaway helper images (exact ref)
	LogAllowed   bool     // FK_PROXY_LOG_ALLOWED=1: log allowed requests too
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

func list(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func LoadConfig() (Config, error) {
	c := Config{
		Listen:       env("FK_PROXY_LISTEN", "/run/fk/docker.sock"),
		Upstream:     env("FK_PROXY_UPSTREAM", "/var/run/docker.sock"),
		Mode:         env("FK_PROXY_MODE", ModeAudit),
		DataDir:      env("DATA_DIR", "/srv/flamekeeper"),
		StateDir:     env("FK_PROXY_STATE_DIR", "/var/lib/fk-dockerproxy"),
		Network:      env("FK_NETWORK", "flamekeeper"),
		GameRepos:    list(env("FK_GAME_REPOS", "mornedhels/enshrouded-server")),
		HelperImages: list(env("FK_HELPER_IMAGES", "busybox:stable,steamcmd/steamcmd:latest")),
		LogAllowed:   env("FK_PROXY_LOG_ALLOWED", "") == "1",
	}
	if c.Mode != ModeAudit && c.Mode != ModeEnforce {
		return c, fmt.Errorf("FK_PROXY_MODE must be %q or %q, not %q", ModeAudit, ModeEnforce, c.Mode)
	}
	if !filepath.IsAbs(c.DataDir) || filepath.Clean(c.DataDir) != c.DataDir || c.DataDir == "/" {
		return c, fmt.Errorf("DATA_DIR must be a clean absolute path other than /, not %q", c.DataDir)
	}
	return c, nil
}

// CheckDataDir makes sure the proxy can see DATA_DIR. Without it every bind path
// would look nonexistent and the symlink check would pass vacuously.
func (c Config) CheckDataDir() error {
	fi, err := os.Stat(c.DataDir)
	if err != nil {
		return fmt.Errorf("DATA_DIR %s isn't visible to the proxy (mount it read-only at the same path): %w", c.DataDir, err)
	}
	if !fi.IsDir() {
		return fmt.Errorf("DATA_DIR %s isn't a directory", c.DataDir)
	}
	return nil
}
