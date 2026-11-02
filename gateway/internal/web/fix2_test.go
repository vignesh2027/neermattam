package web

import (
	"testing"
	"time"

	"neermattam/gateway/internal/certs"
)

// Times shown to visitors say they are IST, so a judge abroad does not
// read them as local time.
func TestTimesSayIST(t *testing.T) {
	at := time.Date(2026, 11, 1, 17, 15, 48, 0, time.UTC)
	if got := When("en", at); got != "22:45 IST, 1 Nov" {
		t.Fatalf("When %q", got)
	}
	// A date late in the UTC day is already the next day in India.
	if got := Day("en", time.Date(2026, 11, 1, 20, 0, 0, 0, time.UTC)); got != "2 Nov 2026" {
		t.Fatalf("Day %q", got)
	}
}

// A record that holds only a date is shown as that date, never as 05:30.
func TestVerifyReceivedDateOnly(t *testing.T) {
	st := certs.Stored{ReceivedAt: time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC), ReceivedDateOnly: true}
	v := NewVerifyView("en", "m_01TEST0000", st, certs.Result{Status: certs.Verified}, "https://example.org")
	if v.ReceivedAt != "1 Nov 2026" {
		t.Fatalf("received %q", v.ReceivedAt)
	}
	st.ReceivedDateOnly = false
	if v := NewVerifyView("en", "m_01TEST0000", st, certs.Result{Status: certs.Verified}, "https://example.org"); v.ReceivedAt != "05:30 IST, 1 Nov" {
		t.Fatalf("received %q", v.ReceivedAt)
	}
}

func TestPlaceInCity(t *testing.T) {
	cases := []struct{ lang, place, city, want string }{
		{"en", "Tatanagar", "bengaluru", "Tatanagar, Bengaluru"},
		{"en", "Kharghar, Navi Mumbai", "mumbai", "Kharghar, Navi Mumbai"},
		{"en", "Nandanam, Chennai", "chennai", "Nandanam, Chennai"},
		{"en", "", "chennai", ""},
	}
	for _, c := range cases {
		if got := PlaceInCity(c.lang, c.place, c.city); got != c.want {
			t.Errorf("PlaceInCity(%q, %q) = %q, want %q", c.place, c.city, got, c.want)
		}
	}
	if got := PlaceInCity("ta", "Tatanagar", "bengaluru"); got == "Tatanagar" {
		t.Error("Tamil gets no city")
	}
}

// Sample, unusable and refused reports name the stages they never enter.
func TestSkippedStages(t *testing.T) {
	if s := SkippedStages("en", "sample"); len(s) != 2 || s["verify"] == "" || s["fuse"] == "" {
		t.Fatalf("sample %v", s)
	}
	if s := SkippedStages("en", "rejected"); len(s) != 1 || s["fuse"] == "" {
		t.Fatalf("rejected %v", s)
	}
	if s := SkippedStages("en", "fused"); s != nil {
		t.Fatalf("fused %v", s)
	}
}
