package main

import (
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync/atomic"
)

// ClientIP picks the visitor's address from X-Forwarded-For, trusting only
// the last `hops` entries: those were appended by the proxies in front of
// this container, which a visitor cannot forge. Anything further left was
// sent by the visitor and is ignored. With no usable header it falls back to
// the connection's address.
//
// Each proxy appends the address that connected to it. With one trusted
// proxy (hops = 1) the visitor is the last entry; with two, the second to
// last, and so on.
func ClientIP(xff []string, remoteAddr string, hops int) string {
	var parts []string
	for _, h := range xff {
		for _, p := range strings.Split(h, ",") {
			if p = strings.TrimSpace(p); p != "" {
				parts = append(parts, p)
			}
		}
	}
	if hops > 0 && len(parts) >= hops {
		if ip := net.ParseIP(parts[len(parts)-hops]); ip != nil {
			return ip.String()
		}
	}
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		return remoteAddr
	}
	return host
}

// NewFront returns the public reverse proxy in front of the gateway. It
// replaces X-Forwarded-For with the one address it trusts, so the gateway's
// per-IP rate limit (which reads the first entry when TRUST_PROXY=1) cannot
// be dodged by a visitor who sends their own header. When logHeaders is
// above zero, that many requests have their forwarding headers logged, to
// check how many hops the hosting proxy adds.
func NewFront(gateway *url.URL, hops int, logHeaders int64, log *slog.Logger) http.Handler {
	var logged atomic.Int64
	proxy := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			in := pr.In
			client := ClientIP(in.Header.Values("X-Forwarded-For"), in.RemoteAddr, hops)
			if logged.Add(1) <= logHeaders {
				log.Info("forwarding headers", "x_forwarded_for", strings.Join(in.Header.Values("X-Forwarded-For"), " | "),
					"x_real_ip", in.Header.Get("X-Real-Ip"), "remote", in.RemoteAddr, "client", client)
			}
			pr.SetURL(gateway)
			pr.Out.Host = in.Host
			pr.Out.Header.Set("X-Forwarded-For", client)
			pr.Out.Header.Del("X-Real-Ip")
			if pr.Out.Header.Get("X-Forwarded-Proto") == "" {
				pr.Out.Header.Set("X-Forwarded-Proto", "https")
			}
		},
		// Pass server-sent events (the live map, the report trace) through at once.
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			if r.Context().Err() != nil {
				return // the visitor went away
			}
			log.Warn("gateway unreachable", "path", r.URL.Path, "error", err.Error())
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.Header().Set("Retry-After", "5")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte("Neermattam is starting or restarting. Please try again in a few seconds.\n"))
		},
	}
	return proxy
}
