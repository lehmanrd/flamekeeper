package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// testdata/*.json are request bodies built by the Docker SDK exactly as FlameKeeper
// builds them, with /DATA standing for DATA_DIR.

type fakeInspector struct {
	containers map[string]map[string]string
	networks   map[string]string
}

func (f fakeInspector) ContainerLabels(_ context.Context, id string) (map[string]string, error) {
	if l, ok := f.containers[id]; ok {
		return l, nil
	}
	return nil, errNotFound
}

func (f fakeInspector) NetworkName(_ context.Context, id string) (string, error) {
	if n, ok := f.networks[id]; ok {
		return n, nil
	}
	return "", errNotFound
}

var world = map[string]string{labelManaged: "true", labelWorldID: "w1"}

func newTestPolicy(t *testing.T) (*Policy, string) {
	t.Helper()
	tmp := t.TempDir()
	data := filepath.Join(tmp, "data")
	state := filepath.Join(tmp, "state")
	for _, d := range []string{filepath.Join(data, "worlds", "embervale", "game"), state} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	cfg := Config{
		Mode: ModeEnforce, DataDir: data, StateDir: state, Network: "flamekeeper",
		GameRepos:    []string{"mornedhels/enshrouded-server"},
		HelperImages: []string{"busybox:stable", "steamcmd/steamcmd:latest"},
	}
	insp := fakeInspector{
		containers: map[string]map[string]string{
			"flamekeeper-embervale": world,
			"helper1":               {labelHelper: "true"},
			"controller":            {labelRole: "controller"},
			"old-server":            {"com.docker.compose.project": "enshrouded"},
		},
		networks: map[string]string{"net1": "flamekeeper", "net2": "bridge"},
	}
	return NewPolicy(cfg, insp), data
}

func openImport(t *testing.T, p *Policy) {
	t.Helper()
	until := fmt.Sprint(time.Now().Add(time.Hour).Unix())
	if err := os.WriteFile(filepath.Join(p.cfg.StateDir, "import-until"), []byte(until), 0o644); err != nil {
		t.Fatal(err)
	}
}

func fixture(t *testing.T, name, data string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	return []byte(strings.ReplaceAll(string(b), "/DATA", data))
}

// edit decodes a fixture, lets f change it, and encodes it again.
func edit(t *testing.T, body []byte, f func(m map[string]any)) []byte {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(body, &m); err != nil {
		t.Fatal(err)
	}
	f(m)
	out, _ := json.Marshal(m)
	return out
}

func hostConfig(m map[string]any) map[string]any { return m["HostConfig"].(map[string]any) }

func decide(p *Policy, method, path, query string, body []byte) Decision {
	q, _ := url.ParseQuery(query)
	return p.Decide(context.Background(), method, path, q, body)
}

func expect(t *testing.T, d Decision, allowed bool, contains string) {
	t.Helper()
	if d.Allow != allowed {
		t.Fatalf("allow=%v, want %v (rule %s: %s)", d.Allow, allowed, d.Rule, d.Reason)
	}
	if contains != "" && !strings.Contains(d.Reason, contains) {
		t.Fatalf("reason %q doesn't mention %q", d.Reason, contains)
	}
}

func TestCreateWorld(t *testing.T) {
	p, data := newTestPolicy(t)
	body := fixture(t, "world", data)
	create := func(b []byte) Decision { return decide(p, "POST", "/v1.47/containers/create", "name=x", b) }

	expect(t, create(body), true, "")

	cases := []struct {
		name, reason string
		f            func(m map[string]any)
	}{
		{"privileged", "Privileged", func(m map[string]any) { hostConfig(m)["Privileged"] = true }},
		{"cap add", "CapAdd", func(m map[string]any) { hostConfig(m)["CapAdd"] = []string{"SYS_ADMIN"} }},
		{"pid host", "PidMode", func(m map[string]any) { hostConfig(m)["PidMode"] = "host" }},
		{"security opt", "SecurityOpt", func(m map[string]any) { hostConfig(m)["SecurityOpt"] = []string{"seccomp=unconfined"} }},
		{"mounts", "Mounts", func(m map[string]any) { hostConfig(m)["Mounts"] = []any{map[string]any{"Type": "bind", "Source": "/"}} }},
		{"host network", "network mode", func(m map[string]any) { hostConfig(m)["NetworkMode"] = "host" }},
		{"container network", "network mode", func(m map[string]any) { hostConfig(m)["NetworkMode"] = "container:x" }},
		{"other image", "game image", func(m map[string]any) { m["Image"] = "alpine:latest" }},
		{"no world id", "world_id", func(m map[string]any) { delete(m["Labels"].(map[string]any), labelWorldID) }},
		{"bind root", "outside", func(m map[string]any) { hostConfig(m)["Binds"] = []string{"/:/host"} }},
		{"bind data dir", "outside", func(m map[string]any) { hostConfig(m)["Binds"] = []string{data + ":/d"} }},
		{"bind dotdot", "clean", func(m map[string]any) {
			hostConfig(m)["Binds"] = []string{data + "/worlds/../../etc:/d"}
		}},
		{"named volume", "named volumes", func(m map[string]any) { hostConfig(m)["Binds"] = []string{"other:/d"} }},
		{"propagation", "option", func(m map[string]any) {
			hostConfig(m)["Binds"] = []string{data + "/worlds/embervale/game:/d:rshared"}
		}},
		{"other device", "device", func(m map[string]any) {
			hostConfig(m)["Devices"] = []any{map[string]any{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda", "CgroupPermissions": "rwm"}}
		}},
		{"tcp port", "UDP", func(m map[string]any) {
			hostConfig(m)["PortBindings"] = map[string]any{"22/tcp": []any{map[string]any{"HostPort": "22"}}}
		}},
		{"low port", "1024", func(m map[string]any) {
			hostConfig(m)["PortBindings"] = map[string]any{"53/udp": []any{map[string]any{"HostPort": "53"}}}
		}},
		{"endpoint", "network", func(m map[string]any) {
			m["NetworkingConfig"] = map[string]any{"EndpointsConfig": map[string]any{"host": map[string]any{}}}
		}},
		{"log driver", "log driver", func(m map[string]any) { hostConfig(m)["LogConfig"] = map[string]any{"Type": "syslog"} }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) { expect(t, create(edit(t, body, c.f)), false, c.reason) })
	}

	t.Run("zero values are fine", func(t *testing.T) {
		b := edit(t, body, func(m map[string]any) {
			hc := hostConfig(m)
			hc["Privileged"] = false
			hc["CapAdd"] = nil
			hc["PidMode"] = ""
			hc["Mounts"] = []any{}
		})
		expect(t, create(b), true, "")
	})
}

func TestBindSymlinks(t *testing.T) {
	p, data := newTestPolicy(t)
	body := fixture(t, "world", data)
	create := func(b []byte) Decision { return decide(p, "POST", "/containers/create", "", b) }

	// a symlink anywhere under DATA_DIR on the bind path
	if err := os.Symlink("/", filepath.Join(data, "worlds", "evil")); err != nil {
		t.Fatal(err)
	}
	b := edit(t, body, func(m map[string]any) { hostConfig(m)["Binds"] = []string{data + "/worlds/evil:/d"} })
	expect(t, create(b), false, "symlink")
	b = edit(t, body, func(m map[string]any) { hostConfig(m)["Binds"] = []string{data + "/worlds/evil/etc:/d"} })
	expect(t, create(b), false, "symlink")

	if err := os.Rename(filepath.Join(data, "worlds", "embervale", "game"), filepath.Join(data, "worlds", "embervale", "real")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("real", filepath.Join(data, "worlds", "embervale", "game")); err != nil {
		t.Fatal(err)
	}
	expect(t, create(body), false, "symlink")

	// a world folder that doesn't exist yet: Docker creates it
	b = edit(t, body, func(m map[string]any) { hostConfig(m)["Binds"] = []string{data + "/worlds/new/game:/d"} })
	expect(t, create(b), true, "")
}

func TestCreateHelper(t *testing.T) {
	p, data := newTestPolicy(t)
	create := func(b []byte) Decision { return decide(p, "POST", "/containers/create", "", b) }

	expect(t, create(fixture(t, "helper_ports", data)), true, "")
	expect(t, create(fixture(t, "helper_data", data)), true, "")
	expect(t, create(fixture(t, "helper_steamcmd", data)), true, "")

	// today's code paths that step 2 replaces
	expect(t, create(fixture(t, "helper_dev", data)), false, "outside")
	expect(t, create(fixture(t, "helper_hostroot", data)), false, "import window")
	expect(t, create(fixture(t, "helper_volumes_from", data)), false, ":ro")

	ports := fixture(t, "helper_ports", data)
	expect(t, create(edit(t, ports, func(m map[string]any) { m["Cmd"] = []string{"sh", "-c", "nc 127.0.0.1 22"} })),
		false, "host networking")
	expect(t, create(edit(t, ports, func(m map[string]any) {
		hostConfig(m)["Binds"] = []string{data + ":/d"}
	})), false, "host networking")
	expect(t, create(edit(t, ports, func(m map[string]any) { m["Image"] = "alpine:latest" })), false, "helper image")
	expect(t, create(edit(t, ports, func(m map[string]any) { m["Image"] = "busybox:latest" })), false, "helper image")
	expect(t, create(edit(t, fixture(t, "helper_data", data), func(m map[string]any) {
		hostConfig(m)["Privileged"] = true
	})), false, "Privileged")

	t.Run("import window", func(t *testing.T) {
		openImport(t, p)
		expect(t, create(fixture(t, "helper_hostroot", data)), true, "")
		vf := edit(t, fixture(t, "helper_volumes_from", data), func(m map[string]any) {
			hostConfig(m)["VolumesFrom"] = []string{"abc123:ro"}
		})
		expect(t, create(vf), true, "")
		// still read-only only
		expect(t, create(fixture(t, "helper_dev", data)), false, "outside")
	})
}

func TestCreateOther(t *testing.T) {
	p, data := newTestPolicy(t)
	expect(t, decide(p, "POST", "/containers/create", "", fixture(t, "self_update", data)), false, "self-update")
	expect(t, decide(p, "POST", "/containers/create", "", []byte(`{"Image":"alpine","HostConfig":{"Privileged":true}}`)),
		false, "only world containers")
	expect(t, decide(p, "POST", "/containers/create", "", []byte(`not json`)), false, "bad create body")
}

func TestContainerOps(t *testing.T) {
	p, _ := newTestPolicy(t)
	for _, id := range []string{"flamekeeper-embervale", "helper1", "missing"} {
		expect(t, decide(p, "POST", "/v1.47/containers/"+id+"/stop", "t=90", nil), true, "")
		expect(t, decide(p, "DELETE", "/containers/"+id, "force=1", nil), true, "")
		expect(t, decide(p, "GET", "/containers/"+id+"/logs", "follow=1", nil), true, "")
		expect(t, decide(p, "HEAD", "/containers/"+id+"/archive", "path=/x", nil), true, "")
	}
	expect(t, decide(p, "POST", "/containers/old-server/stop", "", nil), false, "import window")
	expect(t, decide(p, "GET", "/containers/old-server/archive", "path=/x", nil), false, "import window")
	expect(t, decide(p, "GET", "/containers/old-server/json", "", nil), true, "")
	expect(t, decide(p, "GET", "/containers/json", "all=1", nil), true, "")
	expect(t, decide(p, "PUT", "/containers/flamekeeper-embervale/archive", "path=/", nil), false, "")
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/attach", "", nil), false, "")
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/commit", "", nil), false, "")

	update := []byte(`{"RestartPolicy":{"Name":"no"}}`)
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/update", "", update), true, "")
	expect(t, decide(p, "POST", "/containers/old-server/update", "", update), false, "import window")
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/update", "",
		[]byte(`{"RestartPolicy":{"Name":"no"},"Devices":[{"PathOnHost":"/dev/sda"}]}`)), false, "Devices")

	openImport(t, p)
	expect(t, decide(p, "POST", "/containers/old-server/stop", "", nil), true, "")
	expect(t, decide(p, "POST", "/containers/old-server/update", "", update), true, "")
	expect(t, decide(p, "DELETE", "/volumes/old-data", "", nil), true, "")
}

func TestImportWindowExpires(t *testing.T) {
	p, _ := newTestPolicy(t)
	past := fmt.Sprint(time.Now().Add(-time.Minute).Unix())
	if err := os.WriteFile(filepath.Join(p.cfg.StateDir, "import-until"), []byte(past), 0o644); err != nil {
		t.Fatal(err)
	}
	expect(t, decide(p, "POST", "/containers/old-server/stop", "", nil), false, "import window")
	expect(t, decide(p, "DELETE", "/volumes/old-data", "", nil), false, "import window")
}

func TestExec(t *testing.T) {
	p, _ := newTestPolicy(t)
	ok := []byte(`{"Cmd":["supervisorctl","restart","enshrouded-server"],"AttachStdout":true,"Privileged":false,"User":""}`)
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/exec", "", ok), true, "")
	expect(t, decide(p, "POST", "/containers/old-server/exec", "", ok), false, "world containers")
	expect(t, decide(p, "POST", "/containers/helper1/exec", "", ok), false, "world containers")
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/exec", "",
		[]byte(`{"Cmd":["sh"],"Privileged":true}`)), false, "privileged")
	expect(t, decide(p, "POST", "/exec/abc/start", "", nil), true, "")
}

func TestNetworks(t *testing.T) {
	p, _ := newTestPolicy(t)
	expect(t, decide(p, "POST", "/networks/create", "",
		[]byte(`{"Name":"flamekeeper","Driver":"bridge","Labels":{"flamekeeper.managed":"true"},"Internal":false,"Options":null}`)), true, "")
	expect(t, decide(p, "POST", "/networks/create", "", []byte(`{"Name":"other","Driver":"bridge"}`)), false, "")
	expect(t, decide(p, "POST", "/networks/create", "", []byte(`{"Name":"flamekeeper","Driver":"macvlan"}`)), false, "")
	expect(t, decide(p, "POST", "/networks/create", "",
		[]byte(`{"Name":"flamekeeper","Options":{"parent":"eth0"}}`)), false, "Options")

	for _, c := range []string{"controller", "flamekeeper-embervale"} {
		expect(t, decide(p, "POST", "/networks/net1/connect", "", []byte(`{"Container":"`+c+`"}`)), true, "")
	}
	expect(t, decide(p, "POST", "/networks/net1/connect", "", []byte(`{"Container":"old-server"}`)), false, "")
	expect(t, decide(p, "POST", "/networks/net2/connect", "", []byte(`{"Container":"controller"}`)), false, "")
	expect(t, decide(p, "POST", "/networks/net1/disconnect", "", []byte(`{"Container":"controller"}`)), false, "")
}

func TestPull(t *testing.T) {
	p, _ := newTestPolicy(t)
	for _, q := range []string{
		"fromImage=mornedhels/enshrouded-server&tag=stable-wine",
		"fromImage=docker.io/mornedhels/enshrouded-server&tag=latest",
		"fromImage=busybox&tag=stable",
		"fromImage=docker.io/library/busybox&tag=stable",
		"fromImage=steamcmd/steamcmd&tag=latest",
		"fromImage=steamcmd/steamcmd",
	} {
		expect(t, decide(p, "POST", "/images/create", q, nil), true, "")
	}
	for _, q := range []string{
		"fromImage=alpine&tag=latest",
		"fromImage=busybox&tag=latest",
		"fromImage=evil.example/mornedhels/enshrouded-server&tag=latest",
		"fromSrc=-&repo=busybox&tag=stable",
	} {
		expect(t, decide(p, "POST", "/images/create", q, nil), false, "")
	}
}

func TestOtherEndpoints(t *testing.T) {
	p, _ := newTestPolicy(t)
	for _, r := range [][2]string{
		{"GET", "/_ping"}, {"HEAD", "/_ping"}, {"GET", "/v1.47/version"}, {"GET", "/info"},
		{"GET", "/images/mornedhels/enshrouded-server:stable-wine/json"},
		{"GET", "/distribution/mornedhels/enshrouded-server:latest/json"},
		{"GET", "/networks"}, {"GET", "/volumes/x"}, {"GET", "/exec/abc/json"},
	} {
		expect(t, decide(p, r[0], r[1], "", nil), true, "")
	}
	for _, r := range [][2]string{
		{"POST", "/build"}, {"POST", "/plugins/pull"}, {"POST", "/swarm/init"}, {"POST", "/secrets/create"},
		{"POST", "/volumes/create"}, {"DELETE", "/images/busybox"}, {"POST", "/session"}, {"GET", "/"},
	} {
		expect(t, decide(p, r[0], r[1], "", nil), false, "")
	}
}

func TestSplitRef(t *testing.T) {
	for in, want := range map[string]string{
		"busybox":                                "busybox latest",
		"busybox:stable":                         "busybox stable",
		"docker.io/library/busybox:stable":       "busybox stable",
		"localhost:5000/busybox":                 "localhost:5000/busybox latest",
		"mornedhels/enshrouded-server@sha256:ab": "mornedhels/enshrouded-server @sha256:ab",
	} {
		if r, tag := splitRef(in); r+" "+tag != want {
			t.Errorf("splitRef(%q) = %q %q, want %q", in, r, tag, want)
		}
	}
}
