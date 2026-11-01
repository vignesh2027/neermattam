package sites

import (
	"fmt"
	"strings"

	"neermattam/gateway/internal/alerts"
	"neermattam/gateway/internal/i18n"
)

// RoutesText names a list of routes ("main_road:truck") in a language.
func RoutesText(lang string, ids []string) string {
	parts := make([]string, 0, len(ids))
	for _, id := range ids {
		t, v := RouteLabel(id)
		parts = append(parts, i18n.T(lang, "sites.route_pair", i18n.T(lang, "sites.vehicle."+v), i18n.T(lang, "sites.target."+t)))
	}
	return strings.Join(parts, "; ")
}

// Compose writes one message. Every body ends with the advisory note, and
// none calls a site safe.
func Compose(s Sub, ch Change, st State, publicURL string) alerts.Notice {
	lang := s.Lang
	name := s.SiteName
	if name == "" {
		name = s.Site
	}
	limit := fmt.Sprintf("%.0f", s.ThresholdCm)
	depth := fmt.Sprintf("%.0f", st.DepthCm)
	n := alerts.Notice{URL: Link(s, publicURL)}
	var body []string
	switch ch.Zone {
	case KindWatching:
		n.Title = i18n.T(lang, "sitealert.t_watching", name)
		switch st.Zone {
		case ZoneOver:
			body = append(body, i18n.T(lang, "sitealert.b_watching_over", depth, limit))
		case ZoneNearby:
			body = append(body, i18n.T(lang, "sitealert.b_watching_nearby", depth, limit))
		case ZoneUnder:
			body = append(body, i18n.T(lang, "sitealert.b_watching_under", limit))
		default:
			body = append(body, i18n.T(lang, "sitealert.b_watching_unknown", limit))
		}
		if len(st.Closed) > 0 {
			body = append(body, i18n.T(lang, "sitealert.b_route_closed", RoutesText(lang, st.Closed)))
		}
	case KindOver:
		n.Title = i18n.T(lang, "sitealert.t_over", name)
		body = append(body, i18n.T(lang, "sitealert.b_over", depth, limit))
	case KindNearby:
		n.Title = i18n.T(lang, "sitealert.t_nearby", name)
		body = append(body, i18n.T(lang, "sitealert.b_nearby", depth, limit))
	case KindUnder:
		n.Title = i18n.T(lang, "sitealert.t_under", name)
		body = append(body, i18n.T(lang, "sitealert.b_under", limit))
	case KindStale:
		n.Title = i18n.T(lang, "sitealert.t_stale", name)
		body = append(body, i18n.T(lang, "sitealert.b_stale", limit))
	}
	if ch.Zone != KindWatching {
		if len(ch.Closed) > 0 {
			if n.Title == "" {
				n.Title = i18n.T(lang, "sitealert.t_route_closed", name)
			}
			body = append(body, i18n.T(lang, "sitealert.b_route_closed", RoutesText(lang, ch.Closed)))
		}
		if len(ch.Opened) > 0 {
			if n.Title == "" {
				n.Title = i18n.T(lang, "sitealert.t_route_open", name)
			}
			body = append(body, i18n.T(lang, "sitealert.b_route_open", RoutesText(lang, ch.Opened)))
		}
	}
	body = append(body, i18n.T(lang, "sitealert.advisory"))
	n.Body = strings.Join(body, " ")
	return n
}
