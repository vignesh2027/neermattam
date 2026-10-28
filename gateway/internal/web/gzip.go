package web

import (
	"bytes"
	"compress/gzip"
	"io/fs"
	"mime"
	"net/http"
	"path"
	"strings"
	"sync"
)

// AcceptsGzip reports whether the client takes gzip responses.
func AcceptsGzip(r *http.Request) bool {
	for _, part := range strings.Split(r.Header.Get("Accept-Encoding"), ",") {
		enc, q, _ := strings.Cut(strings.TrimSpace(part), ";")
		if strings.EqualFold(strings.TrimSpace(enc), "gzip") && strings.TrimSpace(q) != "q=0" {
			return true
		}
	}
	return false
}

// Compress gzips a body.
func Compress(b []byte) []byte {
	var buf bytes.Buffer
	zw, _ := gzip.NewWriterLevel(&buf, gzip.BestSpeed)
	_, _ = zw.Write(b)
	_ = zw.Close()
	return buf.Bytes()
}

// CompressBest gzips at the best ratio, for bodies made once and served
// many times.
func CompressBest(b []byte) []byte {
	var buf bytes.Buffer
	zw, _ := gzip.NewWriterLevel(&buf, gzip.BestCompression)
	_, _ = zw.Write(b)
	_ = zw.Close()
	return buf.Bytes()
}

// WriteBody writes body, gzipped when the client accepts it and the body is
// worth compressing. gz may be a precomputed compressed copy, or nil.
func WriteBody(w http.ResponseWriter, r *http.Request, body, gz []byte) {
	if w.Header().Get("Vary") == "" {
		w.Header().Set("Vary", "Accept-Encoding")
	}
	if len(body) > 1024 && AcceptsGzip(r) {
		if gz == nil {
			gz = Compress(body)
		}
		w.Header().Set("Content-Encoding", "gzip")
		w.Header().Del("Content-Length")
		_, _ = w.Write(gz)
		return
	}
	_, _ = w.Write(body)
}

// gzipStatic compresses each text asset once, on first request, and serves
// it from memory afterwards. MapLibre GL JS shrinks from about 920 KB to
// about a quarter of that.
type gzipStatic struct {
	fsys  fs.FS
	next  http.Handler
	mu    sync.Mutex
	cache map[string][]byte
}

func (g *gzipStatic) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	name := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
	ext := path.Ext(name)
	if (ext != ".js" && ext != ".css" && ext != ".svg" && ext != ".txt") || !AcceptsGzip(r) {
		g.next.ServeHTTP(w, r)
		return
	}
	g.mu.Lock()
	gz, ok := g.cache[name]
	g.mu.Unlock()
	if !ok {
		raw, err := fs.ReadFile(g.fsys, name)
		if err != nil {
			g.next.ServeHTTP(w, r)
			return
		}
		gz = Compress(raw)
		g.mu.Lock()
		g.cache[name] = gz
		g.mu.Unlock()
	}
	ct := mime.TypeByExtension(ext)
	if ext == ".txt" {
		ct = "text/plain; charset=utf-8"
	}
	w.Header().Set("Content-Type", ct)
	w.Header().Set("Content-Encoding", "gzip")
	w.Header().Add("Vary", "Accept-Encoding")
	_, _ = w.Write(gz)
}
