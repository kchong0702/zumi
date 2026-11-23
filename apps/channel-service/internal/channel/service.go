package channel

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/google/uuid"

	"github.com/kchong0702/zumi/apps/channel-service/internal/auth"
	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
	"github.com/kchong0702/zumi/apps/channel-service/internal/harness"
	"github.com/kchong0702/zumi/apps/channel-service/internal/store"
)

// Runs records the runs the service relays. *store.Store is the one in use.
type Runs interface {
	Run(ctx context.Context, id string) (store.Run, error)
	StartRun(ctx context.Context, run store.Run) error
	RelayedEvent(ctx context.Context, id string, eventID int64) error
	FinishRun(ctx context.Context, id string, status store.RunStatus, eventID int64) error
}

// Service serves the app WebSocket and relays each user's runs to every socket the user has open.
type Service struct {
	cfg             config.Config
	verifier        *auth.Verifier
	exchanger       *auth.Exchanger
	harness         *harness.Client
	runs            Runs
	ctx             context.Context
	stop            context.CancelFunc
	relaysDone      sync.WaitGroup
	unauthenticated chan struct{}

	mu      sync.Mutex
	sockets map[string]map[*socket]struct{}
	relays  map[string]*relay
}

// New returns a Service with no sockets open.
func New(cfg config.Config, verifier *auth.Verifier, exchanger *auth.Exchanger, client *harness.Client, runs Runs) *Service {
	ctx, stop := context.WithCancel(context.Background())
	return &Service{
		cfg:             cfg,
		verifier:        verifier,
		exchanger:       exchanger,
		harness:         client,
		runs:            runs,
		ctx:             ctx,
		stop:            stop,
		unauthenticated: make(chan struct{}, cfg.WebSocket.MaxUnauthenticated),
		sockets:         map[string]map[*socket]struct{}{},
		relays:          map[string]*relay{},
	}
}

// Close closes every socket, refuses new relays and waits for every relay to stop.
// The harness keeps running the runs.
func (s *Service) Close() {
	s.mu.Lock()
	s.stop()
	s.mu.Unlock()
	s.relaysDone.Wait()
}

func (s *Service) handshake(ctx context.Context, conn *websocket.Conn) (auth.User, error) {
	timeout := time.AfterFunc(s.cfg.WebSocket.AuthTimeout, func() { conn.Close(statusUnauthorized, "no auth message") })
	defer timeout.Stop()
	_, data, err := conn.Read(ctx)
	if err != nil {
		return auth.User{}, err
	}
	var m inbound
	if err := json.Unmarshal(data, &m); err != nil {
		return auth.User{}, err
	}
	if m.Type != "auth" {
		return auth.User{}, fmt.Errorf("the first message is %q, not auth", m.Type)
	}
	return s.verifier.Verify(ctx, m.Token)
}

func (s *Service) harnessToken(ctx context.Context, userID string) (string, error) {
	s.mu.Lock()
	var subject string
	var expiry time.Time
	for sock := range s.sockets[userID] {
		if sock.expiry.After(expiry) {
			subject, expiry = sock.token, sock.expiry
		}
	}
	s.mu.Unlock()
	if subject == "" {
		return "", errors.New("the user has no socket open")
	}
	return s.exchanger.Exchange(ctx, subject)
}

func (s *Service) cancelAtHarness(ctx context.Context, run store.Run) error {
	token, err := s.harnessToken(ctx, run.UserID)
	if err != nil {
		return err
	}
	return s.harness.Cancel(ctx, token, run.Agent, run.ID)
}

func (s *Service) ownRun(ctx context.Context, sock *socket, runID string) (store.Run, bool) {
	run, err := s.runs.Run(ctx, runID)
	if errors.Is(err, store.ErrNotFound) || (err == nil && run.UserID != sock.userID) {
		sock.fail(runID, codeNotFound, errorMessages[codeNotFound])
		return store.Run{}, false
	}
	if err != nil {
		slog.ErrorContext(ctx, "could not read a run", "run_id", runID, "error", err)
		sock.fail(runID, codeInternal, errorMessages[codeInternal])
		return store.Run{}, false
	}
	return run, true
}

func (s *Service) startRun(ctx context.Context, sock *socket, m inbound) {
	if uuid.Validate(m.ID) != nil {
		sock.fail(m.ID, codeBadRequest, "A message's id must be a UUID.")
		return
	}
	if strings.TrimSpace(m.Text) == "" {
		sock.fail(m.ID, codeBadRequest, "A message's text must not be empty.")
		return
	}
	run, err := s.runs.Run(ctx, m.ID)
	switch {
	case errors.Is(err, store.ErrNotFound):
		s.startRelay(&relay{run: store.Run{ID: m.ID, UserID: sock.userID, Agent: s.cfg.Harness.Agent, Status: store.RunRunning}, text: m.Text}, false)
	case err != nil:
		slog.ErrorContext(ctx, "could not read a run", "run_id", m.ID, "error", err)
		sock.fail(m.ID, codeInternal, errorMessages[codeInternal])
	case run.UserID != sock.userID:
		sock.fail(m.ID, codeNotFound, errorMessages[codeNotFound])
	case run.Status != store.RunRunning:
		sock.fail(m.ID, codeRunEnded, errorMessages[codeRunEnded])
	default:
		s.startRelay(&relay{run: run}, false)
	}
}

func (s *Service) resume(ctx context.Context, sock *socket, m inbound) {
	if m.RunID == "" || m.LastEventID < 0 {
		sock.fail(m.RunID, codeBadRequest, "resume needs a runId and a lastEventId of 0 or more.")
		return
	}
	if run, ok := s.ownRun(ctx, sock, m.RunID); ok {
		s.startRelay(&relay{run: run, lastEventID: m.LastEventID}, true)
	}
}

func (s *Service) cancel(sock *socket, runID string) {
	ctx, cancel := context.WithTimeout(s.ctx, s.cfg.Harness.CancelTimeout)
	defer cancel()
	run, ok := s.ownRun(ctx, sock, runID)
	if !ok {
		return
	}
	if err := s.cancelAtHarness(ctx, run); err != nil {
		slog.WarnContext(ctx, "could not cancel a run", "run_id", run.ID, "error", err)
		code, message, _ := refusal(err)
		sock.fail(run.ID, code, message)
	}
}

func (s *Service) send(userID string, m outbound) {
	data, err := json.Marshal(m)
	if err != nil {
		slog.Error("could not encode a message", "type", m.Type, "error", err)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for sock := range s.sockets[userID] {
		sock.push(data)
	}
}

// ServeHTTP authenticates the app, upgrades the request to a WebSocket and serves it until it closes.
func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var user auth.User
	doneWaiting := func() {}
	if header := r.Header.Get("Authorization"); header != "" {
		token, _ := strings.CutPrefix(header, "Bearer ")
		var err error
		if user, err = s.verifier.Verify(r.Context(), token); err != nil {
			slog.InfoContext(r.Context(), "refused a socket's token", "error", err)
			w.Header().Set("WWW-Authenticate", "Bearer")
			http.Error(w, "invalid token", http.StatusUnauthorized)
			return
		}
	} else {
		select {
		case s.unauthenticated <- struct{}{}:
			doneWaiting = sync.OnceFunc(func() { <-s.unauthenticated })
			defer doneWaiting()
		default:
			slog.WarnContext(r.Context(), "refused a socket: too many are waiting to authenticate")
			http.Error(w, "too many sockets are waiting to authenticate", http.StatusServiceUnavailable)
			return
		}
	}
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{OriginPatterns: s.cfg.WebSocket.AllowedOrigins})
	if err != nil {
		slog.InfoContext(r.Context(), "refused a socket", "origin", r.Header.Get("Origin"), "error", err)
		return
	}
	defer conn.CloseNow()
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	defer context.AfterFunc(s.ctx, func() { conn.Close(websocket.StatusGoingAway, "the channel service is stopping") })()

	if user.ID == "" {
		user, err = s.handshake(ctx, conn)
		doneWaiting()
		if err != nil {
			slog.InfoContext(ctx, "refused a socket's auth message", "error", err)
			conn.Close(statusUnauthorized, "unauthorized")
			return
		}
	}
	expire := time.AfterFunc(time.Until(user.Expiry), func() { conn.Close(statusUnauthorized, "token expired") })
	defer expire.Stop()
	sock := &socket{conn: conn, userID: user.ID, out: make(chan []byte, s.cfg.WebSocket.SendBuffer), token: user.Token, expiry: user.Expiry}
	s.mu.Lock()
	if s.sockets[user.ID] == nil {
		s.sockets[user.ID] = map[*socket]struct{}{}
	}
	s.sockets[user.ID][sock] = struct{}{}
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		delete(s.sockets[user.ID], sock)
		if len(s.sockets[user.ID]) > 0 {
			return
		}
		delete(s.sockets, user.ID)
		for _, r := range s.relays {
			if r.run.UserID == user.ID {
				r.cancel()
			}
		}
	}()
	go sock.write(ctx, s.cfg.WebSocket.WriteTimeout, s.cfg.WebSocket.PingInterval)
	sock.reply(outbound{Type: "ready"})

	for {
		_, data, err := conn.Read(ctx)
		if err != nil {
			return
		}
		var m inbound
		if err := json.Unmarshal(data, &m); err != nil {
			sock.fail("", codeBadRequest, "A message must be a JSON object.")
			continue
		}
		switch m.Type {
		case "auth":
			refreshed, err := s.verifier.Verify(ctx, m.Token)
			if err != nil || refreshed.ID != sock.userID {
				slog.InfoContext(ctx, "refused a socket's new token", "error", err)
				conn.Close(statusUnauthorized, "unauthorized")
				return
			}
			s.mu.Lock()
			sock.token, sock.expiry = refreshed.Token, refreshed.Expiry
			s.mu.Unlock()
			expire.Reset(time.Until(refreshed.Expiry))
		case "message":
			s.startRun(ctx, sock, m)
		case "resume":
			s.resume(ctx, sock, m)
		case "cancel":
			if m.RunID == "" {
				sock.fail("", codeBadRequest, "cancel needs a runId.")
				continue
			}
			go s.cancel(sock, m.RunID)
		default:
			sock.fail(m.RunID, codeBadRequest, fmt.Sprintf("Unknown message type %q.", m.Type))
		}
	}
}
