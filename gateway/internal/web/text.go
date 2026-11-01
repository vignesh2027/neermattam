package web

import (
	"fmt"
	"strings"
	"time"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/verify"
)

var ist = time.FixedZone("IST", 5*3600+30*60)

// Ago formats a past time for people, in a language.
func Ago(lang string, t time.Time) string {
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return i18n.T(lang, "js.just_now")
	case d < time.Hour:
		return i18n.T(lang, "js.min_ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return i18n.T(lang, "js.h_ago", fmt.Sprintf("%.0f", d.Hours()))
	default:
		lt := t.In(ist)
		return fmt.Sprintf("%d %s", lt.Day(), i18n.Month(lang, lt.Month()))
	}
}

// When formats a time of day and date in IST.
func When(lang string, t time.Time) string {
	lt := t.In(ist)
	return fmt.Sprintf("%s, %d %s", lt.Format("15:04"), lt.Day(), i18n.Month(lang, lt.Month()))
}

// Place localises "near X" and "at lat, lon"; place names stay as they are.
func Place(lang, p string) string {
	switch {
	case strings.HasPrefix(p, "near "):
		return i18n.T(lang, "js.place_near", strings.TrimPrefix(p, "near "))
	case strings.HasPrefix(p, "at "):
		return i18n.T(lang, "place.at", strings.TrimPrefix(p, "at "))
	}
	return capitalise(p)
}

// AgeText formats a duration in a language.
func AgeText(lang string, a verify.Age) string {
	d := time.Duration(a)
	switch {
	case d < time.Minute:
		return i18n.T(lang, "dur.under_minute")
	case d < time.Hour:
		return i18n.T(lang, "dur.minutes", int(d.Minutes()))
	case d < 48*time.Hour:
		h := d.Hours()
		if h < 10 {
			return i18n.T(lang, "dur.hours", fmt.Sprintf("%.1f", h))
		}
		return i18n.T(lang, "dur.hours", fmt.Sprintf("%.0f", h))
	case d < 2*365*24*time.Hour:
		return i18n.T(lang, "dur.days", fmt.Sprintf("%.0f", d.Hours()/24))
	default:
		return i18n.T(lang, "dur.years", fmt.Sprintf("%.0f", d.Hours()/24/365.25))
	}
}

// localArgs turns ages into words and leaves everything else alone.
func localArgs(lang string, args []any) []any {
	out := make([]any, len(args))
	for i, a := range args {
		if age, ok := a.(verify.Age); ok {
			out[i] = AgeText(lang, age)
		} else if c, ok := a.(report.CityID); ok {
			out[i] = i18n.T(lang, "city."+string(c))
		} else {
			out[i] = a
		}
	}
	return out
}

func has(lang, key string) bool {
	_, ok := i18n.Catalogs[lang][key]
	if !ok {
		_, ok = i18n.Catalogs[i18n.Default][key]
	}
	return ok
}

// ReasonText is a verification reason in a language. English keeps the
// rule's own sentence, which has proper plurals.
func ReasonText(lang string, r verify.RuleResult) string {
	if lang == i18n.Default || r.Key == "" || !has(lang, "reason."+r.Key) {
		return capitalise(r.Reason)
	}
	s := i18n.T(lang, "reason."+r.Key, localArgs(lang, r.Args)...)
	if r.ZoneAssumed {
		s += " " + i18n.T(lang, "reason.zone_ist")
	}
	return capitalise(s)
}

// ErrorText is a report error in a language.
func ErrorText(lang string, e *report.Error) string {
	if e == nil {
		return ""
	}
	if lang == i18n.Default || e.Key == "" || !has(lang, "err."+e.Key) {
		return e.Message
	}
	return i18n.T(lang, "err."+e.Key, localArgs(lang, e.Args)...)
}

// ReasonTexts is a check's reason in every page language, keyed by
// language code, for the decision kept beside a certificate.
func ReasonTexts(r verify.RuleResult) map[string]string {
	out := make(map[string]string, len(i18n.Languages))
	for _, l := range i18n.Languages {
		out[l.Code] = ReasonText(l.Code, r)
	}
	return out
}
