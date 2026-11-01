package app

import (
	"fmt"
	"sync"
	"time"

	"neermattam/gateway/internal/upstream"
)

// readiness keeps each upstream's last successful health probe, so that
// /readyz does not flip to 503 on a single slow probe.
//
// The rule: a probe that runs out of time (READY_PROBE_TIMEOUT, 5 s by
// default) is treated as the service's last successful answer when that
// answer is younger than READY_GRACE (30 s by default); /readyz then stays
// 200 and says the latest probe timed out and how old the answer it used
// is. A refused connection, or an error status (503 while a model or a
// road graph loads), is reported at once, and so is any failure once the
// grace has passed or when there has been no successful probe yet.
type readiness struct {
	mu   sync.Mutex
	last map[string]readyAnswer
}

type readyAnswer struct {
	at  time.Time
	eng upstream.Readiness
}

// ok records a successful probe.
func (r *readiness) ok(service string, now time.Time, eng upstream.Readiness) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.last == nil {
		r.last = map[string]readyAnswer{}
	}
	r.last[service] = readyAnswer{at: now, eng: eng}
}

// lean returns the last successful answer and its age when a failed probe
// may lean on it: the probe timed out and the answer is younger than grace.
func (r *readiness) lean(service string, err error, now time.Time, grace time.Duration) (upstream.Readiness, time.Duration, bool) {
	if !upstream.IsTimeout(err) || grace <= 0 {
		return upstream.Readiness{}, 0, false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	a, ok := r.last[service]
	if !ok {
		return upstream.Readiness{}, 0, false
	}
	age := now.Sub(a.at)
	if age > grace {
		return upstream.Readiness{}, 0, false
	}
	return a.eng, age, true
}

func staleNote(err error, age time.Duration) string {
	return fmt.Sprintf("the latest probe timed out (%v); using the last successful probe, %.0f s ago", err, age.Seconds())
}
