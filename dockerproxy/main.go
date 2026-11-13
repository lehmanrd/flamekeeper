// fk-dockerproxy sits between FlameKeeper and the Docker socket and only lets through
// what FlameKeeper needs. See docs/socket-proxy.md.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("fatal", "error", err.Error())
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	cfg, err := LoadConfig()
	if err != nil {
		return err
	}

	upstream := unixTransport(cfg.Upstream)
	insp := dockerInspector{client: &http.Client{Transport: upstream, Timeout: 10 * time.Second}}
	srv := &http.Server{Handler: NewServer(cfg, NewPolicy(cfg, insp), upstream, log)}

	if err := os.Remove(cfg.Listen); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	ln, err := net.Listen("unix", cfg.Listen)
	if err != nil {
		return err
	}
	if err := os.Chmod(cfg.Listen, 0o660); err != nil {
		return err
	}
	log.Info("listening", "socket", cfg.Listen, "upstream", cfg.Upstream, "mode", cfg.Mode,
		"data_dir", cfg.DataDir)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(shutdown)
	}()
	if err := srv.Serve(ln); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
