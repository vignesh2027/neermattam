package upstream

import "testing"

func TestDecodePersonReference(t *testing.T) {
	m, err := decodeMeasurement([]byte(`{"usable":true,"depth_cm":49.0,"depth_sigma_cm":12.7,"confidence":0.23,
		"reference":{"kind":"person","diameter_cm":null,"diameter_sigma_cm":null,"source":"prior:adult stature","height_cm":158.8,"height_sigma_cm":9.0},
		"waterline":{"method":"person_mask_bottom","y_px":604.0,"agreement_cm":3.8},"method":"person_reference"}`))
	if err != nil {
		t.Fatal(err)
	}
	if m.Kind() != MethodPersonReference || !m.MethodIs(MethodPersonReference) || m.MethodIs(MethodModelEstimate) {
		t.Fatalf("method %q", m.Kind())
	}
	r := m.Reference
	if !r.IsPerson() || r.DiameterCm != nil || r.HeightCm == nil || *r.HeightCm != 158.8 || r.HeightSigmaCm == nil || *r.HeightSigmaCm != 9 {
		t.Fatalf("reference %+v", r)
	}
}

func TestUnknownMethodReadsAsEstimate(t *testing.T) {
	for in, want := range map[string]string{
		"wheel_geometry":   MethodWheelGeometry,
		"person_reference": MethodPersonReference,
		"model_estimate":   MethodModelEstimate,
		"stereo_depth":     MethodModelEstimate,
		"WHEEL_GEOMETRY":   MethodModelEstimate,
		"":                 "",
	} {
		if got := NormalizeMethod(in); got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
		}
	}
	m, err := decodeMeasurement([]byte(`{"usable":true,"depth_cm":30,"depth_sigma_cm":5,"method":"stereo_depth"}`))
	if err != nil {
		t.Fatal(err)
	}
	if !m.MethodIs(MethodModelEstimate) || m.MethodIs(MethodWheelGeometry) || *m.Method != "stereo_depth" || KnownMethod(*m.Method) {
		t.Fatalf("an unknown method must read as an estimate and keep its name: %+v", m)
	}
	var none *Measurement
	if none.Kind() != "" || none.MethodIs(MethodModelEstimate) {
		t.Fatal("nil measurement")
	}
}
