package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
)

const maxBody = 4 << 20

func unixTransport(sock string) *http.Transport {
	return &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", sock)
		},
		DisableCompression: true,
	}
}

// dockerInspector asks the real daemon about containers and networks.
type dockerInspector struct{ client *http.Client }

func (d dockerInspector) get(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, "GET", "http://docker"+path, nil)
	if err != nil {
		return err
	}
	res, err := d.client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode == http.StatusNotFound {
		return errNotFound
	}
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("docker answered %s", res.Status)
	}
	return json.NewDecoder(res.Body).Decode(out)
}

func (d dockerInspector) ContainerLabels(ctx context.Context, id string) (map[string]string, error) {
	var c struct {
		Config struct{ Labels map[string]string }
	}
	if err := d.get(ctx, "/containers/"+url.PathEscape(id)+"/json", &c); err != nil {
		return nil, err
	}
	if c.Config.Labels == nil {
		return map[string]string{}, nil
	}
	return c.Config.Labels, nil
}

func (d dockerInspector) NetworkName(ctx context.Context, id string) (string, error) {
	var n struct{ Name string }
	if err := d.get(ctx, "/networks/"+url.PathEscape(id), &n); err != nil {
		return "", err
	}
	return n.Name, nil
}

type Server struct {
	cfg    Config
	policy *Policy
	proxy  *httputil.ReverseProxy
	log    *slog.Logger
}

func NewServer(cfg Config, policy *Policy, upstream http.RoundTripper, log *slog.Logger) *Server {
	return &Server{
		cfg:    cfg,
		policy: policy,
		log:    log,
		proxy: &httputil.ReverseProxy{
			Rewrite: func(pr *httputil.ProxyRequest) {
				pr.Out.URL.Scheme = "http"
				pr.Out.URL.Host = "docker"
				pr.Out.Host = "docker"
			},
			Transport:     upstream,
			FlushInterval: -1, // logs, stats and pull progress stream
			ErrorLog:      slog.NewLogLogger(log.Handler(), slog.LevelError),
		},
	}
}

// needsBody: the requests whose JSON body the policy looks at.
func needsBody(method, path string) bool {
	if method != "POST" {
		return false
	}
	p := versionPrefix.ReplaceAllString(path, "/")
	return p == "/containers/create" || p == "/networks/create" ||
		strings.HasSuffix(p, "/exec") || strings.HasSuffix(p, "/update") ||
		(strings.HasPrefix(p, "/networks/") && strings.HasSuffix(p, "/connect"))
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var body []byte
	var d Decision
	if needsBody(r.Method, r.URL.Path) {
		var err error
		body, err = io.ReadAll(io.LimitReader(r.Body, maxBody+1))
		r.Body.Close()
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
		if len(body) > maxBody {
			d = deny("body", "request body too large")
		}
	}
	if d.Rule == "" {
		d = s.policy.Decide(r.Context(), r.Method, r.URL.Path, r.URL.Query(), body)
	}

	switch {
	case d.Allow:
		if s.cfg.LogAllowed {
			s.log.Info("allow", "method", r.Method, "path", r.URL.Path, "rule", d.Rule)
		}
	case s.cfg.Mode == ModeEnforce:
		s.log.Warn("deny", "method", r.Method, "path", r.URL.Path, "rule", d.Rule, "reason", d.Reason)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		json.NewEncoder(w).Encode(map[string]string{"message": "FlameKeeper docker proxy: " + d.Reason})
		return
	default:
		s.log.Warn("would-deny", "method", r.Method, "path", r.URL.Path, "rule", d.Rule, "reason", d.Reason)
	}
	s.proxy.ServeHTTP(w, r)
}
