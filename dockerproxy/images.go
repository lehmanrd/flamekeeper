package main

import "strings"

// splitRef normalizes an image reference to (repository, tag-or-digest):
// "docker.io/library/busybox" -> ("busybox", "latest").
func splitRef(ref string) (repo, tag string) {
	ref = strings.TrimSpace(ref)
	if i := strings.Index(ref, "@"); i >= 0 {
		repo, tag = ref[:i], ref[i:]
	} else {
		repo = ref
		slash := strings.LastIndex(repo, "/")
		if colon := strings.LastIndex(repo, ":"); colon > slash {
			repo, tag = repo[:colon], repo[colon+1:]
		}
	}
	for _, prefix := range []string{"docker.io/", "index.docker.io/", "registry-1.docker.io/"} {
		repo = strings.TrimPrefix(repo, prefix)
	}
	repo = strings.TrimPrefix(repo, "library/")
	if tag == "" {
		tag = "latest"
	}
	return strings.ToLower(repo), tag
}

func (p *Policy) gameImageAllowed(ref string) bool {
	repo, _ := splitRef(ref)
	for _, r := range p.cfg.GameRepos {
		if allowed, _ := splitRef(r); allowed == repo {
			return true
		}
	}
	return false
}

func (p *Policy) helperImageAllowed(ref string) bool {
	repo, tag := splitRef(ref)
	for _, h := range p.cfg.HelperImages {
		if hr, ht := splitRef(h); hr == repo && ht == tag {
			return true
		}
	}
	return false
}
