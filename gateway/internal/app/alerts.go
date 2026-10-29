package app

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"neermattam/gateway/internal/alerts"
	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/places"
	"neermattam/gateway/internal/web"
	"neermattam/gateway/internal/whatsapp"
)

// setupAlerts builds the alert store and watcher. Web Push needs a VAPID
// key: from VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY, else made once and kept
// in the data directory.
func (a *App) setupAlerts() error {
	cfg := a.Cfg
	a.AlertStore = alerts.NewStore(cfg.AlertsMax, cfg.AlertsPerClient, cfg.AlertTTL)
	if !cfg.Alerts {
		return nil
	}
	if path := a.alertsPath(); path != "" {
		if n, err := a.AlertStore.Load(path); err != nil {
			aside := path + ".rejected-" + time.Now().UTC().Format("20060102T150405")
			_ = os.Rename(path, aside)
			a.Log.Warn("alerts not loaded; starting with none", "error", err.Error(), "moved_to", aside)
		} else if n > 0 {
			a.Log.Info("alerts loaded", "watches", n)
		}
	}
	sv := &alerts.Service{
		Store: a.AlertStore, Cells: a.cellFeatures, PublicURL: cfg.PublicURL, Log: a.Log,
		MinGap: cfg.AlertMinGap, Sweep: cfg.AlertSweep,
	}
	sent := a.Metrics.Registry.NewCounterVec("neermattam_alerts_sent_total", "Alert messages by channel, kind and outcome.", "channel", "kind", "outcome")
	sv.OnSent = func(ch, kind, outcome string) { sent.Inc(ch, kind, outcome) }
	a.Metrics.Registry.NewGaugeFunc("neermattam_alert_watches", "Watches held.", func() float64 { return float64(a.AlertStore.Len()) })
	v, err := alerts.LoadVAPID(cfg.VAPIDPublic, cfg.VAPIDPrivate, cfg.DataDir, cfg.VAPIDSubject)
	if err != nil {
		return fmt.Errorf("VAPID key: %w", err)
	}
	hosts := cfg.PushHosts
	if len(hosts) == 0 {
		hosts = alerts.DefaultPushHosts
	}
	sv.Pusher = &alerts.Pusher{VAPID: v, AllowHost: func(h string) bool { return alerts.HostAllowed(h, hosts) }}
	sv.Start()
	a.Alerts = sv
	a.Hub.Listen(func(m live.Message) {
		if m.Event == "cell" {
			sv.OnCell(m.City, m.Data)
		}
	})
	return nil
}

func (a *App) alertsPath() string {
	if a.Cfg.DataDir == "" {
		return ""
	}
	return filepath.Join(a.Cfg.DataDir, alerts.FileName)
}

func (a *App) saveAlerts() {
	if path := a.alertsPath(); path != "" && a.AlertStore != nil {
		if err := a.AlertStore.Save(path); err != nil {
			a.Log.Warn("alerts not saved", "error", err.Error())
		}
	}
}

// alertsPage is GET /alerts.
func (a *App) alertsPage(w http.ResponseWriter, r *http.Request) {
	p := a.page(w, r, "alerts", true)
	p.Footer = true
	v := &web.AlertsView{
		WhatsApp: a.WhatsApp != nil && a.Alerts != nil, MaxPer: a.Cfg.AlertsPerClient,
		TTLDays: int(a.Cfg.AlertTTL.Hours() / 24), Vehicles: alerts.Vehicles, Vehicle: "two_wheeler",
	}
	if a.Alerts != nil && a.Alerts.Pusher != nil {
		v.Push, v.VAPIDKey = true, a.Alerts.Pusher.VAPID.Public
	}
	q := r.URL.Query()
	if veh, ok := NormaliseVehicle(q.Get("vehicle")); ok {
		v.Vehicle = veh
	}
	if _, _, ok := parseLatLon(q.Get("from")); ok {
		if _, _, ok := parseLatLon(q.Get("to")); ok {
			v.From, v.To = q.Get("from"), q.Get("to")
		}
	}
	v.SetConfig(p.City.ID, p.Lang)
	p.Alerts = v
	a.render(w, http.StatusOK, "alerts", p)
}

// serviceWorker is GET /sw.js. It only shows a notification and opens its
// link when tapped; it caches nothing and reads nothing else.
func (a *App) serviceWorker(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Service-Worker-Allowed", "/")
	_, _ = w.Write([]byte(serviceWorkerJS))
}

const serviceWorkerJS = `/* Neermattam alerts: show a pushed notice, open its page when tapped. */
self.addEventListener("push", function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { title: "Neermattam", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Neermattam", {
    body: d.body || "", tag: d.tag || "neermattam", renotify: true,
    icon: "/static/brand/apple-touch-icon.png", badge: "/static/brand/favicon-32.png",
    data: { url: d.url || "/map" }
  }));
});
self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || "/map";
  e.waitUntil(clients.openWindow(url));
});
`

// apiAlertKey is GET /api/alerts/key.
func (a *App) apiAlertKey(w http.ResponseWriter, r *http.Request) {
	if a.Alerts == nil || a.Alerts.Pusher == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "push alerts are off on this gateway", "code": "alerts_off"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"vapid_public_key": a.Alerts.Pusher.VAPID.Public})
}

type alertRequest struct {
	Subscription struct {
		Endpoint string `json:"endpoint"`
		Keys     struct {
			P256dh string `json:"p256dh"`
			Auth   string `json:"auth"`
		} `json:"keys"`
	} `json:"subscription"`
	City    string        `json:"city"`
	Vehicle string        `json:"vehicle"`
	Lang    string        `json:"lang"`
	Point   *alerts.Point `json:"point"`
	Route   *routeEnds    `json:"route"`
}

// routeEnds are a route's two ends, "lat,lon" each.
type routeEnds struct {
	From string `json:"from"`
	To   string `json:"to"`
}

func alertErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": msg, "code": code})
}

// apiAlertCreate is POST /api/alerts.
func (a *App) apiAlertCreate(w http.ResponseWriter, r *http.Request) {
	if a.Alerts == nil || a.Alerts.Pusher == nil {
		alertErr(w, http.StatusNotFound, "alerts_off", "Push alerts are off on this gateway.")
		return
	}
	if !a.limit(w, r, a.reportLimit, "alerts") {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 16<<10)
	var req alertRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		alertErr(w, http.StatusBadRequest, "bad_json", "The request must be JSON with subscription, city, vehicle and a point or a route.")
		return
	}
	s := &alerts.Subscription{Channel: alerts.ChannelPush, Endpoint: req.Subscription.Endpoint, P256dh: req.Subscription.Keys.P256dh, Auth: req.Subscription.Keys.Auth}
	if err := a.Alerts.Pusher.CheckEndpoint(s.Endpoint); err != nil {
		alertErr(w, http.StatusBadRequest, "bad_endpoint", capitalise(err.Error())+".")
		return
	}
	if !validKey(s.P256dh, 65) || !validKey(s.Auth, 16) {
		alertErr(w, http.StatusBadRequest, "bad_keys", "The subscription's p256dh and auth keys are missing or malformed.")
		return
	}
	lang := req.Lang
	if _, ok := i18n.Get(lang); !ok {
		lang = langFrom(r.Context())
	}
	code, msg := a.fillWatch(r.Context(), s, req.City, req.Vehicle, lang, req.Point, req.Route)
	if code != 0 {
		alertErr(w, code, "bad_watch", msg)
		return
	}
	token, st, err := a.Alerts.Watch(r.Context(), s)
	if err != nil {
		switch {
		case errors.Is(err, alerts.ErrTooMany):
			alertErr(w, http.StatusConflict, "too_many", fmt.Sprintf("This browser already watches %d places. Stop one first.", a.Cfg.AlertsPerClient))
		case errors.Is(err, alerts.ErrFull):
			alertErr(w, http.StatusServiceUnavailable, "full", "The gateway holds as many watches as it can. Try again later.")
		default:
			alertErr(w, http.StatusInternalServerError, "internal", "The watch could not be saved.")
		}
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": s.ID, "token": token, "status": st.State, "class": st.Class, "depth_cm": st.DepthCm,
		"where": alerts.Where(*s), "message": alerts.Compose(*s, alerts.KindWatching, st, a.Cfg.PublicURL).Body,
		"expires_at": s.ExpiresAt,
	})
}

func validKey(s string, n int) bool {
	b, err := decodeB64URL(s)
	return err == nil && len(b) == n
}

// fillWatch checks and fills the place, city, vehicle and language.
func (a *App) fillWatch(ctx context.Context, s *alerts.Subscription, city, vehicle, lang string, pt *alerts.Point, route *routeEnds) (int, string) {
	c, ok := a.Cities.Get(strings.ToLower(city))
	if !ok {
		return http.StatusBadRequest, "city must be one of " + strings.Join(a.Cities.SortedIDs(), ", ") + "."
	}
	v, ok := NormaliseVehicle(vehicle)
	if !ok || !alerts.ValidVehicle(v) {
		return http.StatusBadRequest, "vehicle must be walking, two_wheeler, auto, car or bus."
	}
	s.City, s.Vehicle, s.Lang = c.ID, v, lang
	switch {
	case pt != nil && route == nil:
		if !c.Contains(pt.Lat, pt.Lon) {
			return http.StatusBadRequest, fmt.Sprintf("The point is outside %s.", c.Name)
		}
		s.Point = &alerts.Point{Lat: pt.Lat, Lon: pt.Lon}
		s.Label = places.Near(pt.Lat, pt.Lon)
	case route != nil && pt == nil:
		flat, flon, ok1 := parseLatLon(route.From)
		tlat, tlon, ok2 := parseLatLon(route.To)
		if !ok1 || !ok2 || !c.Contains(flat, flon) || !c.Contains(tlat, tlon) {
			return http.StatusBadRequest, fmt.Sprintf("The route's ends must be lat,lon points inside %s.", c.Name)
		}
		line, err := a.routeLine(ctx, route.From, route.To, v, c.ID)
		if err != nil {
			return http.StatusBadGateway, "The route could not be planned, so it cannot be watched: " + err.Error()
		}
		s.Route = alerts.SampleRoute(line, 25, 800)
		s.Label, s.LabelTo = places.Near(flat, flon), places.Near(tlat, tlon)
	default:
		return http.StatusBadRequest, "Send either a point or a route."
	}
	return 0, ""
}

// routeLine plans the water-aware route and returns its line (lon,lat).
func (a *App) routeLine(ctx context.Context, from, to, vehicle, city string) ([][2]float64, error) {
	raw, err := a.routeJSON(ctx, routeQuery{From: from, To: to, Vehicle: vehicle, City: city})
	if err != nil {
		return nil, a.engineRouteError(err, city)
	}
	var f struct {
		Geometry struct {
			Coordinates [][2]float64 `json:"coordinates"`
		} `json:"geometry"`
	}
	if err := json.Unmarshal(raw, &f); err != nil || len(f.Geometry.Coordinates) < 2 {
		return nil, errors.New("the route had no line")
	}
	return f.Geometry.Coordinates, nil
}

// apiAlertGet is GET /api/alerts/{id} with X-Alert-Token.
func (a *App) apiAlertGet(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	s, err := a.AlertStore.Get(r.PathValue("id"), r.Header.Get("X-Alert-Token"))
	if err != nil {
		alertErr(w, http.StatusNotFound, "not_found", "No such watch, or the token does not match. Watches end after their expiry date.")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"id": s.ID, "city": s.City, "vehicle": s.Vehicle, "where": alerts.Where(s), "status": s.Last.State,
		"class": s.Last.Class, "depth_cm": s.Last.DepthCm, "expires_at": s.ExpiresAt, "sent": s.Sent,
	})
}

// apiAlertDelete is DELETE /api/alerts/{id} with X-Alert-Token.
func (a *App) apiAlertDelete(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	if err := a.AlertStore.Delete(r.PathValue("id"), r.Header.Get("X-Alert-Token")); err != nil {
		alertErr(w, http.StatusNotFound, "not_found", "No such watch, or the token does not match.")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// whatsAppWatch connects WhatsApp's "watch" and "stop" to the same watcher.
func (a *App) whatsAppWatch() *whatsapp.Watch {
	return &whatsapp.Watch{
		Start: func(ctx context.Context, phone, phoneID string, lat, lon float64, vehicle string) string {
			c, ok := a.Cities.Find(lat, lon)
			if !ok {
				return "That location is outside the cities Neermattam covers (" + strings.Join(a.Cities.Names(), ", ") + "), so it cannot be watched."
			}
			s := &alerts.Subscription{Channel: alerts.ChannelWhatsApp, Phone: phone, PhoneID: phoneID}
			pt := &alerts.Point{Lat: lat, Lon: lon}
			if code, msg := a.fillWatch(ctx, s, c.ID, vehicle, i18n.Default, pt, nil); code != 0 {
				return msg
			}
			_, st, err := a.Alerts.Watch(ctx, s)
			switch {
			case errors.Is(err, alerts.ErrTooMany):
				return fmt.Sprintf("This number already watches %d places. Send stop to end them all.", a.Cfg.AlertsPerClient)
			case err != nil:
				return "Sorry, the watch could not be saved. Please try again later."
			}
			_ = st // the watcher sends the first message itself
			return ""
		},
		Stop: func(phone string) int { return a.AlertStore.RemovePhone(phone) },
	}
}

func decodeB64URL(s string) ([]byte, error) {
	return base64.RawURLEncoding.DecodeString(strings.TrimRight(s, "="))
}
