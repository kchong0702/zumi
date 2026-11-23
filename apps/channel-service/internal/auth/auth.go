package auth

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/google/uuid"
	"golang.org/x/oauth2"
	"golang.org/x/oauth2/clientcredentials"

	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
)

// User is the person an app token was issued to.
type User struct {
	// ID is the token's sub, the user's Keycloak id.
	ID string
	// Token is the app's access token, kept to exchange for harness tokens.
	Token string
	// Expiry is when Token expires.
	Expiry time.Time
}

// Verifier checks app tokens locally against the realm's keys.
type Verifier struct {
	verifier *oidc.IDTokenVerifier
}

// NewVerifier checks tokens against the keys at cfg.JWKSURL, cached and fetched again for an unknown key ID,
// allowing cfg.Leeway of clock skew on exp.
func NewVerifier(ctx context.Context, client *http.Client, cfg config.Auth) *Verifier {
	keys := oidc.NewRemoteKeySet(oidc.ClientContext(ctx, client), cfg.JWKSURL)
	return &Verifier{verifier: oidc.NewVerifier(cfg.Issuer, keys, &oidc.Config{
		ClientID:             cfg.Audience,
		SupportedSigningAlgs: []string{oidc.RS256},
		Now:                  func() time.Time { return time.Now().Add(-cfg.Leeway) },
	})}
}

// Verify checks the token's RS256 signature, issuer, audience, expiry and that it is a Keycloak
// access token (typ Bearer) for a user.
func (v *Verifier) Verify(ctx context.Context, token string) (User, error) {
	verified, err := v.verifier.Verify(ctx, token)
	if err != nil {
		return User{}, err
	}
	var claims struct {
		Typ string `json:"typ"`
	}
	if err := verified.Claims(&claims); err != nil {
		return User{}, err
	}
	if claims.Typ != "Bearer" {
		return User{}, fmt.Errorf("token typ is %q, not Bearer", claims.Typ)
	}
	if err := uuid.Validate(verified.Subject); err != nil {
		return User{}, fmt.Errorf("token sub is not a user id: %w", err)
	}
	return User{ID: verified.Subject, Token: token, Expiry: verified.Expiry}, nil
}

// Exchanger trades a user's app token for one aimed at the harness, as the channel service's client.
type Exchanger struct {
	client *http.Client
	cfg    config.Auth
}

// NewExchanger exchanges tokens at cfg.TokenURL as cfg.ClientID.
func NewExchanger(client *http.Client, cfg config.Auth) *Exchanger {
	return &Exchanger{client: client, cfg: cfg}
}

// Exchange returns an access token for cfg.HarnessAudience on behalf of the subject token's user.
func (e *Exchanger) Exchange(ctx context.Context, subjectToken string) (string, error) {
	exchange := clientcredentials.Config{
		ClientID:     e.cfg.ClientID,
		ClientSecret: e.cfg.ClientSecret,
		TokenURL:     e.cfg.TokenURL,
		AuthStyle:    oauth2.AuthStyleInHeader,
		EndpointParams: url.Values{
			"grant_type":         {"urn:ietf:params:oauth:grant-type:token-exchange"},
			"subject_token":      {subjectToken},
			"subject_token_type": {"urn:ietf:params:oauth:token-type:access_token"},
			"audience":           {e.cfg.HarnessAudience},
		},
	}
	token, err := exchange.Token(context.WithValue(ctx, oauth2.HTTPClient, e.client))
	if err != nil {
		return "", fmt.Errorf("exchange the token: %w", err)
	}
	return token.AccessToken, nil
}
