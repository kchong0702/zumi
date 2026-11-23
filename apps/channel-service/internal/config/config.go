package config

import (
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"path/filepath"
	"strings"
	"time"

	"github.com/go-viper/mapstructure/v2"
	"github.com/knadh/koanf/parsers/dotenv"
	"github.com/knadh/koanf/parsers/toml/v2"
	"github.com/knadh/koanf/providers/env/v2"
	"github.com/knadh/koanf/providers/file"
	"github.com/knadh/koanf/v2"
)

// EnvPrefix starts every environment variable that overrides config.toml.
const EnvPrefix = "ZUMI_CHANNEL_"

// Config holds every setting of the channel service.
type Config struct {
	App           App           `koanf:"app"`
	Observability Observability `koanf:"observability"`
	Database      Database      `koanf:"database"`
	Auth          Auth          `koanf:"auth"`
	Harness       Harness       `koanf:"harness"`
	Relay         Relay         `koanf:"relay"`
	WebSocket     WebSocket     `koanf:"websocket"`
}

// App holds where the server listens and how long it waits.
type App struct {
	// Env is where the service runs: dev, staging or prod.
	Env string `koanf:"env"`
	// Version is the service's version, reported by /health and on telemetry.
	Version string `koanf:"version"`
	// Host is the address the server binds to.
	Host string `koanf:"host"`
	// Port is the port the server listens on.
	Port int `koanf:"port"`
	// HeaderTimeout bounds reading a request's headers, and waiting for the headers of Keycloak's
	// or the harness's response.
	HeaderTimeout time.Duration `koanf:"header_timeout"`
	// ShutdownTimeout bounds stopping the server and flushing telemetry.
	ShutdownTimeout time.Duration `koanf:"shutdown_timeout"`
}

// Observability holds logging, tracing and metrics settings.
type Observability struct {
	// ServiceName names the service on every log line, span and metric.
	ServiceName string `koanf:"service_name"`
	// LogLevel is the lowest level logged: debug, info, warn or error.
	LogLevel slog.Level `koanf:"log_level"`
	// LogFormat is json for machines or text for people.
	LogFormat string `koanf:"log_format"`
	// TracingEnabled exports traces over OTLP HTTP to OTLPEndpoint.
	TracingEnabled bool `koanf:"tracing_enabled"`
	// MetricsEnabled exports metrics over OTLP HTTP to OTLPEndpoint.
	MetricsEnabled bool `koanf:"metrics_enabled"`
	// MetricsExportInterval is the time between metric exports.
	MetricsExportInterval time.Duration `koanf:"metrics_export_interval"`
	// OTLPEndpoint is the collector's OTLP HTTP base URL, such as http://localhost:4318.
	OTLPEndpoint string `koanf:"otlp_endpoint"`
}

// Database holds the Postgres connection.
type Database struct {
	// URL is the Postgres connection string.
	URL string `koanf:"url"`
	// SchemaName is the schema that holds the service's tables.
	SchemaName string `koanf:"schema_name"`
}

// Auth holds how app tokens are checked and exchanged for harness tokens.
type Auth struct {
	// Issuer is the realm every app token must be issued by.
	Issuer string `koanf:"issuer"`
	// JWKSURL is where the realm's signing keys are fetched from.
	JWKSURL string `koanf:"jwks_url"`
	// TokenURL is the realm's token endpoint, used for token exchange.
	TokenURL string `koanf:"token_url"`
	// Audience is the audience every app token must name.
	Audience string `koanf:"audience"`
	// ClientID is the channel service's own Keycloak client.
	ClientID string `koanf:"client_id"`
	// ClientSecret is the secret of ClientID, set only in the environment.
	ClientSecret string `koanf:"client_secret"`
	// HarnessAudience is the audience of the token exchanged for each harness call.
	HarnessAudience string `koanf:"harness_audience"`
	// Leeway is the clock skew allowed when checking a token's exp.
	Leeway time.Duration `koanf:"leeway"`
}

// Harness holds where the harness is and which agent runs the app's messages.
type Harness struct {
	// URL is the harness's base URL. Load drops a trailing /.
	URL string `koanf:"url"`
	// Agent is the agent every app message goes to.
	Agent string `koanf:"agent"`
	// IdleTimeout drops a run's stream after this long without a byte, pings included.
	IdleTimeout time.Duration `koanf:"idle_timeout"`
	// MaxEventBytes is the largest event the stream may carry.
	MaxEventBytes int `koanf:"max_event_bytes"`
	// CancelTimeout bounds asking the harness to cancel a run, token exchange included.
	CancelTimeout time.Duration `koanf:"cancel_timeout"`
}

// Relay holds how a dropped run stream is reconnected.
type Relay struct {
	// ReconnectInitial is the first wait before reconnecting.
	ReconnectInitial time.Duration `koanf:"reconnect_initial"`
	// ReconnectMax is the longest wait between reconnects, before jitter.
	ReconnectMax time.Duration `koanf:"reconnect_max"`
	// GiveUpAfter ends the run as an error when reconnecting fails for this long.
	GiveUpAfter time.Duration `koanf:"give_up_after"`
	// PersistInterval is the shortest time between two writes of a run's last relayed event.
	PersistInterval time.Duration `koanf:"persist_interval"`
}

// WebSocket holds the app socket's settings.
type WebSocket struct {
	// AllowedOrigins lists the Origin headers a socket may come from, such as tauri://localhost.
	AllowedOrigins []string `koanf:"allowed_origins"`
	// AuthTimeout is how long a socket without an Authorization header has to send its auth message.
	AuthTimeout time.Duration `koanf:"auth_timeout"`
	// SendBuffer is how many messages may wait for a socket before it is closed as too slow.
	SendBuffer int `koanf:"send_buffer"`
	// WriteTimeout closes a socket that takes longer than this to take a message or answer a ping.
	WriteTimeout time.Duration `koanf:"write_timeout"`
	// PingInterval is the time between pings that check the app is still there.
	PingInterval time.Duration `koanf:"ping_interval"`
	// MaxUnauthenticated is how many sockets may wait for their auth message at once.
	MaxUnauthenticated int `koanf:"max_unauthenticated"`
}

func envKey(name string) string {
	key := strings.TrimPrefix(name, EnvPrefix)
	if !strings.Contains(key, "__") {
		return ""
	}
	return strings.ReplaceAll(strings.ToLower(key), "__", ".")
}

func (c Config) validate() error {
	var errs []error
	required := map[string]string{
		"app.host":                   c.App.Host,
		"database.url":               c.Database.URL,
		"database.schema_name":       c.Database.SchemaName,
		"auth.issuer":                c.Auth.Issuer,
		"auth.jwks_url":              c.Auth.JWKSURL,
		"auth.token_url":             c.Auth.TokenURL,
		"auth.audience":              c.Auth.Audience,
		"auth.client_id":             c.Auth.ClientID,
		"auth.client_secret":         c.Auth.ClientSecret,
		"auth.harness_audience":      c.Auth.HarnessAudience,
		"harness.url":                c.Harness.URL,
		"harness.agent":              c.Harness.Agent,
		"observability.service_name": c.Observability.ServiceName,
	}
	for key, value := range required {
		if value == "" {
			errs = append(errs, fmt.Errorf("%s is required", key))
		}
	}
	if c.Auth.Leeway < 0 {
		errs = append(errs, errors.New("auth.leeway must not be negative"))
	}
	if c.App.Port <= 0 {
		errs = append(errs, errors.New("app.port must be positive"))
	}
	if c.Observability.LogFormat != "json" && c.Observability.LogFormat != "text" {
		errs = append(errs, errors.New("observability.log_format must be json or text"))
	}
	if (c.Observability.TracingEnabled || c.Observability.MetricsEnabled) && c.Observability.OTLPEndpoint == "" {
		errs = append(errs, errors.New("observability.otlp_endpoint is required when tracing or metrics are enabled"))
	}
	for key, d := range map[string]time.Duration{
		"app.header_timeout":                    c.App.HeaderTimeout,
		"app.shutdown_timeout":                  c.App.ShutdownTimeout,
		"observability.metrics_export_interval": c.Observability.MetricsExportInterval,
		"harness.idle_timeout":                  c.Harness.IdleTimeout,
		"harness.cancel_timeout":                c.Harness.CancelTimeout,
		"relay.reconnect_initial":               c.Relay.ReconnectInitial,
		"relay.reconnect_max":                   c.Relay.ReconnectMax,
		"relay.give_up_after":                   c.Relay.GiveUpAfter,
		"relay.persist_interval":                c.Relay.PersistInterval,
		"websocket.auth_timeout":                c.WebSocket.AuthTimeout,
		"websocket.write_timeout":               c.WebSocket.WriteTimeout,
		"websocket.ping_interval":               c.WebSocket.PingInterval,
	} {
		if d <= 0 {
			errs = append(errs, fmt.Errorf("%s must be positive", key))
		}
	}
	if c.Harness.MaxEventBytes <= 0 || c.WebSocket.SendBuffer <= 0 || c.WebSocket.MaxUnauthenticated <= 0 {
		errs = append(errs, errors.New("harness.max_event_bytes, websocket.send_buffer and websocket.max_unauthenticated must be positive"))
	}
	if c.App.Env != "dev" && c.App.Env != "staging" && c.App.Env != "prod" {
		errs = append(errs, errors.New("app.env must be dev, staging or prod"))
	}
	if c.Relay.ReconnectMax < c.Relay.ReconnectInitial {
		errs = append(errs, errors.New("relay.reconnect_max must not be shorter than relay.reconnect_initial"))
	}
	if c.Relay.GiveUpAfter <= c.Relay.ReconnectInitial {
		errs = append(errs, errors.New("relay.give_up_after must be longer than relay.reconnect_initial"))
	}
	if c.WebSocket.PingInterval <= c.WebSocket.WriteTimeout {
		errs = append(errs, errors.New("websocket.ping_interval must be longer than websocket.write_timeout"))
	}
	if len(c.WebSocket.AllowedOrigins) == 0 {
		errs = append(errs, errors.New("websocket.allowed_origins must list at least one origin, such as tauri://localhost"))
	}
	return errors.Join(errs...)
}

// Load reads path, then the .env file next to it, then the environment, each overriding the one before.
// A variable such as ZUMI_CHANNEL_APP__PORT sets [app] port.
func Load(path string) (Config, error) {
	k := koanf.New(".")
	if err := k.Load(file.Provider(path), toml.Parser()); err != nil {
		return Config{}, fmt.Errorf("read %s: %w", path, err)
	}
	dotenvPath := filepath.Join(filepath.Dir(path), ".env")
	if err := k.Load(file.Provider(dotenvPath), dotenv.ParserEnv(EnvPrefix, ".", envKey)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return Config{}, fmt.Errorf("read %s: %w", dotenvPath, err)
	}
	if err := k.Load(env.Provider(".", env.Opt{Prefix: EnvPrefix, TransformFunc: func(k, v string) (string, any) { return envKey(k), v }}), nil); err != nil {
		return Config{}, fmt.Errorf("read the environment: %w", err)
	}
	var c Config
	err := k.UnmarshalWithConf("", &c, koanf.UnmarshalConf{DecoderConfig: &mapstructure.DecoderConfig{
		DecodeHook: mapstructure.ComposeDecodeHookFunc(
			mapstructure.StringToTimeDurationHookFunc(),
			mapstructure.StringToSliceHookFunc(","),
			mapstructure.TextUnmarshallerHookFunc(),
		),
		WeaklyTypedInput: true,
		ErrorUnused:      true,
	}})
	if err != nil {
		return Config{}, fmt.Errorf("parse config: %w", err)
	}
	c.Harness.URL = strings.TrimRight(c.Harness.URL, "/")
	return c, c.validate()
}
