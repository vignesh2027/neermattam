package ratelimit

import (
	"net/http/httptest"
	"testing"
	"time"
)

type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

func TestBurstThenRefill(t *testing.T) {
	c := &clock{t: time.Unix(1_700_000_000, 0)}
	l := New(6, 3).WithClock(c.now) // one token every 10 s, bursts of 3
	for i := 0; i < 3; i++ {
		if ok, _ := l.Allow("a"); !ok {
			t.Fatalf("request %d refused inside the burst", i+1)
		}
	}
	ok, wait := l.Allow("a")
	if ok {
		t.Fatal("fourth request allowed")
	}
	if wait < 9*time.Second || wait > 10*time.Second {
		t.Fatalf("retry after %v, want about 10 s", wait)
	}
	if ok, _ := l.Allow("b"); !ok {
		t.Fatal("another client must have its own bucket")
	}
	c.t = c.t.Add(10 * time.Second)
	if ok, _ := l.Allow("a"); !ok {
		t.Fatal("token not refilled after 10 s")
	}
	if ok, _ := l.Allow("a"); ok {
		t.Fatal("only one token should have refilled")
	}
}

func TestBucketNeverExceedsBurst(t *testing.T) {
	c := &clock{t: time.Unix(0, 0)}
	l := New(60, 2).WithClock(c.now)
	l.Allow("a")
	c.t = c.t.Add(time.Hour)
	n := 0
	for {
		if ok, _ := l.Allow("a"); !ok {
			break
		}
		n++
	}
	if n != 2 {
		t.Fatalf("allowed %d after a long idle period, want the burst of 2", n)
	}
}

func TestSweepForgetsFullBuckets(t *testing.T) {
	c := &clock{t: time.Unix(0, 0)}
	l := New(60, 5).WithClock(c.now)
	l.Allow("a")
	l.Allow("b")
	c.t = c.t.Add(2 * time.Second)
	l.Sweep()
	if l.Len() != 2 {
		t.Fatalf("swept buckets that were not full yet: %d left", l.Len())
	}
	c.t = c.t.Add(10 * time.Second)
	l.Sweep()
	if l.Len() != 0 {
		t.Fatalf("%d buckets left after they refilled", l.Len())
	}
}

func TestClientIP(t *testing.T) {
	r := httptest.NewRequest("GET", "/", nil)
	r.RemoteAddr = "10.0.0.7:5555"
	r.Header.Set("X-Forwarded-For", "203.0.113.9, 10.0.0.1")
	if got := ClientIP(r, false); got != "10.0.0.7" {
		t.Errorf("untrusted proxy: got %q", got)
	}
	if got := ClientIP(r, true); got != "203.0.113.9" {
		t.Errorf("trusted proxy: got %q", got)
	}
	r.Header.Set("X-Forwarded-For", "not an ip")
	if got := ClientIP(r, true); got != "10.0.0.7" {
		t.Errorf("garbage header: got %q", got)
	}
}
