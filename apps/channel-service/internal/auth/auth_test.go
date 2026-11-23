package auth_test

import (
	"context"
	"net/http"
	"net/url"
	"reflect"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	"github.com/google/uuid"

	"github.com/kchong0702/zumi/apps/channel-service/internal/auth"
	"github.com/kchong0702/zumi/apps/channel-service/internal/fakekeycloak"
)

func TestVerify(t *testing.T) {
	realm := fakekeycloak.New(t)
	verifier := auth.NewVerifier(context.Background(), http.DefaultClient, realm.Config())
	userID := uuid.NewString()
	hs256, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.HS256, Key: []byte("a-shared-secret-of-thirty-two-bytes")}, nil)
	if err != nil {
		t.Fatal(err)
	}
	unsigned, err := jwt.Signed(hs256).Claims(map[string]any{"iss": realm.URL, "aud": "zumi-channel", "typ": "Bearer", "sub": userID, "exp": time.Now().Add(time.Minute).Unix()}).Serialize()
	if err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name  string
		token string
		ok    bool
	}{
		{"an app access token", realm.Token(t, userID, nil), true},
		{"a token for another audience", realm.Token(t, userID, map[string]any{"aud": "zumi-harness"}), false},
		{"a token from another issuer", realm.Token(t, userID, map[string]any{"iss": "http://localhost:8180/realms/other"}), false},
		{"a token expired within the leeway", realm.Token(t, userID, map[string]any{"exp": time.Now().Add(-10 * time.Second).Unix()}), true},
		{"a token expired beyond the leeway", realm.Token(t, userID, map[string]any{"exp": time.Now().Add(-time.Minute).Unix()}), false},
		{"a token without exp", realm.Token(t, userID, map[string]any{"exp": nil}), false},
		{"an ID token", realm.Token(t, userID, map[string]any{"typ": "ID"}), false},
		{"a token whose sub is not a user id", realm.Token(t, userID, map[string]any{"sub": "service-account-zumi-channel"}), false},
		{"an HS256 token", unsigned, false},
		{"garbage", "not-a-jwt", false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			user, err := verifier.Verify(context.Background(), test.token)
			if test.ok != (err == nil) {
				t.Fatalf("Verify() error = %v, want ok %v", err, test.ok)
			}
			if test.ok && (user.ID != userID || user.Token != test.token || user.Expiry.IsZero()) {
				t.Fatalf("Verify() = %+v, want user %s with the token and its expiry", user, userID)
			}
		})
	}
}

func TestVerifyFetchesKeysAgainForAnUnknownKeyID(t *testing.T) {
	realm := fakekeycloak.New(t)
	verifier := auth.NewVerifier(context.Background(), http.DefaultClient, realm.Config())
	userID := uuid.NewString()
	if _, err := verifier.Verify(context.Background(), realm.Token(t, userID, nil)); err != nil {
		t.Fatal(err)
	}
	realm.RotateKey(t)
	if _, err := verifier.Verify(context.Background(), realm.Token(t, userID, nil)); err != nil {
		t.Fatalf("Verify() with a rotated key = %v, want ok", err)
	}
}

func TestExchange(t *testing.T) {
	realm := fakekeycloak.New(t)
	exchanger := auth.NewExchanger(http.DefaultClient, realm.Config())
	userID := uuid.NewString()
	subject := realm.Token(t, userID, nil)

	token, err := exchanger.Exchange(context.Background(), subject)
	if err != nil {
		t.Fatal(err)
	}
	if token != fakekeycloak.HarnessToken(userID) {
		t.Fatalf("Exchange() = %q, want the realm's harness token", token)
	}
	want := fakekeycloak.Exchange{
		ClientID:     "zumi-channel",
		ClientSecret: fakekeycloak.ClientSecret,
		Form: url.Values{
			"grant_type":         {"urn:ietf:params:oauth:grant-type:token-exchange"},
			"subject_token":      {subject},
			"subject_token_type": {"urn:ietf:params:oauth:token-type:access_token"},
			"audience":           {"zumi-harness"},
		},
	}
	if got := realm.Exchanges(); len(got) != 1 || !reflect.DeepEqual(got[0], want) {
		t.Fatalf("exchange requests = %+v, want one %+v", got, want)
	}
}

func TestExchangeRefused(t *testing.T) {
	realm := fakekeycloak.New(t)
	cfg := realm.Config()
	cfg.ClientSecret = "wrong"
	if _, err := auth.NewExchanger(http.DefaultClient, cfg).Exchange(context.Background(), realm.Token(t, uuid.NewString(), nil)); err == nil {
		t.Fatal("Exchange() with the wrong client secret succeeded")
	}
}
