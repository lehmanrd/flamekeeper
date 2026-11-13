package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeDocker answers like dockerd: JSON for most requests, and a raw stream after a
// 101 for exec start (Connection: Upgrade, Upgrade: tcp).
func fakeDocker(t *testing.T) string {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "docker.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/containers/{id}/json", func(w http.ResponseWriter, r *http.Request) {
		labels := map[string]string{}
		if r.PathValue("id") == "managed" {
			labels = world
		}
		json.NewEncoder(w).Encode(map[string]any{"Id": "id-" + r.PathValue("id"), "Name": "/" + r.PathValue("id"),
			"Config": map[string]any{"Labels": labels}})
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		json.NewEncoder(w).Encode(map[string]string{"path": r.URL.Path, "body": string(body)})
	})
	mux.HandleFunc("/{version}/exec/{id}/start", func(w http.ResponseWriter, r *http.Request) {
		conn, buf, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.Close()
		buf.WriteString("HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n" +
			"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\nhello from exec")
		buf.Flush()
	})
	srv := &http.Server{Handler: mux}
	go srv.Serve(ln)
	t.Cleanup(func() { srv.Close() })
	return sock
}

// grantPolicy is the policy behind the last startProxy, to write grants for it.
var grantPolicy *Policy

func startProxy(t *testing.T, mode string) *http.Client {
	t.Helper()
	upstream := fakeDocker(t)
	p, _ := newTestPolicy(t)
	grantPolicy = p
	p.cfg.Mode = mode
	transport := unixTransport(upstream)
	p.insp = dockerInspector{client: &http.Client{Transport: transport}}
	var logs bytes.Buffer
	srv := NewServer(p.cfg, p, transport, slog.New(slog.NewJSONHandler(&logs, nil)))

	sock := filepath.Join(t.TempDir(), "proxy.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	hs := &http.Server{Handler: srv}
	go hs.Serve(ln)
	t.Cleanup(func() {
		hs.Close()
		if t.Failed() {
			t.Log(logs.String())
		}
	})
	return &http.Client{Transport: unixTransport(sock), Timeout: 5 * time.Second}
}

func TestServerEnforce(t *testing.T) {
	c := startProxy(t, ModeEnforce)

	res, err := c.Post("http://docker/v1.47/containers/create", "application/json",
		strings.NewReader(`{"Image":"alpine","HostConfig":{"Privileged":true}}`))
	if err != nil {
		t.Fatal(err)
	}
	var e struct{ Message string }
	json.NewDecoder(res.Body).Decode(&e)
	res.Body.Close()
	if res.StatusCode != 403 || !strings.Contains(e.Message, "FlameKeeper docker proxy") {
		t.Fatalf("got %d %q, want 403 with a message", res.StatusCode, e.Message)
	}

	// allowed: the body still reaches Docker after the proxy has read it
	res, err = c.Post("http://docker/v1.47/containers/managed/update", "application/json",
		strings.NewReader(`{"RestartPolicy":{"Name":"no"}}`))
	if err != nil {
		t.Fatal(err)
	}
	var echo struct{ Path, Body string }
	json.NewDecoder(res.Body).Decode(&echo)
	res.Body.Close()
	if res.StatusCode != 200 || echo.Body != `{"RestartPolicy":{"Name":"no"}}` {
		t.Fatalf("got %d %+v", res.StatusCode, echo)
	}

	res, err = c.Post("http://docker/containers/unmanaged/stop", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != 403 {
		t.Fatalf("stop of an unmanaged container: got %d, want 403", res.StatusCode)
	}
}

func TestServerAudit(t *testing.T) {
	c := startProxy(t, ModeAudit)
	res, err := c.Post("http://docker/containers/create", "application/json",
		strings.NewReader(`{"Image":"alpine","HostConfig":{"Privileged":true}}`))
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != 200 {
		t.Fatalf("audit mode should forward, got %d", res.StatusCode)
	}
}

func TestServerUpgrade(t *testing.T) {
	c := startProxy(t, ModeEnforce)
	conn, err := c.Transport.(*http.Transport).DialContext(context.Background(), "unix", "")
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(5 * time.Second))
	io.WriteString(conn, "POST /v1.47/exec/abc/start HTTP/1.1\r\nHost: docker\r\nContent-Type: application/json\r\n"+
		"Connection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 2\r\n\r\n{}")
	r := bufio.NewReader(conn)
	res, err := http.ReadResponse(r, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 101 {
		t.Fatalf("got %d, want 101", res.StatusCode)
	}
	rest, _ := io.ReadAll(r)
	if string(rest) != "hello from exec" {
		t.Fatalf("stream after upgrade: %q", rest)
	}
}

func TestServerGrantEndpoint(t *testing.T) {
	c := startProxy(t, ModeEnforce)
	get := func() map[string]any {
		res, err := c.Get("http://docker/v1.47/_fk/import-grant")
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		var m map[string]any
		json.NewDecoder(res.Body).Decode(&m)
		return m
	}
	if g := get(); g["active"] != false || len(g["paths"].([]any)) != 0 {
		t.Fatalf("no grant: %v", g)
	}
	grantImport(t, grantPolicy)
	if g := get(); g["active"] != true || g["containers"].([]any)[0] != "old-server" ||
		g["paths"].([]any)[0] != "/srv/old-server" {
		t.Fatalf("with a grant: %v", g)
	}
}
