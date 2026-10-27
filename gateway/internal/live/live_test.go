package live

import (
	"encoding/json"
	"io"
	"log/slog"
	"testing"
	"time"
)

const feature = `{"type":"Feature","id":"c1","geometry":{"type":"Polygon","coordinates":[[[80.2182,12.9908],[80.2182,12.9915],[80.2176,12.9919],[80.2170,12.9915],[80.2170,12.9908],[80.2176,12.9905],[80.2182,12.9908]]]},"properties":{"cell":"c1","depth_cm":10}}`

func origin(t *testing.T, o *Origins, raw string) string {
	t.Helper()
	b, err := DecorateFeature([]byte(raw), o)
	if err != nil {
		t.Fatal(err)
	}
	var f struct {
		Properties map[string]any `json:"properties"`
	}
	json.Unmarshal(b, &f)
	return f.Properties["origin"].(string)
}

func TestOriginLabels(t *testing.T) {
	o := NewOrigins()
	if got := origin(t, o, feature); got != OriginReported {
		t.Fatalf("unmarked cell: %s", got)
	}
	o.Mark("c1", true)
	if got := origin(t, o, feature); got != OriginSimulated {
		t.Fatalf("simulated cell: %s", got)
	}
	o.Mark("c1", false)
	if got := origin(t, o, feature); got != OriginMixed {
		t.Fatalf("mixed cell: %s", got)
	}
	if !o.HasSimulated() {
		t.Fatal("HasSimulated")
	}
}

func TestZoneLabelsStreamedCellsBeforeTheResponse(t *testing.T) {
	o := NewOrigins()
	o.Expect(12.9912, 80.2176, true) // a simulated reading is about to be posted here
	if got := origin(t, o, feature); got != OriginSimulated {
		t.Fatalf("cell streamed during a simulated post: %s", got)
	}
	o2 := NewOrigins()
	o2.Expect(13.05, 80.25, true) // far away
	if got := origin(t, o2, feature); got != OriginReported {
		t.Fatalf("zone leaked to a cell 7 km away: %s", got)
	}
	clock := time.Now()
	o3 := NewOrigins()
	o3.now = func() time.Time { return clock }
	o3.Expect(12.9912, 80.2176, true)
	clock = clock.Add(time.Minute)
	if got := origin(t, o3, feature); got != OriginReported {
		t.Fatalf("expired zone still labels: %s", got)
	}
}

func TestHubBoundsAndDropsSlowClients(t *testing.T) {
	h := NewHub(2, NewOrigins(), slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	h.buffer = 1
	a, _ := h.Subscribe()
	b, _ := h.Subscribe()
	if _, err := h.Subscribe(); err != ErrFull {
		t.Fatalf("third subscriber: %v", err)
	}
	h.Broadcast(Message{Event: "x"})
	<-a.C() // a keeps up, b does not
	h.Broadcast(Message{Event: "y"})
	if h.Clients() != 1 {
		t.Fatalf("slow client not dropped: %d clients", h.Clients())
	}
	if _, open := <-b.C(); !open {
		t.Fatal("b should still receive its buffered message before close")
	}
	if _, open := <-b.C(); open {
		t.Fatal("b's channel should be closed")
	}
	h.Close()
	if _, open := <-a.C(); !open {
		t.Fatal("a has one buffered message")
	}
	if _, err := h.Subscribe(); err != ErrClosed {
		t.Fatalf("subscribe after close: %v", err)
	}
}

func TestFeedKeepsRealReportsAboveTheSimulatedDrip(t *testing.T) {
	h := NewHub(1, NewOrigins(), slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	h.PublishFeed(FeedItem{Place: "real"})
	for i := 0; i < 20; i++ {
		h.PublishFeed(FeedItem{Simulated: true, Place: "sim"})
	}
	f := h.Feed()
	if f[0].Place != "real" || len(f) != 9 {
		t.Fatalf("feed %+v", f)
	}
}
