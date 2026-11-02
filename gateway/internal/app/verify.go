package app

import (
	"context"
	"net/http"
	"path/filepath"
	"strconv"
	"time"

	"neermattam/gateway/internal/certs"
	"neermattam/gateway/internal/photos"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/web"
)

// Certificates kept: on disk under GATEWAY_DATA_DIR, or in memory when
// persistence is off.
const (
	certsOnDisk   = 20_000
	certsInMemory = 2_000
	// keysRefresh is how often perception's keys are fetched; keysRetry is
	// the least time between fetches made because a certificate named a key
	// the gateway did not know.
	keysRefresh = 10 * time.Minute
	keysRetry   = 30 * time.Second
)

// setupCertificates makes the certificate store and the trusted key set:
// keys pinned in CERTIFICATE_PUBLIC_KEYS, plus those fetched from
// perception before (kept in certificate-keys.json).
func (a *App) setupCertificates() error {
	dir := ""
	if a.Cfg.DataDir != "" {
		dir = filepath.Join(a.Cfg.DataDir, certs.DirName)
	}
	a.Certs = certs.NewStore(dir, certsInMemory, certsOnDisk)
	keys, err := certs.NewKeySet(a.Cfg.DataDir)
	if err != nil {
		a.Log.Warn("saved certificate keys not loaded", "error", err.Error())
	}
	for _, k := range a.Cfg.CertificatePublicKeys {
		id, err := keys.AddPublic(k, certs.SourceConfig)
		if err != nil {
			a.Log.Warn("CERTIFICATE_PUBLIC_KEYS holds a value that is not an Ed25519 public key; ignored")
			continue
		}
		a.Log.Info("certificate key pinned", "key_id", id)
	}
	a.CertKeys = keys
	return nil
}

// fetchCertKeys asks perception for its public keys and trusts the new
// ones. force skips the throttle used for on-demand fetches. Only on-demand
// fetches start the throttle, so the scheduled fetch at start-up never blocks
// the first certificate that names a key perception published later.
func (a *App) fetchCertKeys(ctx context.Context, force bool) {
	if !force {
		a.keysMu.Lock()
		if time.Since(a.keysFetched) < keysRetry {
			a.keysMu.Unlock()
			return
		}
		a.keysFetched = time.Now()
		a.keysMu.Unlock()
	}
	doc, err := a.Perception.Keys(ctx)
	if err != nil {
		a.Log.Debug("perception keys not fetched", "error", err.Error())
		return
	}
	n, err := a.CertKeys.AddDocument(doc, certs.SourcePerception)
	if err != nil {
		a.Log.Warn("perception keys not readable", "error", err.Error())
		return
	}
	if n > 0 {
		a.Log.Info("certificate keys fetched from perception", "new", n)
		if a.Cfg.DataDir != "" {
			if err := a.CertKeys.Save(); err != nil {
				a.Log.Warn("certificate keys not saved", "error", err.Error())
			}
		}
	}
}

// certKeysRefresh fetches perception's keys at start and then regularly.
func (a *App) certKeysRefresh(ctx context.Context) {
	a.fetchCertKeys(ctx, true)
	t := time.NewTicker(keysRefresh)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			a.fetchCertKeys(ctx, true)
		}
	}
}

// checkCertificate verifies a stored certificate. A key the gateway does
// not know yet (perception's key changed since the last fetch) triggers
// one throttled fetch from perception, never trust in the certificate's
// own claim.
func (a *App) checkCertificate(ctx context.Context, st certs.Stored) certs.Result {
	res := certs.Verify(st.Certificate, a.CertKeys)
	if res.Status == certs.UnknownKey {
		fctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		a.fetchCertKeys(fctx, false)
		cancel()
		res = certs.Verify(st.Certificate, a.CertKeys)
	}
	return res
}

// verifyPage is GET /verify/{id}: "Verify this measurement".
func (a *App) verifyPage(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	p := a.page(w, r, "verify", false)
	p.Footer = true
	st, ok := a.certificate(id)
	if !ok {
		p.Verify = &web.VerifyView{ID: id, Missing: true}
		a.render(w, http.StatusNotFound, "verify", p)
		return
	}
	res := a.checkCertificate(r.Context(), st)
	p.Verify = web.NewVerifyView(p.Lang, id, st, res, a.Cfg.PublicURL)
	if ph, ok := a.Photos.ByMeasurement(id); ok {
		p.Verify.Archival = &web.ArchivalView{Year: ph.Year(), MeasuredOn: web.Day(p.Lang, measuredAt(ph)), Place: ph.Place, City: ph.City, Credit: ph.Credit(), SourceURL: ph.SourceURL, LicenceURL: ph.LicenceURL}
	}
	a.render(w, http.StatusOK, "verify", p)
}

// apiCertificate is GET /api/certificates/{id}: the certificate as
// perception signed it, with the gateway's verdict in a header.
func (a *App) apiCertificate(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	st, ok := a.certificate(r.PathValue("id"))
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "This gateway holds no certificate for that measurement.", "code": "no_certificate"})
		return
	}
	res := a.checkCertificate(r.Context(), st)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=300")
	w.Header().Set("X-Neermattam-Verified", strconv.FormatBool(res.Valid()))
	w.Header().Set("Content-Disposition", `inline; filename="`+r.PathValue("id")+`.certificate.json"`)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(st.Certificate)
}

// wellKnownKeys is GET /.well-known/neermattam-keys.json.
func (a *App) wellKnownKeys(w http.ResponseWriter, r *http.Request) {
	if a.CertKeys.Len() == 0 {
		fctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		a.fetchCertKeys(fctx, false)
		cancel()
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=300")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	_, _ = w.Write(a.CertKeys.Document())
}

// certificate finds a stored report certificate, or one of the measured
// archival photos' certificates.
func (a *App) certificate(id string) (certs.Stored, bool) {
	if st, ok := a.Certs.Get(id); ok {
		return st, true
	}
	ph, ok := a.Photos.ByMeasurement(id)
	if !ok || len(ph.Measurement.Certificate) == 0 {
		return certs.Stored{}, false
	}
	m := ph.Measurement
	at, exact := ph.MeasuredAt()
	st := certs.Stored{Certificate: m.Certificate, UploadSHA256: ph.FileSHA256, ReceivedAt: at, ReceivedDateOnly: !exact}
	if len(m.ReasonCodes) == len(m.Reasons) {
		st.ReasonCodes = m.ReasonCodes
	}
	for _, step := range m.AgentTrace {
		if step.DecisionCode != nil {
			st.StepCodes = make([]*upstream.Code, len(m.AgentTrace))
			for i := range m.AgentTrace {
				st.StepCodes[i] = m.AgentTrace[i].DecisionCode
			}
			break
		}
	}
	st.Decision = &certs.Decision{Status: certs.DecisionArchival, DecidedAt: at}
	return st, true
}

// measuredAt is when an archival photo was measured, for a date.
func measuredAt(ph photos.Photo) time.Time {
	t, _ := ph.MeasuredAt()
	return t
}
