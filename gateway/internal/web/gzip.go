package web

import (
	"bytes"
	"compress/gzip"
	"net/http"
	"strings"
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

// AddVary adds a field to Vary once, keeping what is already there.
func AddVary(h http.Header, field string) {
	for _, v := range h.Values("Vary") {
		for _, f := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(f), field) {
				return
			}
		}
	}
	h.Add("Vary", field)
}

// WriteBody writes body, gzipped when the client accepts it and the body is
// worth compressing. gz may be a precomputed compressed copy, or nil.
func WriteBody(w http.ResponseWriter, r *http.Request, body, gz []byte) {
	AddVary(w.Header(), "Accept-Encoding")
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
