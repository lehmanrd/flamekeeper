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
	volumes    map[string]volumeInfo
}

func (f fakeInspector) Volume(_ context.Context, name string) (volumeInfo, error) {
	if v, ok := f.volumes[name]; ok {
		return v, nil
	}
	return volumeInfo{}, errNotFound
}

func (f fakeInspector) Container(_ context.Context, ref string) (containerInfo, error) {
	if l, ok := f.containers[ref]; ok {
		return containerInfo{ID: "id-" + ref, Name: ref, Labels: l}, nil
	}
	return containerInfo{}, errNotFound
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
		DataVolume:   "flamekeeper-data",
		GameRepos:    []string{"mornedhels/enshrouded-server"},
		HelperImages: []string{"busybox:stable", "steamcmd/steamcmd:latest"},
	}
	insp := fakeInspector{
		containers: map[string]map[string]string{
			"flamekeeper-embervale": world,
			"helper1":               {labelHelper: "true"},
			"controller":            {labelRole: "controller"},
			"old-server":            {"com.docker.compose.project": "enshrouded"},
			"other-server":          {},
		},
		networks: map[string]string{"net1": "flamekeeper", "net2": "bridge"},
		volumes: map[string]volumeInfo{"flamekeeper-data": {Driver: "local",
			Options: map[string]string{"type": "none", "o": "bind", "device": data}}},
	}
	return NewPolicy(cfg, insp), data
}

func writeGrant(t *testing.T, p *Policy, until time.Time, paths, containers []string) {
	t.Helper()
	b, _ := json.Marshal(importGrant{Until: until.Unix(), Paths: paths, Containers: containers})
	if err := os.WriteFile(filepath.Join(p.cfg.StateDir, "import.json"), b, 0o644); err != nil {
		t.Fatal(err)
	}
}

// grantImport: the admin allowed importing old-server and /srv/old-server for an hour.
func grantImport(t *testing.T, p *Policy) {
	writeGrant(t, p, time.Now().Add(time.Hour), []string{"/srv/old-server"}, []string{"old-server"})
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
	create := func(b []byte) Decision { return decide(p, "POST", "/v1.45/containers/create", "name=x", b) }

	expect(t, create(body), true, "")
	// The old shape (a bind of worlds/<slug>/game) can be swapped for a symlink.
	expect(t, create(fixture(t, "world_bind", data)), false, "Binds")
	expect(t, decide(p, "POST", "/v1.44/containers/create", "", body), false, "API 1.45")
	expect(t, decide(p, "POST", "/containers/create", "", body), true, "")
	mount := func(f func(mt map[string]any)) []byte {
		return edit(t, body, func(m map[string]any) { f(hostConfig(m)["Mounts"].([]any)[0].(map[string]any)) })
	}
	sub := func(s string) func(map[string]any) {
		return func(mt map[string]any) { mt["VolumeOptions"] = map[string]any{"Subpath": s} }
	}
	for _, bad := range []string{"", "worlds/embervale", "worlds/../game", "worlds/embervale/game/..",
		"/worlds/embervale/game", "worlds/Embervale/game", "imports/x/game", "worlds/a/b/game"} {
		expect(t, create(mount(sub(bad))), false, "subpath")
	}
	expect(t, create(mount(func(mt map[string]any) { mt["Source"] = "other" })), false, "volume")
	expect(t, create(mount(func(mt map[string]any) { mt["Type"] = "bind"; mt["Source"] = data })), false, "volume")
	expect(t, create(mount(func(mt map[string]any) {
		mt["VolumeOptions"] = map[string]any{"Subpath": "worlds/embervale/game",
			"DriverConfig": map[string]any{"Name": "local", "Options": map[string]any{"device": "/"}}}
	})), false, "DriverConfig")
	expect(t, create(edit(t, body, func(m map[string]any) {
		hc := hostConfig(m)
		hc["Mounts"] = append(hc["Mounts"].([]any), map[string]any{"Type": "volume", "Source": "flamekeeper-data", "Target": "/x"})
	})), false, "exactly one")
	expect(t, create(edit(t, body, func(m map[string]any) { delete(hostConfig(m), "Mounts") })), false, "exactly one")

	// The volume must exist and be DATA_DIR (Docker would create an empty one by that name).
	insp := p.insp.(fakeInspector)
	good := insp.volumes["flamekeeper-data"]
	insp.volumes["flamekeeper-data"] = volumeInfo{Driver: "local", Options: map[string]string{"type": "none", "o": "bind", "device": "/"}}
	expect(t, create(body), false, "isn't bound")
	insp.volumes["flamekeeper-data"] = volumeInfo{Driver: "local"}
	expect(t, create(body), false, "isn't bound")
	delete(insp.volumes, "flamekeeper-data")
	expect(t, create(body), false, "doesn't exist")
	insp.volumes["flamekeeper-data"] = good

	cases := []struct {
		name, reason string
		f            func(m map[string]any)
	}{
		{"privileged", "Privileged", func(m map[string]any) { hostConfig(m)["Privileged"] = true }},
		{"cap add", "CapAdd", func(m map[string]any) { hostConfig(m)["CapAdd"] = []string{"SYS_ADMIN"} }},
		{"pid host", "PidMode", func(m map[string]any) { hostConfig(m)["PidMode"] = "host" }},
		{"security opt", "SecurityOpt", func(m map[string]any) { hostConfig(m)["SecurityOpt"] = []string{"seccomp=unconfined"} }},
		{"mounts", "volume", func(m map[string]any) { hostConfig(m)["Mounts"] = []any{map[string]any{"Type": "bind", "Source": "/"}} }},
		{"host network", "network mode", func(m map[string]any) { hostConfig(m)["NetworkMode"] = "host" }},
		{"container network", "network mode", func(m map[string]any) { hostConfig(m)["NetworkMode"] = "container:x" }},
		{"other image", "game image", func(m map[string]any) { m["Image"] = "alpine:latest" }},
		{"no world id", "world_id", func(m map[string]any) { delete(m["Labels"].(map[string]any), labelWorldID) }},
		{"a bind as well", "Binds", func(m map[string]any) { hostConfig(m)["Binds"] = []string{"/:/host"} }},
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
			hc["Binds"] = []any{}
		})
		expect(t, create(b), true, "")
	})
}

func TestVolumeCreateAndRemove(t *testing.T) {
	p, data := newTestPolicy(t)
	vol := func(name, device string) []byte {
		return []byte(fmt.Sprintf(`{"Name": %q, "Driver": "local", "DriverOpts": {"type": "none", "o": "bind", "device": %q}, "Labels": {"flamekeeper.managed": "true"}}`, name, device))
	}
	expect(t, decide(p, "POST", "/volumes/create", "", vol("flamekeeper-data", data)), true, "")
	expect(t, decide(p, "POST", "/volumes/create", "", vol("flamekeeper-data", "/")), false, "only")
	expect(t, decide(p, "POST", "/volumes/create", "", vol("flamekeeper-data", data+"/worlds")), false, "only")
	expect(t, decide(p, "POST", "/volumes/create", "", vol("other", data)), false, "only")
	expect(t, decide(p, "POST", "/volumes/create", "", []byte(`{"Name": "flamekeeper-data"}`)), false, "only")
	grantImport(t, p)
	expect(t, decide(p, "DELETE", "/volumes/flamekeeper-data", "", nil), false, "never removed")
	expect(t, decide(p, "DELETE", "/volumes/old-data", "", nil), true, "")
}

func TestCreateHelper(t *testing.T) {
	p, data := newTestPolicy(t)
	create := func(b []byte) Decision { return decide(p, "POST", "/containers/create", "", b) }

	expect(t, create(fixture(t, "helper_ports", data)), true, "")
	expect(t, create(fixture(t, "helper_data", data)), true, "")
	expect(t, create(fixture(t, "helper_steamcmd", data)), true, "")
	expect(t, create(fixture(t, "helper_ntsync", data)), true, "")

	// the shapes step 2 removed stay refused
	expect(t, create(fixture(t, "helper_dev", data)), false, "only DATA_DIR")
	expect(t, create(fixture(t, "helper_hostroot", data)), false, "only DATA_DIR")
	expect(t, create(fixture(t, "helper_volumes_from", data)), false, ":ro")

	// import helpers need a grant
	expect(t, create(fixture(t, "helper_hostmount", data)), false, "import grant")
	expect(t, create(fixture(t, "helper_volumes_from_ro", data)), false, "import grant")

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
	expect(t, create(edit(t, fixture(t, "helper_ntsync", data), func(m map[string]any) {
		hostConfig(m)["Devices"] = []any{map[string]any{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda"}}
	})), false, "device")

	t.Run("with a grant", func(t *testing.T) {
		grantImport(t, p)
		for _, f := range []string{"helper_hostmount", "helper_volumes_from_ro"} {
			d := create(fixture(t, f, data))
			expect(t, d, true, "")
			if d.Rule != "import-create-helper" {
				t.Fatalf("%s: rule %q, want import-create-helper", f, d.Rule)
			}
		}
		// still not: other containers, writable volumes_from, other folders
		vf := fixture(t, "helper_volumes_from_ro", data)
		expect(t, create(edit(t, vf, func(m map[string]any) {
			hostConfig(m)["VolumesFrom"] = []string{"flamekeeper-embervale:ro"}
		})), false, "import grant")
		expect(t, create(fixture(t, "helper_volumes_from", data)), false, ":ro")
		expect(t, create(fixture(t, "helper_dev", data)), false, "only DATA_DIR")
		expect(t, create(fixture(t, "helper_hostroot", data)), false, "only DATA_DIR")
	})
}

func TestHelperMounts(t *testing.T) {
	p, data := newTestPolicy(t)
	writeGrant(t, p, time.Now().Add(time.Hour),
		[]string{"/srv/old-server", "/", "/var", "/run/user/1000/srv"}, []string{"old-server"})
	body := fixture(t, "helper_hostmount", data)
	mount := func(f func(mt map[string]any)) Decision {
		return decide(p, "POST", "/containers/create", "", edit(t, body, func(m map[string]any) {
			mt := hostConfig(m)["Mounts"].([]any)[0].(map[string]any)
			f(mt)
		}))
	}
	src := func(s string) func(map[string]any) { return func(mt map[string]any) { mt["Source"] = s } }

	expect(t, mount(func(map[string]any) {}), true, "")
	expect(t, mount(src(data)), true, "") // DATA_DIR itself needs no grant
	// A folder in DATA_DIR could be swapped for a symlink; one above it holds DATA_DIR.
	expect(t, mount(src(data+"/imports/abc")), false, "not a folder in it")
	expect(t, mount(src(filepath.Dir(data))), false, "not a folder in it")
	// granted, but these hold sockets or contain folders that do
	expect(t, mount(src("/")), false, "above it") // also holds DATA_DIR
	for _, s := range []string{"/var", "/run/user/1000/srv"} {
		expect(t, mount(src(s)), false, "never mounted")
	}
	// only the granted folder itself: a subfolder could be a symlink on the host
	expect(t, mount(src("/srv/old-server/link")), false, "import grant")
	expect(t, mount(src("/srv/old-server/../../run")), false, "clean")
	expect(t, mount(func(mt map[string]any) { mt["ReadOnly"] = false }), false, "read-only")
	expect(t, mount(func(mt map[string]any) { mt["Type"] = "volume" }), false, "read-only")
	expect(t, mount(func(mt map[string]any) {
		mt["BindOptions"] = map[string]any{"CreateMountpoint": true}
	}), false, "CreateMountpoint")
	expect(t, mount(func(mt map[string]any) {
		mt["BindOptions"] = map[string]any{"Propagation": "rshared"}
	}), false, "propagation")
	expect(t, mount(func(mt map[string]any) {
		mt["VolumeOptions"] = map[string]any{"DriverConfig": map[string]any{"Name": "local"}}
	}), false, "VolumeOptions")

	// Writable binds: DATA_DIR itself only.
	bind := func(b string) Decision {
		return decide(p, "POST", "/containers/create", "", edit(t, body, func(m map[string]any) {
			hostConfig(m)["Binds"] = []string{b}
		}))
	}
	expect(t, bind(data+":/data"), true, "")
	expect(t, bind(data+":/data:ro"), true, "")
	expect(t, bind(data+"/imports/abc:/out"), false, "only DATA_DIR")
	expect(t, bind(data+"/:/data"), false, "only DATA_DIR")
	expect(t, bind(filepath.Dir(data)+":/up"), false, "only DATA_DIR")
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
	expect(t, decide(p, "POST", "/containers/old-server/stop", "", nil), false, "import grant")
	expect(t, decide(p, "GET", "/containers/old-server/archive", "path=/x", nil), false, "import grant")
	expect(t, decide(p, "GET", "/containers/old-server/json", "", nil), true, "")
	expect(t, decide(p, "GET", "/containers/json", "all=1", nil), true, "")
	expect(t, decide(p, "PUT", "/containers/flamekeeper-embervale/archive", "path=/", nil), false, "")
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/attach", "", nil), false, "")
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/commit", "", nil), false, "")

	update := []byte(`{"RestartPolicy":{"Name":"no"}}`)
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/update", "", update), true, "")
	expect(t, decide(p, "POST", "/containers/old-server/update", "", update), false, "import grant")
	expect(t, decide(p, "POST", "/containers/flamekeeper-embervale/update", "",
		[]byte(`{"RestartPolicy":{"Name":"no"},"Devices":[{"PathOnHost":"/dev/sda"}]}`)), false, "Devices")

	grantImport(t, p)
	expect(t, decide(p, "POST", "/containers/old-server/stop", "", nil), true, "")
	expect(t, decide(p, "POST", "/containers/id-old-server/stop", "", nil), true, "") // by ID
	expect(t, decide(p, "POST", "/containers/old-server/update", "", update), true, "")
	expect(t, decide(p, "DELETE", "/volumes/old-data", "", nil), true, "")
	// the grant names one container, not all of them
	expect(t, decide(p, "POST", "/containers/other-server/stop", "", nil), false, "import grant")
}

func TestImportGrantExpires(t *testing.T) {
	p, _ := newTestPolicy(t)
	writeGrant(t, p, time.Now().Add(-time.Minute), []string{"/srv/old-server"}, []string{"old-server"})
	expect(t, decide(p, "POST", "/containers/old-server/stop", "", nil), false, "import grant")
	expect(t, decide(p, "DELETE", "/volumes/old-data", "", nil), false, "import grant")
	if err := os.WriteFile(filepath.Join(p.cfg.StateDir, "import.json"), []byte("garbage"), 0o644); err != nil {
		t.Fatal(err)
	}
	expect(t, decide(p, "POST", "/containers/old-server/stop", "", nil), false, "import grant")
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
