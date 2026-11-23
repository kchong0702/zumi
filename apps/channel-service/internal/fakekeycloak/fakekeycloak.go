package fakekeycloak

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	"github.com/google/uuid"

	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
)

// ClientSecret is the secret the fake realm expects from the channel service's client.
const ClientSecret = "test-channel-secret"

// Exchange is one token exchange request the fake realm answered.
type Exchange struct {
	// ClientID and ClientSecret are the client credentials from the Authorization header.
	ClientID, ClientSecret string
	// Form is the request's form.
	Form url.Values
}

// Server is a fake zumi realm: it signs tokens, serves their keys and exchanges tokens.
type Server struct {
	*httptest.Server
	mu        sync.Mutex
	key       *rsa.PrivateKey
	keyID     string
	exchanges []Exchange
}

// HarnessToken is the token the fake realm exchanges a user's app token for.
func HarnessToken(userID string) string {
	return "harness-token-for-" + userID
}

// New starts a fake realm that stops when the test ends.
func New(t testing.TB) *Server {
	s := &Server{}
	s.RotateKey(t)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /certs", func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		keys := jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &s.key.PublicKey, KeyID: s.keyID, Algorithm: "RS256", Use: "sig"}}}
		s.mu.Unlock()
		_ = json.NewEncoder(w).Encode(keys)
	})
	mux.HandleFunc("POST /token", func(w http.ResponseWriter, r *http.Request) {
		id, secret, _ := r.BasicAuth()
		if err := r.ParseForm(); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		s.mu.Lock()
		s.exchanges = append(s.exchanges, Exchange{ClientID: id, ClientSecret: secret, Form: r.PostForm})
		s.mu.Unlock()
		subject, err := jwt.ParseSigned(r.PostForm.Get("subject_token"), []jose.SignatureAlgorithm{jose.RS256})
		var claims jwt.Claims
		if err != nil || subject.UnsafeClaimsWithoutVerification(&claims) != nil || secret != ClientSecret {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error":"invalid_request"}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"access_token": HarnessToken(claims.Subject), "token_type": "Bearer", "expires_in": 300})
	})
	s.Server = httptest.NewServer(mux)
	t.Cleanup(s.Close)
	return s
}

// Config points the channel service's auth settings at the fake realm.
func (s *Server) Config() config.Auth {
	return config.Auth{
		Issuer:          s.URL,
		JWKSURL:         s.URL + "/certs",
		TokenURL:        s.URL + "/token",
		Audience:        "zumi-channel",
		ClientID:        "zumi-channel",
		ClientSecret:    ClientSecret,
		HarnessAudience: "zumi-harness",
		Leeway:          30 * time.Second,
	}
}

// RotateKey makes the realm sign with, and publish only, a new key.
func (s *Server) RotateKey(t testing.TB) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	s.key, s.keyID = key, uuid.NewString()
	s.mu.Unlock()
}

// Token signs an app access token for userID, with overrides replacing or, when nil, removing claims.
func (s *Server) Token(t testing.TB, userID string, overrides map[string]any) string {
	now := time.Now()
	claims := map[string]any{
		"iss": s.URL,
		"aud": []string{"zumi-channel", "account"},
		"azp": "zumi-app",
		"typ": "Bearer",
		"sub": userID,
		"iat": now.Unix(),
		"exp": now.Add(5 * time.Minute).Unix(),
	}
	for key, value := range overrides {
		if value == nil {
			delete(claims, key)
			continue
		}
		claims[key] = value
	}
	s.mu.Lock()
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: jose.JSONWebKey{Key: s.key, KeyID: s.keyID}}, (&jose.SignerOptions{}).WithType("JWT"))
	s.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	token, err := jwt.Signed(signer).Claims(claims).Serialize()
	if err != nil {
		t.Fatal(err)
	}
	return token
}

// Exchanges returns every token exchange request so far.
func (s *Server) Exchanges() []Exchange {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Exchange(nil), s.exchanges...)
}
