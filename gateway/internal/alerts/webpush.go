package alerts

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/ecdh"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/hkdf"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Web Push with VAPID, from the standard library only:
//
//   - RFC 8291 message encryption (aes128gcm): an ephemeral P-256 key
//     (crypto/ecdh), HKDF-SHA-256 (crypto/hkdf) and AES-128-GCM;
//   - RFC 8292 VAPID: an ES256 JWT signed with crypto/ecdsa, sent with the
//     application server's public key.
//
// No third-party module is needed: every primitive is in the standard
// library since Go 1.24.

var b64 = base64.RawURLEncoding

// VAPID is the application server's key pair.
type VAPID struct {
	priv    *ecdsa.PrivateKey
	Public  string // base64url uncompressed P-256 point, for applicationServerKey
	Subject string // a mailto: or https: contact, as RFC 8292 asks
}

type vapidFile struct {
	Public  string `json:"public"`
	Private string `json:"private"`
}

// LoadVAPID returns a key pair from base64url strings, or, when both are
// empty, loads or creates one in dir (vapid.json, mode 0600). An empty dir
// with no keys makes a key that lasts only for this process.
func LoadVAPID(public, private, dir, subject string) (*VAPID, error) {
	if public != "" || private != "" {
		return parseVAPID(public, private, subject)
	}
	if dir == "" {
		return newVAPID(subject)
	}
	path := filepath.Join(dir, "vapid.json")
	if raw, err := os.ReadFile(path); err == nil {
		var f vapidFile
		if err := json.Unmarshal(raw, &f); err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		return parseVAPID(f.Public, f.Private, subject)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	v, err := newVAPID(subject)
	if err != nil {
		return nil, err
	}
	d, _ := v.priv.Bytes()
	raw, _ := json.MarshalIndent(vapidFile{Public: v.Public, Private: b64.EncodeToString(d)}, "", "  ")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	if err := writeFileAtomic(path, raw, 0o600); err != nil {
		return nil, err
	}
	return v, nil
}

func newVAPID(subject string) (*VAPID, error) {
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	return vapidFrom(k, subject)
}

func parseVAPID(public, private, subject string) (*VAPID, error) {
	d, err := b64.DecodeString(strings.TrimRight(private, "="))
	if err != nil {
		return nil, fmt.Errorf("VAPID private key is not base64url: %w", err)
	}
	k, err := ecdsa.ParseRawPrivateKey(elliptic.P256(), d)
	if err != nil {
		return nil, fmt.Errorf("VAPID private key: %w", err)
	}
	v, err := vapidFrom(k, subject)
	if err != nil {
		return nil, err
	}
	if public != "" && strings.TrimRight(public, "=") != v.Public {
		return nil, errors.New("VAPID public key does not match the private key")
	}
	return v, nil
}

func vapidFrom(k *ecdsa.PrivateKey, subject string) (*VAPID, error) {
	pub, err := k.PublicKey.Bytes()
	if err != nil {
		return nil, err
	}
	return &VAPID{priv: k, Public: b64.EncodeToString(pub), Subject: subject}, nil
}

// header is the Authorization value for one push service origin.
func (v *VAPID) header(endpoint string, now time.Time) (string, error) {
	u, err := url.Parse(endpoint)
	if err != nil {
		return "", err
	}
	head := b64.EncodeToString([]byte(`{"typ":"JWT","alg":"ES256"}`))
	claims, _ := json.Marshal(map[string]any{
		"aud": u.Scheme + "://" + u.Host,
		"exp": now.Add(12 * time.Hour).Unix(),
		"sub": v.Subject,
	})
	signing := head + "." + b64.EncodeToString(claims)
	sum := sha256.Sum256([]byte(signing))
	r, s, err := ecdsa.Sign(rand.Reader, v.priv, sum[:])
	if err != nil {
		return "", err
	}
	sig := make([]byte, 64)
	r.FillBytes(sig[:32])
	s.FillBytes(sig[32:])
	return "vapid t=" + signing + "." + b64.EncodeToString(sig) + ", k=" + v.Public, nil
}

// Encrypt seals a payload for one subscription (RFC 8291, aes128gcm, one
// record). uaPublic and authSecret are the subscription's p256dh and auth
// keys. salt and asKey may be nil, which picks fresh random ones; tests pass
// the RFC's.
func Encrypt(payload, uaPublic, authSecret, salt []byte, asKey *ecdh.PrivateKey) ([]byte, error) {
	curve := ecdh.P256()
	ua, err := curve.NewPublicKey(uaPublic)
	if err != nil {
		return nil, fmt.Errorf("p256dh: %w", err)
	}
	if len(authSecret) != 16 {
		return nil, errors.New("auth secret must be 16 bytes")
	}
	if asKey == nil {
		if asKey, err = curve.GenerateKey(rand.Reader); err != nil {
			return nil, err
		}
	}
	if salt == nil {
		salt = make([]byte, 16)
		if _, err := rand.Read(salt); err != nil {
			return nil, err
		}
	}
	secret, err := asKey.ECDH(ua)
	if err != nil {
		return nil, err
	}
	asPublic := asKey.PublicKey().Bytes()
	// IKM = HKDF(auth_secret, ecdh_secret, "WebPush: info" || 0 || ua || as, 32)
	keyInfo := "WebPush: info\x00" + string(uaPublic) + string(asPublic)
	prkKey, err := hkdf.Extract(sha256.New, secret, authSecret)
	if err != nil {
		return nil, err
	}
	ikm, err := hkdf.Expand(sha256.New, prkKey, keyInfo, 32)
	if err != nil {
		return nil, err
	}
	prk, err := hkdf.Extract(sha256.New, ikm, salt)
	if err != nil {
		return nil, err
	}
	cek, err := hkdf.Expand(sha256.New, prk, "Content-Encoding: aes128gcm\x00", 16)
	if err != nil {
		return nil, err
	}
	nonce, err := hkdf.Expand(sha256.New, prk, "Content-Encoding: nonce\x00", 12)
	if err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(cek)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	// One record: the payload and the last-record delimiter 0x02.
	plain := append(append([]byte{}, payload...), 0x02)
	sealed := gcm.Seal(nil, nonce, plain, nil)
	var out bytes.Buffer
	out.Write(salt)
	var rs [4]byte
	binary.BigEndian.PutUint32(rs[:], 4096)
	out.Write(rs[:])
	out.WriteByte(byte(len(asPublic)))
	out.Write(asPublic)
	out.Write(sealed)
	return out.Bytes(), nil
}

// ErrGone means the push service says the subscription no longer exists.
var ErrGone = errors.New("push subscription gone")

// Pusher sends encrypted notifications to push services.
type Pusher struct {
	VAPID  *VAPID
	Client *http.Client
	// AllowHost limits the endpoints messages are sent to, so a
	// subscription cannot make the gateway post to an arbitrary host.
	AllowHost func(host string) bool
	TTL       time.Duration
}

// DefaultPushHosts are the push services of the major browsers.
var DefaultPushHosts = []string{
	"fcm.googleapis.com",                // Chrome, Edge on Android, Samsung Internet
	"updates.push.services.mozilla.com", // Firefox
	"push.apple.com",                    // Safari (web.push.apple.com and regional hosts)
	"notify.windows.com",                // Edge on Windows (*.notify.windows.com)
	"push.services.mozilla.com",
}

// HostAllowed reports whether host is one of hosts or a subdomain of one.
func HostAllowed(host string, hosts []string) bool {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	for _, a := range hosts {
		a = strings.ToLower(a)
		if host == a || strings.HasSuffix(host, "."+a) {
			return true
		}
	}
	return false
}

// CheckEndpoint validates a subscription endpoint.
func (p *Pusher) CheckEndpoint(endpoint string) error {
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "https" || u.Host == "" || len(endpoint) > 2048 {
		return errors.New("the push endpoint must be an https URL")
	}
	if p.AllowHost != nil && !p.AllowHost(u.Host) {
		return fmt.Errorf("push endpoints on %s are not accepted", u.Hostname())
	}
	return nil
}

// Send encrypts and posts one message.
func (p *Pusher) Send(ctx context.Context, endpoint, p256dh, auth string, payload []byte, urgency string) error {
	if err := p.CheckEndpoint(endpoint); err != nil {
		return err
	}
	ua, err := b64.DecodeString(strings.TrimRight(p256dh, "="))
	if err != nil {
		return fmt.Errorf("p256dh: %w", err)
	}
	as, err := b64.DecodeString(strings.TrimRight(auth, "="))
	if err != nil {
		return fmt.Errorf("auth: %w", err)
	}
	body, err := Encrypt(payload, ua, as, nil, nil)
	if err != nil {
		return err
	}
	authz, err := p.VAPID.header(endpoint, time.Now())
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	ttl := p.TTL
	if ttl <= 0 {
		ttl = 6 * time.Hour
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("Content-Encoding", "aes128gcm")
	req.Header.Set("TTL", strconv.Itoa(int(ttl.Seconds())))
	req.Header.Set("Urgency", urgency)
	req.Header.Set("Authorization", authz)
	client := p.Client
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
	switch {
	case resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusGone:
		return ErrGone
	case resp.StatusCode/100 != 2:
		return fmt.Errorf("push service answered %d: %s", resp.StatusCode, strings.TrimSpace(string(msg)))
	}
	return nil
}

func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}
