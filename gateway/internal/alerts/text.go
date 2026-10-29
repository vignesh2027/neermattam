package alerts

import (
	"fmt"
	"strings"

	"neermattam/gateway/internal/i18n"
)

// Notice is one message.
type Notice struct {
	Title, Body, URL string
}

// place localises "near X" and "at lat, lon"; names stay as they are.
func place(lang, p string) string {
	if lang == i18n.Default {
		return p // "near Velachery" reads right mid-sentence in English
	}
	switch {
	case strings.HasPrefix(p, "near "):
		return i18n.T(lang, "js.place_near", strings.TrimPrefix(p, "near "))
	case strings.HasPrefix(p, "at "):
		return i18n.T(lang, "place.at", strings.TrimPrefix(p, "at "))
	}
	return p
}

// Where names a subscription's place in a language.
func Where(s Subscription) string {
	if s.LabelTo != "" {
		return i18n.T(s.Lang, "alert.place_route", place(s.Lang, s.Label), place(s.Lang, s.LabelTo))
	}
	return place(s.Lang, s.Label)
}

// Compose writes the message for a notice kind. Every message says it is
// advisory, and none calls water safe.
func Compose(s Subscription, kind string, st Status, publicURL string) Notice {
	l := s.Lang
	where := Where(s)
	who := i18n.T(l, "alert.v_"+s.Vehicle)
	limit := fmt.Sprintf("%.0f", LimitCm(s.Vehicle))
	class := i18n.T(l, "hazard."+orUnknown(st.Class))
	walk := s.Vehicle == "pedestrian"
	n := Notice{URL: Link(s, publicURL)}
	switch kind {
	case KindWatching:
		n.Title = i18n.T(l, "alert.t_watching", where)
		switch st.State {
		case StateOver:
			n.Body = i18n.T(l, "alert.b_watching_over", who)
		case StateUnder:
			n.Body = i18n.T(l, "alert.b_watching_under", who)
		default:
			n.Body = i18n.T(l, "alert.b_watching_unknown", who)
		}
	case KindOver:
		n.Title = i18n.T(l, "alert.t_over", where)
		if walk {
			n.Body = i18n.T(l, "alert.b_over_walk", class)
		} else {
			n.Body = i18n.T(l, "alert.b_over", fmt.Sprintf("%.0f", st.DepthCm), limit, who, class)
		}
	case KindWorse:
		n.Title = i18n.T(l, "alert.t_worse", where)
		n.Body = i18n.T(l, "alert.b_worse", class)
	case KindClear:
		n.Title = i18n.T(l, "alert.t_clear", where)
		if walk {
			n.Body = i18n.T(l, "alert.b_clear_walk")
		} else {
			n.Body = i18n.T(l, "alert.b_clear", limit, who)
		}
	case KindStale:
		n.Title = i18n.T(l, "alert.t_stale", where)
		n.Body = i18n.T(l, "alert.b_stale")
	}
	return n
}

func orUnknown(c string) string {
	if c == "" {
		return "unknown"
	}
	return c
}
