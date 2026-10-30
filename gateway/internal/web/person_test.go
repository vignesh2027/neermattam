package web

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/verify"
)

// personOutcome is a fused report from perception's recorded answer on
// the Commons photo c1 (eval/out/person/c1.json, overlay left out): a man
// standing in knee-deep water, measured against the adult stature prior.
func personOutcome(t *testing.T) *report.Outcome {
	t.Helper()
	raw, err := os.ReadFile("testdata/person_reference.json")
	if err != nil {
		t.Fatal(err)
	}
	var m upstream.Measurement
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 10, 30, 10, 0, 0, 0, time.UTC)
	vr := (&verify.Verifier{T: verify.DefaultThresholds()}).Verify(context.Background(), verify.Input{
		Lat: 12.98, Lon: 80.22, DepthCm: *m.DepthCm, DepthSigmaCm: *m.DepthSigmaCm, Confidence: m.Confidence,
		Source: "photo", Method: *m.Method, ReceivedAt: now,
	})
	return &report.Outcome{
		Status: report.StatusFused, Place: "near Velachery", Source: "photo", ReceivedAt: now,
		DepthCm: m.DepthCm, DepthSigmaCm: m.DepthSigmaCm, Measurement: &m, Verification: &vr,
		OverlayURL: "/api/overlay/" + m.MeasurementID,
		Cell:       &upstream.ReadingResult{DepthCm: 47, DepthSigmaCm: 11.8, DepthUpperCm: 65, NReadings: 1, Hazard: upstream.Hazard{Class: "danger_most"}},
		Advisory:   report.Advisory,
	}
}

func TestPersonReferenceParses(t *testing.T) {
	m := personOutcome(t).Measurement
	if !m.MethodIs(upstream.MethodPersonReference) || m.MethodIs(upstream.MethodModelEstimate) || m.Kind() != "person_reference" {
		t.Fatalf("method %v", *m.Method)
	}
	r := m.Reference
	if !r.IsPerson() || r.DiameterCm != nil || r.HeightCm == nil || *r.HeightCm != 158.8 || *r.HeightSigmaCm != 9 {
		t.Fatalf("reference %+v", r)
	}
	if m.Waterline.Method != "person_mask_bottom" || *m.Waterline.AgreementCm != 3.8 {
		t.Fatalf("waterline %+v", m.Waterline)
	}
}

func TestPersonReferenceResultCard(t *testing.T) {
	r, err := NewRenderer()
	if err != nil {
		t.Fatal(err)
	}
	o := personOutcome(t)
	for _, tc := range []struct {
		lang string
		want []string
		not  []string
	}{
		{"en", []string{
			"Measured from a person</span>",
			"<b>Measured from a person standing in the water.</b>",
			"The depth assumes an Indian adult of average height, 159 cm ± 9 cm",
			"Faces of the people the models found are blurred",
			"Person, assumed height 159\u00a0cm\u00a0±\u00a09",
			"prior:adult stature, NCD-RisC 2016 India",
			"Where the person&#39;s outline meets the water",
			"gives a depth 4 cm away",
			"What the models noted",
			"the scene model puts the water at the knee, about 45 cm (4 cm apart)",
			"Measured from the person",
			"Visual estimate by muse-spark-open",
			"the person&#39;s outline, the height line and the waterline. Faces are blurred.",
			"Measured against a person standing in the water, assuming an adult of average height",
			"Confidence 0.23, as expected when a person is the ruler",
			"× 0.85",
			"49<small>cm</small>", "± 12.7 cm",
		}, []string{"Model estimate</span>", "No wheel outline passed its checks", "Tips:"}},
		{"ta", []string{
			"நபரை வைத்து அளக்கப்பட்டது</span>",
			"<b>தண்ணீரில் நிற்கும் ஒருவரை வைத்து அளக்கப்பட்டது.</b>",
			"சராசரி உயரமுள்ள இந்திய வயதுவந்தவர் (159 cm ± 9 cm)",
			"முகங்கள் இங்கே காட்டப்படும் படத்தில் மங்கலாக்கப்பட்டுள்ளன",
			"நபர், கொள்ளப்பட்ட உயரம் 159\u00a0cm\u00a0±\u00a09",
			"நபரின் வரைவு தண்ணீரைச் சந்திக்கும் இடம்",
			"மாதிரிகள் குறிப்பிட்டவை",
			"the scene model puts the water at the knee", // perception's own words
			"நம்பகத்தன்மை 0.23, ஒருவரை அளவுகோலாக வைக்கும்போது",
			"தண்ணீரில் நிற்கும் ஒருவரை வைத்து, சராசரி உயரமுள்ள",
		}, []string{"மாதிரி மதிப்பீடு</span>", "Measured from a person"}},
	} {
		t.Run(tc.lang, func(t *testing.T) {
			html, err := r.Fragment(tc.lang, "result_card", NewResultView(o, tc.lang))
			if err != nil {
				t.Fatal(err)
			}
			for _, w := range tc.want {
				if !strings.Contains(html, w) {
					t.Errorf("card lacks %q", w)
				}
			}
			for _, w := range tc.not {
				if strings.Contains(html, w) {
					t.Errorf("card should not have %q", w)
				}
			}
			if strings.ContainsAny(html, "—→←") {
				t.Error("em dash or arrow in the card")
			}
		})
	}
}

// A person without the stature numbers still says what it assumed.
func TestPersonNoteWithoutHeight(t *testing.T) {
	o := personOutcome(t)
	m := *o.Measurement
	m.Reference = &upstream.Reference{Kind: "person"}
	o.Measurement = &m
	v := NewResultView(o, "en")
	if v.Method != "person" || !strings.Contains(v.PersonNote, "Indian adult of average height, which is why") {
		t.Fatalf("%+v", v.PersonNote)
	}
}

// A wheel measurement shows perception's reasons, so a person in the same
// photo who disagrees with the wheel is visible.
func TestWheelResultShowsADisagreeingPerson(t *testing.T) {
	r, _ := NewRenderer()
	d, s := 20.0, 3.0
	method := upstream.MethodWheelGeometry
	disagree := "a person in the water gives 60 cm (sigma 13 cm), more than two sigma from the wheel; confidence lowered"
	o := &report.Outcome{Status: report.StatusFused, Source: "photo", ReceivedAt: time.Now(), DepthCm: &d, DepthSigmaCm: &s,
		Measurement: &upstream.Measurement{Usable: true, DepthCm: &d, DepthSigmaCm: &s, Confidence: 0.3, Method: &method,
			Reasons: []string{"standing water visible", "two-wheeler wheel found", disagree}, Reference: &upstream.Reference{Kind: "two_wheeler_wheel"}},
		Advisory: report.Advisory}
	html, err := r.Fragment("en", "result_card", NewResultView(o, "en"))
	if err != nil {
		t.Fatal(err)
	}
	for _, w := range []string{"Measured from a two-wheeler wheel", "What the models noted", "more than two sigma from the wheel; confidence lowered"} {
		if !strings.Contains(html, w) {
			t.Errorf("card lacks %q", w)
		}
	}
	if strings.Contains(html, "person-note") {
		t.Error("a wheel result must not carry the person note")
	}
}

// A method this gateway does not know is shown as an estimate, never as a
// measurement.
func TestUnknownMethodShowsAsEstimate(t *testing.T) {
	o := personOutcome(t)
	m := *o.Measurement
	future := "stereo_depth"
	m.Method = &future
	o.Measurement = &m
	v := NewResultView(o, "en")
	if v.Method != "estimate" || v.MethodLabel != "Model estimate" || v.EstimateNote == "" || v.PersonNote != "" || v.Readings != nil {
		t.Fatalf("%+v", v)
	}
	o.Status = report.StatusRejected
	if v := NewResultView(o, "en"); v.Title != "Estimated, but not added to the map" {
		t.Fatalf("title %q", v.Title)
	}
}
