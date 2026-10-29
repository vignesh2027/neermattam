package app

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"neermattam/gateway/internal/cities"
	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/upstream"
)

// CityCookie remembers the chosen city.
const CityCookie = "nm_city"

// cityFor picks the city for a page: ?city=, then the city holding ?at=,
// then the cookie, then the default. An explicit ?city= is remembered.
func (a *App) cityFor(w http.ResponseWriter, r *http.Request) cities.City {
	q := r.URL.Query()
	if id := strings.ToLower(strings.TrimSpace(q.Get("city"))); id != "" {
		if c, ok := a.Cities.Get(id); ok {
			if w != nil {
				http.SetCookie(w, &http.Cookie{Name: CityCookie, Value: c.ID, Path: "/", MaxAge: 365 * 24 * 3600, SameSite: http.SameSiteLaxMode, HttpOnly: true})
			}
			return c
		}
	}
	if lat, lon, ok := parseLatLon(q.Get("at")); ok {
		if c, ok := a.Cities.Find(lat, lon); ok {
			return c
		}
	}
	if ck, err := r.Cookie(CityCookie); err == nil {
		if c, ok := a.Cities.Get(ck.Value); ok {
			return c
		}
	}
	c, _ := a.Cities.Get(cities.Default)
	return c
}

func parseLatLon(s string) (float64, float64, bool) {
	la, lo, ok := strings.Cut(s, ",")
	if !ok {
		return 0, 0, false
	}
	lat, err1 := strconv.ParseFloat(strings.TrimSpace(la), 64)
	lon, err2 := strconv.ParseFloat(strings.TrimSpace(lo), 64)
	if err1 != nil || err2 != nil || lat != lat || lon != lon {
		return 0, 0, false
	}
	return lat, lon, true
}

// simulatedIn says whether a city's map holds the simulated scenario. The
// scenario only ever lives in the default city.
func (a *App) simulatedIn(city string) bool {
	return city == cities.Default && a.Origins.HasSimulated()
}

// cellsForCity returns the cells on a city's map, from the shared cache.
func (a *App) cellsForCity(ctx context.Context, city string) (*cellsEntry, error) {
	return a.cellsCached(ctx, "city="+city, url.Values{"city": {city}}, city)
}

// cellFeatures decodes a city's cells, for the alert watcher.
func (a *App) cellFeatures(ctx context.Context, city string) ([]upstream.CellFeature, error) {
	e, err := a.cellsForCity(ctx, city)
	if err != nil {
		return nil, err
	}
	return upstream.DecodeCells(e.body)
}

// apiCities is GET /api/cities.
func (a *App) apiCities(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	list := []map[string]any{}
	for _, c := range a.Cities.All() {
		list = append(list, map[string]any{
			"id": c.ID, "name": c.Name,
			"bbox":               [4]float64{c.BBox.MinLon, c.BBox.MinLat, c.BBox.MaxLon, c.BBox.MaxLat},
			"centre":             map[string]float64{"lat": c.CentreLat, "lon": c.CentreLon},
			"cells":              c.Cells,
			"routing":            c.Routing,
			"simulated_scenario": a.simulatedIn(c.ID),
		})
	}
	w.Header().Set("Cache-Control", "public, max-age=30")
	writeJSON(w, http.StatusOK, map[string]any{"cities": list, "default": cities.Default})
}

// routeError is a route failure with a stable code and words for people.
type routeError struct {
	Status int
	Code   string
	Key    string // i18n key under err.
	Args   []any
	Msg    string // English
}

func (e *routeError) Error() string { return e.Msg }

// Text is the message in a language; city ids among the arguments are
// printed as the city's name in that language.
func (e *routeError) Text(lang string) string {
	if lang == i18n.Default || e.Key == "" {
		return e.Msg
	}
	args := make([]any, len(e.Args))
	for i, x := range e.Args {
		if c, ok := x.(cityArg); ok {
			args[i] = cityName(lang, string(c))
		} else {
			args[i] = x
		}
	}
	return i18n.T(lang, "err."+e.Key, args...)
}

// cityArg marks a city id inside message arguments.
type cityArg string

func cityName(lang, id string) string {
	if s := i18n.T(lang, "city."+id); s != "city."+id {
		return s
	}
	return id
}

func rerr(status int, code, key, msg string, args ...any) *routeError {
	english := make([]any, len(args))
	for i, x := range args {
		if c, ok := x.(cityArg); ok {
			english[i] = cityName(i18n.Default, string(c))
		} else {
			english[i] = x
		}
	}
	return &routeError{Status: status, Code: code, Key: key, Args: args, Msg: fmt.Sprintf(msg, english...)}
}

// engineRouteError turns an engine failure into plain words. city is the
// city the route was asked for.
func (a *App) engineRouteError(err error, city string) *routeError {
	var ue *upstream.Error
	if errors.As(err, &ue) {
		msg := strings.ToLower(ue.Message)
		switch {
		case ue.Status == http.StatusServiceUnavailable:
			if c, ok := a.Cities.Get(city); ok && c.Routing == cities.RoutingFailed {
				return rerr(503, "routing_unavailable", "route_failed", "Routes in %s are not available: its road map could not be loaded. The live map and reports there still work.", cityArg(city))
			}
			return rerr(503, "routing_unavailable", "route_loading", "Routes in %s are not available yet: its road map is still loading. Try again in a minute.", cityArg(city))
		case ue.Status == http.StatusNotFound:
			return rerr(404, "no_road", "route_no_road", "No road connects these two points. Try points on or near a main road.")
		case strings.Contains(msg, "same city") || strings.Contains(msg, "different cities"):
			return rerr(400, "different_cities", "route_different_cities", "The start and the end are in different cities. A route has to start and end in the same city.")
		case strings.Contains(msg, "outside"):
			return rerr(400, "outside_area", "route_outside", "The start or the end is outside the area Neermattam covers in %s.", cityArg(city))
		case ue.Status == http.StatusBadRequest:
			return rerr(400, "bad_request", "", "The route could not be planned: "+ue.Message)
		}
		return rerr(502, "engine_error", "route_down", "The route service had a problem. Try again shortly.")
	}
	return rerr(503, "engine_unavailable", "route_down", "The route service is not reachable right now. Try again shortly.")
}

// citiesRefresh keeps the city list current.
func (a *App) citiesRefresh(ctx context.Context) {
	a.Cities.Run(ctx, a.Engine, 30*time.Second)
}
