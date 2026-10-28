package config

import (
	"strings"
	"testing"
	"time"
)

func TestDefaults(t *testing.T) {
	c, err := FromMap(nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.Addr != ":8080" || c.EngineURL != "http://127.0.0.1:8302" || c.PerceptionURL != "http://127.0.0.1:8301" {
		t.Errorf("unexpected defaults: %+v", c)
	}
	if c.MaxImageBytes != 12<<20 || c.MaxVideoBytes != 40<<20 || c.MaxVideoSeconds != 30 {
		t.Errorf("upload limits: %d %d %v", c.MaxImageBytes, c.MaxVideoBytes, c.MaxVideoSeconds)
	}
	if c.Demo || c.WhatsApp.Enabled() {
		t.Error("demo and WhatsApp must be off by default")
	}
	if !c.ServiceArea.Contains(12.9791, 80.2209) || c.ServiceArea.Contains(13.5, 80.2) {
		t.Error("service area does not cover Chennai as expected")
	}
}

func TestOverridesAndErrors(t *testing.T) {
	c, err := FromMap(map[string]string{"DEMO": "1", "ENGINE_TIMEOUT": "750ms", "ENGINE_URL": "http://engine:8302/"})
	if err != nil {
		t.Fatal(err)
	}
	if !c.Demo || c.EngineTimeout != 750*time.Millisecond || c.EngineURL != "http://engine:8302" {
		t.Errorf("overrides not applied: %+v", c)
	}
	for k, v := range map[string]string{
		"DEMO": "maybe", "ENGINE_TIMEOUT": "soon", "MAX_IMAGE_BYTES": "-1", "GATEWAY_BBOX": "1,2,3", "MAX_SSE_CLIENTS": "0",
	} {
		if _, err := FromMap(map[string]string{k: v}); err == nil || !strings.Contains(err.Error(), k[:4]) {
			t.Errorf("%s=%s: want an error naming the variable, got %v", k, v, err)
		}
	}
}

func TestWhatsAppEnabledOnlyWithAllSecrets(t *testing.T) {
	w := WhatsApp{VerifyToken: "v", AppSecret: "s"}
	if w.Enabled() {
		t.Fatal("enabled without an access token")
	}
	if got := w.Missing(); len(got) != 1 || got[0] != "WHATSAPP_TOKEN" {
		t.Fatalf("missing = %v", got)
	}
	w.AccessToken = "t"
	if !w.Enabled() {
		t.Fatal("should be enabled")
	}
}

func TestReplyDeadlineCoversPerception(t *testing.T) {
	c, _ := FromMap(nil)
	if c.PerceptionTimeout != 240*time.Second || c.ReplyDeadline != 270*time.Second {
		t.Fatalf("defaults %v %v", c.PerceptionTimeout, c.ReplyDeadline)
	}
	if _, err := FromMap(map[string]string{"REPLY_DEADLINE": "200s"}); err == nil {
		t.Fatal("a reply deadline shorter than the perception timeout must be refused")
	}
	c, err := FromMap(map[string]string{"REPLY_DEADLINE": "6m", "PERCEPTION_TIMEOUT": "5m"})
	if err != nil || c.ReplyDeadline != 6*time.Minute {
		t.Fatalf("%v %v", c.ReplyDeadline, err)
	}
}
