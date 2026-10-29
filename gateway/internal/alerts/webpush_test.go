package alerts

import (
	"crypto/ecdh"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/sha256"
	"encoding/json"
	"math/big"
	"strings"
	"testing"
	"time"
)

func mustB64(t *testing.T, s string) []byte {
	t.Helper()
	b, err := b64.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// The worked example in RFC 8291 section 5.
func TestEncryptMatchesRFC8291Example(t *testing.T) {
	asPriv, err := ecdh.P256().NewPrivateKey(mustB64(t, "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"))
	if err != nil {
		t.Fatal(err)
	}
	got, err := Encrypt([]byte("When I grow up, I want to be a watermelon"),
		mustB64(t, "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
		mustB64(t, "BTBZMqHH6r4Tts7J_aSIgg"),
		mustB64(t, "DGv6ra1nlYgDCS1FRnbzlw"), asPriv)
	if err != nil {
		t.Fatal(err)
	}
	want := "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
	if b64.EncodeToString(got) != want {
		t.Fatalf("got  %s\nwant %s", b64.EncodeToString(got), want)
	}
}

func TestVAPIDHeaderIsAValidES256JWT(t *testing.T) {
	v, err := LoadVAPID("", "", "", "mailto:ops@example.org")
	if err != nil {
		t.Fatal(err)
	}
	h, err := v.header("https://fcm.googleapis.com/fcm/send/abc", time.Unix(1_800_000_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	tok, key, ok := strings.Cut(strings.TrimPrefix(h, "vapid t="), ", k=")
	if !ok || key != v.Public {
		t.Fatalf("header %q", h)
	}
	parts := strings.Split(tok, ".")
	var claims map[string]any
	if err := json.Unmarshal(mustB64(t, parts[1]), &claims); err != nil {
		t.Fatal(err)
	}
	if claims["aud"] != "https://fcm.googleapis.com" || claims["sub"] != "mailto:ops@example.org" {
		t.Fatalf("claims %v", claims)
	}
	pub, err := ecdsa.ParseUncompressedPublicKey(elliptic.P256(), mustB64(t, v.Public))
	if err != nil {
		t.Fatal(err)
	}
	sig := mustB64(t, parts[2])
	sum := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if !ecdsa.Verify(pub, sum[:], new(big.Int).SetBytes(sig[:32]), new(big.Int).SetBytes(sig[32:])) {
		t.Fatal("signature does not verify")
	}
	// The key survives a round trip through its file.
	dir := t.TempDir()
	a, err := LoadVAPID("", "", dir, "mailto:x@example.org")
	if err != nil {
		t.Fatal(err)
	}
	b, err := LoadVAPID("", "", dir, "mailto:x@example.org")
	if err != nil || a.Public != b.Public {
		t.Fatalf("reloaded key differs: %v", err)
	}
}

func TestEndpointsAreLimitedToPushServices(t *testing.T) {
	p := &Pusher{AllowHost: func(h string) bool { return HostAllowed(h, DefaultPushHosts) }}
	for ep, ok := range map[string]bool{
		"https://fcm.googleapis.com/fcm/send/x":                true,
		"https://web.push.apple.com/QK":                        true,
		"https://wns2-db5p.notify.windows.com/w/?token=1":      true,
		"https://updates.push.services.mozilla.com/wpush/v2/x": true,
		"http://fcm.googleapis.com/fcm/send/x":                 false,
		"https://169.254.169.254/latest":                       false,
		"https://evil.example/fcm.googleapis.com":              false,
		"https://fcm.googleapis.com.evil.example/x":            false,
	} {
		if err := p.CheckEndpoint(ep); (err == nil) != ok {
			t.Errorf("%s: err %v, want ok=%v", ep, err, ok)
		}
	}
}
