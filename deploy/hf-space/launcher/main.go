// Command neermattam-launcher runs the whole Neermattam stack in one
// container, for the Hugging Face Space.
//
// It starts the engine, perception and the gateway, restarts any that exit,
// and on SIGTERM or SIGINT stops them all cleanly. Between the gateway and
// perception it runs a small guard that counts model calls against a daily
// cap, so public visitors cannot spend the model credit without limit. It
// also logs when each service first answers and, every few minutes, how much
// memory each one holds.
package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Service is one child process.
type Service struct {
	Name   string
	Path   string
	Env    map[string]string // set on top of the launcher's environment
	Drop   []string          // removed from the child's environment
	Health string            // URL that answers 200 once the service is up
	Ready  string            // URL that answers 200 once it can do real work

	mu  sync.Mutex
	cmd *exec.Cmd
}

func env(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}

func main() {
	log := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo})).With("svc", "launcher")
	started := time.Now()

	limit, err := strconv.Atoi(env("PERCEPTION_DAILY_CAP", "150"))
	if err != nil || limit < 0 {
		log.Error("PERCEPTION_DAILY_CAP must be a whole number", "value", os.Getenv("PERCEPTION_DAILY_CAP"))
		os.Exit(2)
	}
	guardAddr := env("GUARD_ADDR", "127.0.0.1:8300")
	percAddr := env("PERCEPTION_ADDR", "127.0.0.1:8301")
	engineAddr := env("ENGINE_ADDR", "127.0.0.1:8302")
	port := env("PORT", "7860")
	// FRONT_ADDR, when set, puts a reverse proxy on that public address and
	// moves the gateway to 127.0.0.1:PORT behind it. The proxy rewrites
	// X-Forwarded-For to the one address it trusts (TRUSTED_PROXY_HOPS).
	frontAddr := os.Getenv("FRONT_ADDR")
	hops, err := strconv.Atoi(env("TRUSTED_PROXY_HOPS", "1"))
	if err != nil || hops < 0 {
		log.Error("TRUSTED_PROXY_HOPS must be a whole number", "value", os.Getenv("TRUSTED_PROXY_HOPS"))
		os.Exit(2)
	}
	logHeaders, _ := strconv.ParseInt(env("LOG_FORWARDING_HEADERS", "0"), 10, 64)
	gatewayBind := "0.0.0.0:" + port
	if frontAddr != "" {
		gatewayBind = "127.0.0.1:" + port
	}
	// Each service sees only the secrets it uses: perception the model keys,
	// the gateway the control-room key and the Web Push private key.
	modelSecrets := []string{"BASETEN_API_KEY", "MODEL_API_KEY", "HF_TOKEN", "MODAL_SAM_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"}
	gatewaySecrets := []string{"CONTROL_ROOM_KEY", "VAPID_PRIVATE_KEY"}
	all := append(append([]string{}, modelSecrets...), gatewaySecrets...)
	bin := env("NEERMATTAM_BIN", "/opt/neermattam/bin")

	percHost, percPort, _ := strings.Cut(percAddr, ":")
	services := []*Service{
		{
			Name: "engine",
			Path: bin + "/neermattam-engine",
			Env: map[string]string{
				"ENGINE_BIND":          engineAddr,
				"ENGINE_OSM_DIR":       env("ENGINE_OSM_DIR", "/opt/neermattam/data/osm"),
				"ENGINE_SNAPSHOT_PATH": env("ENGINE_SNAPSHOT_PATH", "/tmp/neermattam/engine/cells.json"),
			},
			Drop:   all,
			Health: "http://" + engineAddr + "/healthz",
			Ready:  "http://" + engineAddr + "/readyz",
		},
		{
			Name: "perception",
			Path: env("PERCEPTION_BIN", "/opt/venv/bin/neermattam-perception"),
			Env: map[string]string{
				"PERCEPTION_HOST": percHost,
				"PERCEPTION_PORT": percPort,
			},
			Drop:   append([]string{"HF_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"}, gatewaySecrets...),
			Health: "http://" + percAddr + "/healthz",
			Ready:  "http://" + percAddr + "/readyz",
		},
		{
			Name: "gateway",
			Path: bin + "/neermattam-gateway",
			Env: map[string]string{
				"GATEWAY_ADDR":   gatewayBind,
				"ENGINE_URL":     "http://" + engineAddr,
				"PERCEPTION_URL": "http://" + guardAddr,
			},
			Drop:   modelSecrets,
			Health: "http://127.0.0.1:" + port + "/healthz",
			Ready:  "http://127.0.0.1:" + port + "/readyz",
		},
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	// The guard sits between the gateway and perception.
	upstream, _ := url.Parse("http://" + percAddr)
	budget := &Budget{Limit: limit, Now: time.Now, File: os.Getenv("BUDGET_FILE")}
	if err := budget.Load(); err != nil {
		log.Warn("saved perception count unreadable; starting from zero", "error", err.Error())
	}
	if day, used := budget.Status(); used > 0 {
		log.Info("perception count restored", "day", day, "used", used)
	}
	guard := &http.Server{
		Addr:              guardAddr,
		Handler:           NewGuard(upstream, budget, log.With("svc", "guard")),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		if err := guard.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("guard stopped", "error", err.Error())
			stop()
		}
	}()
	var front *http.Server
	if frontAddr != "" {
		gw, _ := url.Parse("http://127.0.0.1:" + port)
		front = &http.Server{
			Addr:              frontAddr,
			Handler:           NewFront(gw, hops, logHeaders, log.With("svc", "front")),
			ReadHeaderTimeout: 10 * time.Second,
		}
		go func() {
			if err := front.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Error("front proxy stopped", "error", err.Error())
				stop()
			}
		}()
	}
	log.Info("starting", "port", port, "front", frontAddr, "trusted_proxy_hops", hops, "perception_daily_cap", limit, "guard", guardAddr)

	var wg sync.WaitGroup
	for _, s := range services {
		wg.Add(2)
		go func() { defer wg.Done(); supervise(ctx, s, log) }()
		go func() { defer wg.Done(); watchStartup(ctx, s, started, log) }()
	}
	go reportMemory(ctx, services, log)

	<-ctx.Done()
	log.Info("shutting down")
	// Stop the gateway first so it drains requests while its upstreams are
	// still there, then the rest.
	for i := len(services) - 1; i >= 0; i-- {
		services[i].signal(syscall.SIGTERM)
	}
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(25 * time.Second):
		log.Warn("services did not stop in 25 s; killing them")
		for _, s := range services {
			s.signal(syscall.SIGKILL)
		}
		<-done
	}
	sctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = guard.Shutdown(sctx)
	if front != nil {
		_ = front.Shutdown(sctx)
	}
	log.Info("stopped")
}

func (s *Service) signal(sig os.Signal) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cmd != nil && s.cmd.Process != nil {
		_ = s.cmd.Process.Signal(sig)
	}
}

func (s *Service) pid() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cmd != nil && s.cmd.Process != nil {
		return s.cmd.Process.Pid
	}
	return 0
}

// childEnv is the launcher's environment, minus Drop, plus Env.
func childEnv(base []string, s *Service) []string {
	drop := map[string]bool{}
	for _, d := range s.Drop {
		drop[d] = true
	}
	for k := range s.Env {
		drop[k] = true
	}
	out := make([]string, 0, len(base)+len(s.Env))
	for _, kv := range base {
		k, _, _ := strings.Cut(kv, "=")
		if !drop[k] {
			out = append(out, kv)
		}
	}
	for k, v := range s.Env {
		out = append(out, k+"="+v)
	}
	return out
}

// Backoff doubles from 1 s up to 30 s, and resets after a run of a minute.
type Backoff struct{ next time.Duration }

func (b *Backoff) After(ran time.Duration) time.Duration {
	if ran >= time.Minute || b.next == 0 {
		b.next = time.Second
	}
	d := b.next
	b.next = min(2*b.next, 30*time.Second)
	return d
}

func supervise(ctx context.Context, s *Service, log *slog.Logger) {
	var b Backoff
	for ctx.Err() == nil {
		cmd := exec.Command(s.Path)
		cmd.Env = childEnv(os.Environ(), s)
		out := newPrefixWriter(s.Name)
		cmd.Stdout, cmd.Stderr = out, out
		t0 := time.Now()
		err := cmd.Start()
		if err == nil {
			s.mu.Lock()
			s.cmd = cmd
			s.mu.Unlock()
			// A signal that arrived between Start and here would be lost.
			if ctx.Err() != nil {
				_ = cmd.Process.Signal(syscall.SIGTERM)
			}
			err = cmd.Wait()
			out.Flush()
			s.mu.Lock()
			s.cmd = nil
			s.mu.Unlock()
		}
		if ctx.Err() != nil {
			log.Info("service stopped", "service", s.Name)
			return
		}
		wait := b.After(time.Since(t0))
		log.Warn("service exited; restarting", "service", s.Name, "error", fmt.Sprint(err), "ran", time.Since(t0).Round(time.Millisecond).String(), "restart_in", wait.String())
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}

// watchStartup logs how long after the launcher started each service first
// answered its health and readiness checks: the cold start, on this hardware.
func watchStartup(ctx context.Context, s *Service, started time.Time, log *slog.Logger) {
	client := &http.Client{Timeout: 5 * time.Second}
	ok := func(u string) bool {
		resp, err := client.Get(u)
		if err != nil {
			return false
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		return resp.StatusCode == http.StatusOK
	}
	healthy := false
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()
	deadline := time.After(15 * time.Minute)
	for {
		select {
		case <-ctx.Done():
			return
		case <-deadline:
			log.Warn("service not ready after 15 minutes", "service", s.Name)
			return
		case <-tick.C:
		}
		if !healthy && ok(s.Health) {
			healthy = true
			log.Info("service answering", "service", s.Name, "after_ms", time.Since(started).Milliseconds())
		}
		if healthy && ok(s.Ready) {
			log.Info("service ready", "service", s.Name, "after_ms", time.Since(started).Milliseconds())
			return
		}
		if healthy {
			// Readiness may call out (perception checks Baseten); poll it gently.
			select {
			case <-ctx.Done():
				return
			case <-time.After(2 * time.Second):
			}
		}
	}
}

// reportMemory logs each service's resident memory and the container total.
func reportMemory(ctx context.Context, services []*Service, log *slog.Logger) {
	every, err := time.ParseDuration(env("MEMORY_LOG_INTERVAL", "2m"))
	if err != nil || every <= 0 {
		return
	}
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		attrs := []any{}
		for _, s := range services {
			if pid := s.pid(); pid > 0 {
				attrs = append(attrs, s.Name+"_rss_mb", procKB(pid, "VmRSS")/1024, s.Name+"_peak_mb", procKB(pid, "VmHWM")/1024)
			}
		}
		// cgroup v2, then v1 (Modal's gVisor sandbox exposes v1).
		if mb, ok := cgroupMB("/sys/fs/cgroup/memory.current"); ok {
			attrs = append(attrs, "container_mb", mb)
		} else if mb, ok := cgroupMB("/sys/fs/cgroup/memory/memory.usage_in_bytes"); ok {
			attrs = append(attrs, "container_mb", mb)
		}
		if mb, ok := cgroupMB("/sys/fs/cgroup/memory.peak"); ok {
			attrs = append(attrs, "container_peak_mb", mb)
		}
		log.Info("memory", attrs...)
	}
}

// procKB reads one kB field of /proc/<pid>/status (Linux only; 0 elsewhere).
func procKB(pid int, field string) int64 {
	f, err := os.Open(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		return 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if v, ok := strings.CutPrefix(sc.Text(), field+":"); ok {
			n, _ := strconv.ParseInt(strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(v), "kB")), 10, 64)
			return n
		}
	}
	return 0
}

func cgroupMB(path string) (int64, bool) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	n, err := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil {
		return 0, false
	}
	return n >> 20, true
}

// prefixWriter writes a child's output to stdout line by line, each line
// tagged with the service name, so the Space log shows who said what.
type prefixWriter struct {
	out    io.Writer
	prefix []byte
	buf    []byte
}

var stdoutMu sync.Mutex

func newPrefixWriter(name string) *prefixWriter {
	return &prefixWriter{out: os.Stdout, prefix: []byte("[" + name + "] ")}
}

func (p *prefixWriter) Write(b []byte) (int, error) {
	p.buf = append(p.buf, b...)
	for {
		i := bytes.IndexByte(p.buf, '\n')
		if i < 0 {
			break
		}
		p.emit(p.buf[:i+1])
		p.buf = p.buf[i+1:]
	}
	// Never hold an unbounded partial line.
	if len(p.buf) > 64<<10 {
		p.Flush()
	}
	return len(b), nil
}

func (p *prefixWriter) emit(line []byte) {
	stdoutMu.Lock()
	defer stdoutMu.Unlock()
	_, _ = p.out.Write(append(append([]byte{}, p.prefix...), line...))
}

// Flush writes any partial last line.
func (p *prefixWriter) Flush() {
	if len(p.buf) > 0 {
		p.emit(append(p.buf, '\n'))
		p.buf = nil
	}
}
