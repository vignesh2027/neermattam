// Package certs checks, keeps and serves perception's signed measurement
// certificates (docs/CONTRACTS.md 1.2, docs/VERIFY.md).
//
// A certificate is an envelope holding the record's canonical JSON text as
// a string and an Ed25519 signature over exactly those bytes, so the check
// here never re-serialises anything: Go and Python write numbers
// differently, and the signature is over Python's text. Only keys the
// gateway trusts (pinned in its configuration or fetched from perception's
// /v1/keys) are used; a key named in a certificate proves nothing by itself.
package certs

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
)

// Format and Alg are the only certificate format and signature algorithm
// this gateway accepts.
const (
	Format = "neermattam-certificate/1"
	Alg    = "Ed25519"
)

// Envelope is a certificate as perception sends it.
type Envelope struct {
	Format    string `json:"format"`
	Alg       string `json:"alg"`
	KeyID     string `json:"key_id"`
	Payload   string `json:"payload"`
	Signature string `json:"signature"`
}

// Record is the signed record, in the fields the verification page shows.
type Record struct {
	Format        string `json:"format"`
	MeasurementID string `json:"measurement_id"`
	IssuedAt      string `json:"issued_at"`
	Input         struct {
		Kind   string  `json:"kind"`
		SHA256 *string `json:"sha256"`
		Width  int     `json:"width"`
		Height int     `json:"height"`
	} `json:"input"`
	Models []struct {
		Role    string `json:"role"`
		Name    string `json:"name"`
		Version string `json:"version"`
	} `json:"models"`
	Prompts []struct {
		ID     string  `json:"id"`
		SHA256 *string `json:"sha256"`
	} `json:"prompts"`
	Steps  []Step `json:"steps"`
	Checks struct {
		Rules    []string `json:"rules"`
		Failures []string `json:"failures"`
	} `json:"checks"`
	Geometry struct {
		Wheel    *Wheel    `json:"wheel"`
		Person   *Person   `json:"person"`
		Estimate *Estimate `json:"estimate"`
	} `json:"geometry"`
	Decision struct {
		Usable       bool     `json:"usable"`
		Method       *string  `json:"method"`
		DepthCm      *float64 `json:"depth_cm"`
		DepthSigmaCm *float64 `json:"depth_sigma_cm"`
		Confidence   float64  `json:"confidence"`
		Reasons      []string `json:"reasons"`
	} `json:"decision"`
}

// Step is one agent step as the certificate records it.
type Step struct {
	Step          string  `json:"step"`
	Target        string  `json:"target"`
	Model         string  `json:"model"`
	PromptVersion *string `json:"prompt_version"`
	Decision      string  `json:"decision"`
	LatencyMs     int     `json:"latency_ms"`
}

// Wheel holds the numbers a wheel's depth was computed from.
type Wheel struct {
	Target    string `json:"target"`
	Reference struct {
		Kind            string  `json:"kind"`
		DiameterCm      float64 `json:"diameter_cm"`
		DiameterSigmaCm float64 `json:"diameter_sigma_cm"`
		Source          string  `json:"source"`
		SquashCm        float64 `json:"squash_cm"`
		SquashSigmaCm   float64 `json:"squash_sigma_cm"`
	} `json:"reference"`
	Ellipse struct {
		Cx    float64 `json:"cx"`
		Cy    float64 `json:"cy"`
		A     float64 `json:"a"`
		B     float64 `json:"b"`
		Theta float64 `json:"theta"`
	} `json:"ellipse"`
	Waterline struct {
		Point       [2]float64 `json:"point"`
		Direction   [2]float64 `json:"direction"`
		WaterNormal [2]float64 `json:"water_normal"`
		RmsPx       float64    `json:"rms_px"`
		NPoints     int        `json:"n_points"`
	} `json:"waterline"`
	Camera *struct {
		FocalPx float64 `json:"focal_px"`
	} `json:"camera"`
	ArcCoveredDeg   float64 `json:"arc_covered_deg"`
	FractionRaw     float64 `json:"fraction_raw"`
	FractionBias    float64 `json:"fraction_bias"`
	Fraction        float64 `json:"fraction"`
	AmbiguitySpread float64 `json:"ambiguity_spread"`
	ResampleSigmaCm float64 `json:"resample_sigma_cm"`
	ModelSigmaShare float64 `json:"model_sigma_share"`
	DepthCm         float64 `json:"depth_cm"`
	DepthSigmaCm    float64 `json:"depth_sigma_cm"`
	SecondWaterline *struct {
		Point     [2]float64 `json:"point"`
		Direction [2]float64 `json:"direction"`
	} `json:"second_waterline"`
	AgreementCm    *float64 `json:"agreement_cm"`
	EstimateHereCm *float64 `json:"estimate_here_cm"`
}

// Person holds the numbers a person's depth was computed from.
type Person struct {
	Target          string   `json:"target"`
	TopY            float64  `json:"top_y"`
	ShoulderFirstY  float64  `json:"shoulder_first_y"`
	ShoulderFullY   float64  `json:"shoulder_full_y"`
	BottomY         float64  `json:"bottom_y"`
	HeightCm        float64  `json:"height_cm"`
	HeightSigmaCm   float64  `json:"height_sigma_cm"`
	VisibleShare    float64  `json:"visible_share"`
	DepthCm         float64  `json:"depth_cm"`
	DepthSigmaCm    float64  `json:"depth_sigma_cm"`
	Landmark        *string  `json:"landmark"`
	LandmarkDepthCm *float64 `json:"landmark_depth_cm"`
	AgreementCm     *float64 `json:"agreement_cm"`
}

// Estimate is the scene model's own estimate the certificate records.
type Estimate struct {
	DepthCm float64  `json:"depth_cm"`
	LowCm   *float64 `json:"low_cm"`
	HighCm  *float64 `json:"high_cm"`
	SigmaCm float64  `json:"sigma_cm"`
	Basis   string   `json:"basis"`
}

// Status of a check.
const (
	Verified   = "verified"    // the signature matches a trusted key
	Invalid    = "invalid"     // malformed, or the signature does not match
	UnknownKey = "unknown_key" // signed by a key this gateway does not trust (yet)
)

// Result is the outcome of checking one certificate.
type Result struct {
	Status string
	Reason string // a plain English sentence; the page shows its own text per status
	KeyID  string
	Record *Record
	// Pretty is the signed record, indented, for reading.
	Pretty string
}

// Valid reports whether the signature matched a trusted key.
func (r Result) Valid() bool { return r.Status == Verified }

// KeyID is "ed25519:" and the first 16 hex digits of the SHA-256 of the
// 32 public key bytes, as perception computes it.
func KeyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return "ed25519:" + hex.EncodeToString(sum[:8])
}

// DecodeB64URL accepts base64url with or without padding, and standard
// base64.
func DecodeB64URL(s string) ([]byte, error) {
	s = strings.TrimSpace(s)
	s = strings.NewReplacer("+", "-", "/", "_").Replace(s)
	s = strings.TrimRight(s, "=")
	return base64.RawURLEncoding.DecodeString(s)
}

// Parse reads an envelope.
func Parse(raw []byte) (*Envelope, error) {
	var e Envelope
	if err := json.Unmarshal(raw, &e); err != nil {
		return nil, err
	}
	if e.Payload == "" || e.Signature == "" || e.KeyID == "" {
		return nil, errors.New("the certificate lacks a key id, payload or signature")
	}
	return &e, nil
}

// Keys looks up a trusted public key by id.
type Keys interface {
	Lookup(id string) (ed25519.PublicKey, bool)
}

// KeyMap is a fixed set of trusted keys by id.
type KeyMap map[string]ed25519.PublicKey

// Lookup implements Keys.
func (m KeyMap) Lookup(id string) (ed25519.PublicKey, bool) { k, ok := m[id]; return k, ok }

// Verify checks a certificate against trusted keys.
func Verify(raw []byte, keys Keys) Result {
	e, err := Parse(raw)
	if err != nil {
		return Result{Status: Invalid, Reason: "not a readable certificate: " + err.Error()}
	}
	res := Result{KeyID: e.KeyID}
	switch {
	case e.Format != Format:
		res.Status, res.Reason = Invalid, "unknown certificate format "+e.Format
		return res
	case e.Alg != Alg:
		res.Status, res.Reason = Invalid, "unknown signature algorithm "+e.Alg
		return res
	}
	pub, ok := keys.Lookup(e.KeyID)
	if !ok {
		res.Status, res.Reason = UnknownKey, "no trusted public key "+e.KeyID
		return res
	}
	if KeyID(pub) != e.KeyID {
		res.Status, res.Reason = Invalid, "the trusted key does not have the id "+e.KeyID
		return res
	}
	sig, err := DecodeB64URL(e.Signature)
	if err != nil || len(sig) != ed25519.SignatureSize {
		res.Status, res.Reason = Invalid, "the signature is not a base64url Ed25519 signature"
		return res
	}
	if !ed25519.Verify(pub, []byte(e.Payload), sig) {
		res.Status, res.Reason = Invalid, "the signature does not match the certificate"
		return res
	}
	var rec Record
	if err := json.Unmarshal([]byte(e.Payload), &rec); err != nil || rec.Format != Format {
		res.Status, res.Reason = Invalid, "the signed payload is not a certificate record"
		return res
	}
	res.Status, res.Reason, res.Record = Verified, "the signature matches", &rec
	res.Pretty = pretty(e.Payload)
	return res
}

// ReadUnverified parses the record without checking the signature, so the
// page can still show what a certificate that fails claims, clearly marked.
func ReadUnverified(raw []byte) (*Record, string) {
	e, err := Parse(raw)
	if err != nil {
		return nil, ""
	}
	var rec Record
	if json.Unmarshal([]byte(e.Payload), &rec) != nil {
		return nil, ""
	}
	return &rec, pretty(e.Payload)
}

func pretty(payload string) string {
	var v any
	if json.Unmarshal([]byte(payload), &v) != nil {
		return payload
	}
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return payload
	}
	return string(b)
}

var validID = regexp.MustCompile(`^m_[0-9A-Za-z]{6,40}$`)

// ValidID reports whether a measurement id is safe to use as a file name
// and in a URL path.
func ValidID(id string) bool { return validID.MatchString(id) }
