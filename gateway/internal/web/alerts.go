package web

import (
	"encoding/json"
	"html/template"
)

// AlertsView is the "Watch my street" page.
type AlertsView struct {
	Push       bool   // Web Push is on
	VAPIDKey   string // the application server key for subscribe()
	WhatsApp   bool   // the WhatsApp channel is configured
	MaxPer     int
	TTLDays    int
	Vehicles   []string
	From, To   string // a route handed over from /route
	Vehicle    string
	PointLabel string
	Config     template.JS // {"key","city","lang","from","to"} for the glue
}

// SetConfig fills Config.
func (v *AlertsView) SetConfig(city, lang string) {
	b, _ := json.Marshal(map[string]string{"key": v.VAPIDKey, "city": city, "lang": lang, "from": v.From, "to": v.To})
	v.Config = template.JS(b) //nolint:gosec // json.Marshal escapes <, > and &
}

// DevView is the developer API page.
type DevView struct {
	Base   string // the public base URL used in the examples
	Cities []string
	Keys   bool // keys are configured on this gateway
}
