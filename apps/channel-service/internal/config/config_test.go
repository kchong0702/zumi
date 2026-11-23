package config_test

import (
	"strings"
	"testing"

	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
)

func TestLoad(t *testing.T) {
	tests := []struct {
		name    string
		env     map[string]string
		wantErr string
	}{
		{name: "the checked-in config"},
		{name: "reconnect_max below reconnect_initial", env: map[string]string{"RELAY__RECONNECT_MAX": "500ms"}, wantErr: "relay.reconnect_max"},
		{name: "give_up_after not above reconnect_initial", env: map[string]string{"RELAY__GIVE_UP_AFTER": "1s"}, wantErr: "relay.give_up_after"},
		{name: "ping_interval not above write_timeout", env: map[string]string{"WEBSOCKET__PING_INTERVAL": "10s"}, wantErr: "websocket.ping_interval"},
		{name: "an unknown env", env: map[string]string{"APP__ENV": "production"}, wantErr: "app.env"},
		{name: "no allowed origins", env: map[string]string{"WEBSOCKET__ALLOWED_ORIGINS": ""}, wantErr: "websocket.allowed_origins"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv(config.EnvPrefix+"AUTH__CLIENT_SECRET", "secret")
			for key, value := range test.env {
				t.Setenv(config.EnvPrefix+key, value)
			}
			_, err := config.Load("../../config.toml")
			if test.wantErr == "" && err != nil {
				t.Fatalf("Load() = %v, want no error", err)
			}
			if test.wantErr != "" && (err == nil || !strings.Contains(err.Error(), test.wantErr)) {
				t.Fatalf("Load() = %v, want an error about %s", err, test.wantErr)
			}
		})
	}
}

func TestLoadTrimsTheHarnessURL(t *testing.T) {
	t.Setenv(config.EnvPrefix+"AUTH__CLIENT_SECRET", "secret")
	t.Setenv(config.EnvPrefix+"HARNESS__URL", "http://harness:8000/")
	cfg, err := config.Load("../../config.toml")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Harness.URL != "http://harness:8000" {
		t.Fatalf("harness.url = %q, want it without the trailing /", cfg.Harness.URL)
	}
}
