package alerts

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/url"
	"sync"
	"time"

	"neermattam/gateway/internal/upstream"
)

// Notice kinds.
const (
	KindWatching = "watching"
	KindOver     = "over"
	KindWorse    = "worse"
	KindClear    = "clear"
	KindStale    = "stale"
)

// Vehicle limits, the same as the engine's routing (docs/ENGINE.md, Vehicle
// limits): a vehicle may not cross water whose blocking depth is over its
// limit; people on foot are kept out of "danger for some" and worse.
func LimitCm(vehicle string) float64 {
	if vehicle == "bus" {
		return 30
	}
	return 15
}

// Vehicles are the vehicles a watch can be for.
var Vehicles = []string{"pedestrian", "two_wheeler", "auto", "car", "bus"}

// ValidVehicle reports whether v is one of Vehicles.
func ValidVehicle(v string) bool {
	for _, x := range Vehicles {
		if x == v {
			return true
		}
	}
	return false
}

// ClassRank orders hazard classes; unknown and empty are 0.
func ClassRank(c string) int {
	switch c {
	case "low":
		return 1
	case "danger_some":
		return 2
	case "danger_most":
		return 3
	case "danger_all":
		return 4
	}
	return 0
}

// Blocks reports whether a cell is closed to a vehicle, judged as routing
// judges it: at the blocking depth (the upper estimate).
func Blocks(vehicle string, p upstream.CellProps) bool {
	if vehicle == "pedestrian" {
		return ClassRank(p.UpperClass()) >= ClassRank("danger_some")
	}
	return p.BlockingDepthCm() > LimitCm(vehicle)
}

// CellSource returns the current cells on a city's map.
type CellSource func(ctx context.Context, city string) ([]upstream.CellFeature, error)

// Service watches subscriptions against the live map.
type Service struct {
	Store     *Store
	Pusher    *Pusher // nil: no Web Push
	WhatsApp  func(ctx context.Context, phone, phoneID, text string) error
	Cells     CellSource
	PublicURL string
	Log       *slog.Logger
	// MinGap is the least time between two messages to one watch; a
	// change inside it is held and sent at the next check.
	MinGap time.Duration
	// Sweep is how often every watched city is checked in full, which
	// catches water that drains away without a new report.
	Sweep time.Duration
	// OnSent counts messages, for metrics.
	OnSent func(channel, kind, outcome string)

	mu     sync.Mutex
	index  map[string]*cityIndex
	events chan cellEvent
	now    func() time.Time
}

type cellEvent struct {
	city string
	cell upstream.CellFeature
}

// gridDeg is the bucket size of the cell index, about 220 m: a hexagon
// containing a point has its centre within one bucket of the point's.
const gridDeg = 0.002

type cityIndex struct {
	cells  map[string]upstream.CellFeature
	grid   map[[2]int32][]string
	loaded time.Time
}

func bucket(lat, lon float64) [2]int32 {
	return [2]int32{int32(math.Floor(lat / gridDeg)), int32(math.Floor(lon / gridDeg))}
}

func newIndex(cells []upstream.CellFeature) *cityIndex {
	ix := &cityIndex{cells: map[string]upstream.CellFeature{}, grid: map[[2]int32][]string{}, loaded: time.Now()}
	for _, c := range cells {
		ix.put(c)
	}
	return ix
}

func (ix *cityIndex) put(c upstream.CellFeature) {
	if _, ok := ix.cells[c.ID]; !ok {
		b := bucket(c.Lat, c.Lon)
		ix.grid[b] = append(ix.grid[b], c.ID)
	}
	ix.cells[c.ID] = c
}

// containing returns the cells whose hexagon holds the point.
func (ix *cityIndex) containing(p Point) []upstream.CellFeature {
	b := bucket(p.Lat, p.Lon)
	var out []upstream.CellFeature
	for dy := int32(-1); dy <= 1; dy++ {
		for dx := int32(-1); dx <= 1; dx++ {
			for _, id := range ix.grid[[2]int32{b[0] + dy, b[1] + dx}] {
				if c := ix.cells[id]; c.ContainsPoint(p.Lat, p.Lon) {
					out = append(out, c)
				}
			}
		}
	}
	return out
}

// Evaluate computes a subscription's status from an index.
func evaluate(s *Subscription, ix *cityIndex) Status {
	st := Status{State: StateUnknown}
	if ix == nil {
		return st
	}
	seen := map[string]bool{}
	over := false
	for _, p := range s.Targets() {
		for _, c := range ix.containing(p) {
			if seen[c.ID] || c.Props.Expired {
				continue
			}
			seen[c.ID] = true
			st.Cells++
			if Blocks(s.Vehicle, c.Props) {
				over = true
			}
			st.DepthCm = math.Max(st.DepthCm, c.Props.BlockingDepthCm())
			if ClassRank(c.Props.UpperClass()) > ClassRank(st.Class) {
				st.Class = c.Props.UpperClass()
			}
		}
	}
	switch {
	case over:
		st.State = StateOver
	case st.Cells > 0:
		st.State = StateUnder
	}
	st.DepthCm = math.Round(st.DepthCm)
	return st
}

// Decide is the notice a change of status calls for, or "".
func Decide(prev, cur Status) string {
	switch {
	case cur.State == StateOver && prev.State != StateOver:
		return KindOver
	case prev.State == StateOver && cur.State == StateUnder:
		return KindClear
	case prev.State == StateOver && cur.State == StateUnknown:
		return KindStale
	case ClassRank(cur.Class) > ClassRank(prev.Class) && ClassRank(cur.Class) >= ClassRank("danger_some"):
		return KindWorse
	}
	return ""
}

func (sv *Service) clock() time.Time {
	if sv.now != nil {
		return sv.now()
	}
	return time.Now()
}

// indexFor returns a city's index, loading it when missing or older than
// the sweep interval.
func (sv *Service) indexFor(ctx context.Context, city string, fresh bool) (*cityIndex, error) {
	sv.mu.Lock()
	if sv.index == nil {
		sv.index = map[string]*cityIndex{}
	}
	ix := sv.index[city]
	sv.mu.Unlock()
	if ix != nil && !fresh && time.Since(ix.loaded) < sv.Sweep {
		return ix, nil
	}
	cells, err := sv.Cells(ctx, city)
	if err != nil {
		if ix != nil {
			return ix, nil
		}
		return nil, err
	}
	ix = newIndex(cells)
	sv.mu.Lock()
	sv.index[city] = ix
	sv.mu.Unlock()
	return ix, nil
}

// Watch evaluates a new subscription, stores it and sends the first message
// saying what it watches and what the water is now. It returns the token.
func (sv *Service) Watch(ctx context.Context, sub *Subscription) (string, Status, error) {
	ix, err := sv.indexFor(ctx, sub.City, false)
	if err != nil {
		ix = nil
	}
	sub.bounds()
	st := evaluate(sub, ix)
	sub.Last = st
	token, err := sv.Store.Add(sub)
	if err != nil {
		return "", st, err
	}
	sub.SentAt = sv.clock()
	cp := *sub
	go sv.send(context.WithoutCancel(ctx), cp, KindWatching, st)
	return token, st, nil
}

// OnCell takes one cell event from the live stream. It never blocks.
func (sv *Service) OnCell(city string, raw []byte) {
	if sv.events == nil || city == "" {
		return
	}
	c, err := upstream.DecodeCell(raw)
	if err != nil {
		return
	}
	select {
	case sv.events <- cellEvent{city, c}:
	default: // the next sweep catches up
	}
}

// Start creates the event queue; call before Run and before OnCell.
func (sv *Service) Start() {
	sv.events = make(chan cellEvent, 4096)
	if sv.Sweep <= 0 {
		sv.Sweep = 2 * time.Minute
	}
}

// Run applies stream events and sweeps until ctx ends.
func (sv *Service) Run(ctx context.Context) {
	t := time.NewTicker(sv.Sweep)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case ev := <-sv.events:
			sv.mu.Lock()
			ix := sv.index[ev.city]
			if ix != nil {
				ix.put(ev.cell)
			}
			sv.mu.Unlock()
			if ix != nil {
				sv.check(ctx, ev.city, ix, &ev.cell)
			}
		case <-t.C:
			sv.SweepAll(ctx)
		}
	}
}

// SweepAll reloads every watched city and checks every watch.
func (sv *Service) SweepAll(ctx context.Context) {
	for _, city := range sv.Store.Cities() {
		ix, err := sv.indexFor(ctx, city, true)
		if err != nil {
			if sv.Log != nil {
				sv.Log.Warn("alerts: city not checked", "city", city, "error", err.Error())
			}
			continue
		}
		sv.check(ctx, city, ix, nil)
	}
}

type pendingNotice struct {
	sub  Subscription
	kind string
	st   Status
}

// check evaluates the watches of a city (only those near cell, when given)
// and sends what changed.
func (sv *Service) check(ctx context.Context, city string, ix *cityIndex, cell *upstream.CellFeature) {
	now := sv.clock()
	var out []pendingNotice
	sv.mu.Lock()
	sv.Store.each(city, func(s *Subscription) bool {
		if cell != nil {
			const m = 0.0015 // about 150 m: a hexagon's reach from its centre
			if cell.Lat < s.minLat-m || cell.Lat > s.maxLat+m || cell.Lon < s.minLon-m || cell.Lon > s.maxLon+m {
				return false
			}
		}
		st := evaluate(s, ix)
		kind := Decide(s.Last, st)
		if kind == "" {
			changed := s.Last != st
			s.Last = st
			return changed
		}
		if now.Sub(s.SentAt) < sv.MinGap {
			return false // held until the gap has passed
		}
		s.Last = st
		s.SentAt = now
		out = append(out, pendingNotice{*s, kind, st})
		return true
	})
	sv.mu.Unlock()
	for _, n := range out {
		sv.send(ctx, n.sub, n.kind, n.st)
	}
}

// send delivers one message on the subscription's channel.
func (sv *Service) send(ctx context.Context, s Subscription, kind string, st Status) {
	n := Compose(s, kind, st, sv.PublicURL)
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	var err error
	switch s.Channel {
	case ChannelPush:
		if sv.Pusher == nil {
			return
		}
		payload, _ := json.Marshal(map[string]string{"title": n.Title, "body": n.Body, "url": n.URL, "tag": "nm-" + s.ID, "kind": kind})
		urgency := "normal"
		if kind == KindOver || kind == KindWorse {
			urgency = "high"
		}
		err = sv.Pusher.Send(ctx, s.Endpoint, s.P256dh, s.Auth, payload, urgency)
	case ChannelWhatsApp:
		if sv.WhatsApp == nil {
			return
		}
		err = sv.WhatsApp(ctx, s.Phone, s.PhoneID, n.Title+"\n"+n.Body+"\n"+n.URL)
	}
	outcome := "ok"
	switch {
	case errors.Is(err, ErrGone):
		outcome = "gone"
		sv.Store.Remove(s.ID)
	case err != nil:
		outcome = "failed"
		sv.Store.each(s.City, func(x *Subscription) bool {
			if x.ID != s.ID {
				return false
			}
			x.Failures++
			if x.Failures >= 5 {
				x.ExpiresAt = time.Time{} // pruned on the next pass
			}
			return true
		})
		if sv.Log != nil {
			sv.Log.Warn("alert not delivered", "channel", s.Channel, "kind", kind, "error", err.Error())
		}
	default:
		sv.Store.each(s.City, func(x *Subscription) bool {
			if x.ID != s.ID {
				return false
			}
			x.Sent++
			x.Failures = 0
			return true
		})
	}
	if sv.OnSent != nil {
		sv.OnSent(s.Channel, kind, outcome)
	}
}

// Link is the page a message opens.
func Link(s Subscription, publicURL string) string {
	q := url.Values{"city": {s.City}, "lang": {s.Lang}}
	if s.Point != nil {
		q.Set("at", fmt.Sprintf("%.5f,%.5f", s.Point.Lat, s.Point.Lon))
		return publicURL + "/map?" + q.Encode()
	}
	if len(s.Route) >= 2 {
		a, b := s.Route[0], s.Route[len(s.Route)-1]
		q.Set("from", fmt.Sprintf("%.5f,%.5f", a[1], a[0]))
		q.Set("to", fmt.Sprintf("%.5f,%.5f", b[1], b[0]))
		q.Set("vehicle", s.Vehicle)
		return publicURL + "/route?" + q.Encode()
	}
	return publicURL + "/map?" + q.Encode()
}

// SampleRoute turns a route line (lon,lat pairs) into points at most every
// stepM metres, capped at max points (the step grows for long routes).
func SampleRoute(line [][2]float64, stepM float64, maxPoints int) [][2]float64 {
	if len(line) == 0 {
		return nil
	}
	total := 0.0
	for i := 1; i < len(line); i++ {
		total += distM(line[i-1], line[i])
	}
	if n := total / stepM; n > float64(maxPoints-1) {
		stepM = total / float64(maxPoints-1)
	}
	out := [][2]float64{line[0]}
	carry := 0.0
	for i := 1; i < len(line); i++ {
		a, b := line[i-1], line[i]
		d := distM(a, b)
		for carry+d >= stepM && d > 0 {
			f := (stepM - carry) / d
			a = [2]float64{a[0] + (b[0]-a[0])*f, a[1] + (b[1]-a[1])*f}
			out = append(out, a)
			d = distM(a, b)
			carry = 0
		}
		carry += d
	}
	last := line[len(line)-1]
	if out[len(out)-1] != last {
		out = append(out, last)
	}
	if len(out) > maxPoints {
		out = append(out[:maxPoints-1], last)
	}
	return out
}

func distM(a, b [2]float64) float64 {
	const r = 6371008.8
	la1, la2 := a[1]*math.Pi/180, b[1]*math.Pi/180
	dla := la2 - la1
	dlo := (b[0] - a[0]) * math.Pi / 180
	h := math.Sin(dla/2)*math.Sin(dla/2) + math.Cos(la1)*math.Cos(la2)*math.Sin(dlo/2)*math.Sin(dlo/2)
	return 2 * r * math.Asin(math.Min(1, math.Sqrt(h)))
}
