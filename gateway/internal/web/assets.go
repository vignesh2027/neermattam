package web

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io/fs"
	"mime"
	"net/http"
	"path"
	"strings"
	"sync"
)

// asset is one static file held in memory, with its gzip form for text.
type asset struct {
	rel   string
	body  []byte
	gz    []byte
	ctype string
	etag  string
}

// Assets fingerprints every static file at start-up: the page refers to
// /static/brand/mark-64.3f2a91c0.png, which can be cached for a year
// because a changed file gets a new name. The plain name still works, with
// a short cache, for links from outside (the Open Graph card, favicon.ico).
type Assets struct {
	byRel  map[string]*asset // brand/mark-64.png
	byName map[string]*asset // brand/mark-64.3f2a91c0.png
	url    map[string]string // rel to fingerprinted URL
}

var (
	assetsOnce sync.Once
	assetsInst *Assets
	assetsErr  error
)

// LoadAssets reads and fingerprints the embedded static files once.
func LoadAssets() (*Assets, error) {
	assetsOnce.Do(func() { assetsInst, assetsErr = loadAssets() })
	return assetsInst, assetsErr
}

// fingerprint returns the file's short content hash and its hashed name.
func fingerprint(rel string, body []byte) (string, string) {
	sum := sha256.Sum256(body)
	h := hex.EncodeToString(sum[:4])
	ext := path.Ext(rel)
	return h, strings.TrimSuffix(rel, ext) + "." + h + ext
}

func loadAssets() (*Assets, error) {
	a := &Assets{byRel: map[string]*asset{}, byName: map[string]*asset{}, url: map[string]string{}}
	sub, err := fs.Sub(staticFS, "static")
	if err != nil {
		return nil, err
	}
	var css []string
	err = fs.WalkDir(sub, ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		if strings.HasSuffix(p, ".css") {
			css = append(css, p) // after the files they refer to
			return nil
		}
		body, err := fs.ReadFile(sub, p)
		if err != nil {
			return err
		}
		a.add(p, body)
		return nil
	})
	if err != nil {
		return nil, err
	}
	for _, p := range css {
		body, err := fs.ReadFile(sub, p)
		if err != nil {
			return nil, err
		}
		// Point the stylesheet at fingerprinted fonts and images too.
		for rel, url := range a.url {
			body = bytes.ReplaceAll(body, []byte("/static/"+rel+"\""), []byte(url+"\""))
			body = bytes.ReplaceAll(body, []byte("/static/"+rel+")"), []byte(url+")"))
		}
		a.add(p, body)
	}
	return a, nil
}

func (a *Assets) add(rel string, body []byte) {
	ext := path.Ext(rel)
	ctype := mime.TypeByExtension(ext)
	switch ext {
	case ".ico":
		ctype = "image/x-icon"
	case ".txt":
		ctype = "text/plain; charset=utf-8"
	case ".woff2":
		ctype = "font/woff2"
	}
	hash, name := fingerprint(rel, body)
	as := &asset{rel: rel, body: body, ctype: ctype, etag: `"` + hash + `"`}
	switch ext {
	case ".js", ".css", ".svg", ".txt", ".json", ".ico":
		as.gz = CompressBest(body)
	}
	a.byRel[rel] = as
	a.byName[name] = as
	a.url[rel] = "/static/" + name
}

// URL returns the fingerprinted URL of a static file, or the plain one
// when the file is unknown (so a typo shows up as a 404, not a panic).
func (a *Assets) URL(rel string) string {
	if u, ok := a.url[rel]; ok {
		return u
	}
	return "/static/" + rel
}

// Handler serves /static/.
func (a *Assets) Handler() http.Handler {
	return http.StripPrefix("/static/", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		as, fingerprinted := a.byName[name]
		if !fingerprinted {
			as = a.byRel[name]
		}
		if as == nil {
			http.NotFound(w, r)
			return
		}
		h := w.Header()
		if fingerprinted {
			h.Set("Cache-Control", "public, max-age=31536000, immutable")
		} else {
			h.Set("Cache-Control", "public, max-age=300")
		}
		h.Set("ETag", as.etag)
		if as.ctype != "" {
			h.Set("Content-Type", as.ctype)
		}
		if as.gz != nil {
			AddVary(h, "Accept-Encoding")
		}
		if r.Header.Get("If-None-Match") == as.etag {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		if as.gz != nil && AcceptsGzip(r) {
			h.Set("Content-Encoding", "gzip")
			_, _ = w.Write(as.gz)
			return
		}
		_, _ = w.Write(as.body)
	}))
}
