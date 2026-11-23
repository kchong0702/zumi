package channel_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/google/uuid"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/kchong0702/zumi/apps/channel-service/internal/auth"
	"github.com/kchong0702/zumi/apps/channel-service/internal/channel"
	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
	"github.com/kchong0702/zumi/apps/channel-service/internal/fakekeycloak"
	"github.com/kchong0702/zumi/apps/channel-service/internal/harness"
	"github.com/kchong0702/zumi/apps/channel-service/internal/store"
	"github.com/kchong0702/zumi/apps/channel-service/internal/store/storetest"
)

const firstEventID = 101

type harnessRequest struct {
	method, path, authorization, lastEventID string
	body                                     map[string]any
}

type fakeHarness struct {
	*httptest.Server
	events []string

	mu             sync.Mutex
	postStatus     int
	postSends      int
	postHolds      bool
	postRaw        string
	eventsFailures int
	eventsReplay   bool
	requests       []harnessRequest
	left           chan string
}

func newFakeHarness(t *testing.T, texts ...string) *fakeHarness {
	h := &fakeHarness{left: make(chan string, 8)}
	h.events = append(h.events, `{"type":"RUN_STARTED","metadata":{"agent":"sothoth"}}`)
	for _, text := range texts {
		h.events = append(h.events, fmt.Sprintf(`{"type":"TEXT_MESSAGE_CONTENT","delta":%q,"metadata":{"agent":"sothoth"}}`, text))
	}
	h.events = append(h.events, `{"type":"RUN_FINISHED","metadata":{"agent":"sothoth"}}`)
	h.postSends = len(h.events)
	mux := http.NewServeMux()
	mux.HandleFunc("POST /sothoth", func(w http.ResponseWriter, r *http.Request) {
		h.mu.Lock()
		h.record(r)
		status, sends, holds, raw := h.postStatus, h.postSends, h.postHolds, h.postRaw
		h.mu.Unlock()
		if status != 0 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_, _ = io.WriteString(w, `{"detail":{"message":"A run is already running on this thread.","runId":"other"}}`)
			return
		}
		h.stream(w, r, 0, sends, holds)
		_, _ = io.WriteString(w, raw)
	})
	mux.HandleFunc("GET /sothoth/runs/{id}/events", func(w http.ResponseWriter, r *http.Request) {
		h.mu.Lock()
		h.record(r)
		failing := h.eventsFailures != 0
		if h.eventsFailures > 0 {
			h.eventsFailures--
		}
		replay, sends := h.eventsReplay, h.postSends
		h.mu.Unlock()
		if failing {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		if replay {
			h.stream(w, r, 0, sends, false)
			return
		}
		after, _ := strconv.ParseInt(r.Header.Get("Last-Event-ID"), 10, 64)
		h.stream(w, r, int(after-firstEventID+1), len(h.events), false)
	})
	mux.HandleFunc("POST /sothoth/runs/{id}/cancel", func(w http.ResponseWriter, r *http.Request) {
		h.mu.Lock()
		h.record(r)
		h.mu.Unlock()
		w.WriteHeader(http.StatusAccepted)
	})
	h.Server = httptest.NewServer(mux)
	t.Cleanup(h.Close)
	return h
}

func (h *fakeHarness) record(r *http.Request) {
	request := harnessRequest{method: r.Method, path: r.URL.Path, authorization: r.Header.Get("Authorization"), lastEventID: r.Header.Get("Last-Event-ID")}
	if r.Method == http.MethodPost && r.URL.Path == "/sothoth" {
		_ = json.NewDecoder(r.Body).Decode(&request.body)
	}
	h.requests = append(h.requests, request)
}

func (h *fakeHarness) stream(w http.ResponseWriter, r *http.Request, from, to int, holds bool) {
	w.Header().Set("Content-Type", "text/event-stream")
	for i := max(from, 0); i < to; i++ {
		fmt.Fprintf(w, ": ping\n\nid: %d\ndata: %s\n\n", firstEventID+i, h.events[i])
		w.(http.Flusher).Flush()
	}
	if holds {
		<-r.Context().Done()
		h.left <- r.URL.Path
	}
}

func (h *fakeHarness) recorded(method, path string) []harnessRequest {
	h.mu.Lock()
	defer h.mu.Unlock()
	var found []harnessRequest
	for _, request := range h.requests {
		if request.method == method && request.path == path {
			found = append(found, request)
		}
	}
	return found
}

type memoryRuns struct {
	mu   sync.Mutex
	runs map[string]store.Run
}

func (m *memoryRuns) Run(_ context.Context, id string) (store.Run, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, ok := m.runs[id]
	if !ok {
		return store.Run{}, store.ErrNotFound
	}
	return run, nil
}

func (m *memoryRuns) StartRun(_ context.Context, run store.Run) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.runs[run.ID]; !ok {
		run.Status = store.RunRunning
		m.runs[run.ID] = run
	}
	return nil
}

func (m *memoryRuns) RelayedEvent(_ context.Context, id string, eventID int64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if run, ok := m.runs[id]; ok {
		run.LastEventID = max(run.LastEventID, eventID)
		m.runs[id] = run
	}
	return nil
}

func (m *memoryRuns) FinishRun(_ context.Context, id string, status store.RunStatus, eventID int64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if run, ok := m.runs[id]; ok {
		run.Status, run.LastEventID = status, max(run.LastEventID, eventID)
		m.runs[id] = run
	}
	return nil
}

type testEnv struct {
	realm   *fakekeycloak.Server
	harness *fakeHarness
	runs    channel.Runs
	url     string
}

func newEnv(t *testing.T, h *fakeHarness, runs channel.Runs, adjust ...func(*config.Config)) *testEnv {
	realm := fakekeycloak.New(t)
	cfg := config.Config{
		Auth:      realm.Config(),
		Harness:   config.Harness{URL: h.URL, Agent: "sothoth", IdleTimeout: 5 * time.Second, MaxEventBytes: 1 << 20, CancelTimeout: 5 * time.Second},
		Relay:     config.Relay{ReconnectInitial: 10 * time.Millisecond, ReconnectMax: 40 * time.Millisecond, GiveUpAfter: 5 * time.Second, PersistInterval: time.Hour},
		WebSocket: config.WebSocket{AllowedOrigins: []string{"tauri://localhost"}, AuthTimeout: 200 * time.Millisecond, SendBuffer: 64, WriteTimeout: 5 * time.Second, PingInterval: time.Hour, MaxUnauthenticated: 64},
	}
	for _, f := range adjust {
		f(&cfg)
	}
	if runs == nil {
		runs = &memoryRuns{runs: map[string]store.Run{}}
	}
	service := channel.New(cfg,
		auth.NewVerifier(context.Background(), http.DefaultClient, cfg.Auth),
		auth.NewExchanger(http.DefaultClient, cfg.Auth),
		harness.NewClient(http.DefaultClient, cfg.Harness),
		runs,
	)
	server := httptest.NewServer(otelhttp.NewHandler(service, "test"))
	t.Cleanup(server.Close)
	t.Cleanup(service.Close)
	return &testEnv{realm: realm, harness: h, runs: runs, url: "ws" + strings.TrimPrefix(server.URL, "http") + "/ws"}
}

type message struct {
	Type    string          `json:"type"`
	RunID   string          `json:"runId"`
	EventID int64           `json:"eventId"`
	Event   json.RawMessage `json:"event"`
	State   string          `json:"state"`
	Attempt int             `json:"attempt"`
	RetryAt *time.Time      `json:"retryAt"`
	Code    string          `json:"code"`
	Message string          `json:"message"`
}

type client struct {
	t    *testing.T
	conn *websocket.Conn
}

func (e *testEnv) dial(t *testing.T, header http.Header) (*client, *http.Response, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, response, err := websocket.Dial(ctx, e.url, &websocket.DialOptions{HTTPHeader: header})
	if err != nil {
		return nil, response, err
	}
	t.Cleanup(func() { conn.CloseNow() })
	return &client{t: t, conn: conn}, response, nil
}

func (e *testEnv) connect(t *testing.T, userID string) *client {
	c, _, err := e.dial(t, http.Header{"Authorization": {"Bearer " + e.realm.Token(t, userID, nil)}})
	if err != nil {
		t.Fatal(err)
	}
	c.expect("ready")
	return c
}

func (c *client) send(m map[string]any) {
	c.t.Helper()
	data, err := json.Marshal(m)
	if err != nil {
		c.t.Fatal(err)
	}
	if err := c.conn.Write(context.Background(), websocket.MessageText, data); err != nil {
		c.t.Fatal(err)
	}
}

func (c *client) next() (message, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, data, err := c.conn.Read(ctx)
	if err != nil {
		return message{}, err
	}
	var m message
	return m, json.Unmarshal(data, &m)
}

func (c *client) expect(typ string) message {
	c.t.Helper()
	m, err := c.next()
	if err != nil {
		c.t.Fatalf("waiting for %s: %v", typ, err)
	}
	if m.Type != typ {
		c.t.Fatalf("got %+v, want a %s message", m, typ)
	}
	return m
}

func (c *client) expectEvents(runID string, ids ...int64) {
	c.t.Helper()
	for _, id := range ids {
		m := c.expect("event")
		if m.RunID != runID || m.EventID != id {
			c.t.Fatalf("got event %d of run %s, want event %d of run %s", m.EventID, m.RunID, id, runID)
		}
	}
}

func (c *client) expectClosed(code websocket.StatusCode) {
	c.t.Helper()
	for {
		_, err := c.next()
		if err == nil {
			continue
		}
		if got := websocket.CloseStatus(err); got != code {
			c.t.Fatalf("socket ended with %v (%d), want close code %d", err, got, code)
		}
		return
	}
}

func eventIDs(from, to int64) []int64 {
	var ids []int64
	for id := from; id <= to; id++ {
		ids = append(ids, id)
	}
	return ids
}

func waitFor(t *testing.T, what string, done func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !done() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func runOf(t *testing.T, runs channel.Runs, id string) store.Run {
	t.Helper()
	run, err := runs.Run(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	return run
}

func TestSocketRefusals(t *testing.T) {
	env := newEnv(t, newFakeHarness(t), nil)
	userID := uuid.NewString()
	valid := env.realm.Token(t, userID, nil)
	tests := []struct {
		name       string
		header     http.Header
		first      map[string]any
		wantStatus int
		wantClose  websocket.StatusCode
	}{
		{name: "an invalid Authorization header", header: http.Header{"Authorization": {"Bearer nope"}}, wantStatus: http.StatusUnauthorized},
		{name: "an ID token in the Authorization header", header: http.Header{"Authorization": {"Bearer " + env.realm.Token(t, userID, map[string]any{"typ": "ID"})}}, wantStatus: http.StatusUnauthorized},
		{name: "an origin outside the allow-list", header: http.Header{"Authorization": {"Bearer " + valid}, "Origin": {"https://evil.example"}}, wantStatus: http.StatusForbidden},
		{name: "no auth message", wantClose: 4401},
		{name: "a first message that isn't auth", first: map[string]any{"type": "message", "id": uuid.NewString(), "text": "hi"}, wantClose: 4401},
		{name: "an auth message with an expired token", first: map[string]any{"type": "auth", "token": env.realm.Token(t, userID, map[string]any{"exp": time.Now().Add(-time.Minute).Unix()})}, wantClose: 4401},
		{name: "an auth message with another audience", first: map[string]any{"type": "auth", "token": env.realm.Token(t, userID, map[string]any{"aud": "zumi-harness"})}, wantClose: 4401},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			c, response, err := env.dial(t, test.header)
			if test.wantStatus != 0 {
				if err == nil || response == nil || response.StatusCode != test.wantStatus {
					t.Fatalf("dial = %v, %v, want HTTP %d", response, err, test.wantStatus)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if test.first != nil {
				c.send(test.first)
			}
			c.expectClosed(test.wantClose)
		})
	}
}

func TestSocketAcceptance(t *testing.T) {
	env := newEnv(t, newFakeHarness(t), nil)
	userID := uuid.NewString()
	tests := []struct {
		name   string
		header http.Header
		first  map[string]any
	}{
		{name: "a token in the Authorization header and no Origin", header: http.Header{"Authorization": {"Bearer " + env.realm.Token(t, userID, nil)}}},
		{name: "a token in the first message", first: map[string]any{"type": "auth", "token": env.realm.Token(t, userID, nil)}},
		{name: "an allowed Tauri origin", header: http.Header{"Authorization": {"Bearer " + env.realm.Token(t, userID, nil)}, "Origin": {"tauri://localhost"}}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			c, _, err := env.dial(t, test.header)
			if err != nil {
				t.Fatal(err)
			}
			if test.first != nil {
				c.send(test.first)
			}
			c.expect("ready")
		})
	}
}

func TestTokenRefresh(t *testing.T) {
	env := newEnv(t, newFakeHarness(t), nil)
	userID := uuid.NewString()
	expiry := time.Now().Add(2 * time.Second)

	c, _, err := env.dial(t, http.Header{"Authorization": {"Bearer " + env.realm.Token(t, userID, map[string]any{"exp": expiry.Unix()})}})
	if err != nil {
		t.Fatal(err)
	}
	c.expect("ready")
	c.send(map[string]any{"type": "auth", "token": env.realm.Token(t, userID, nil)})
	time.Sleep(time.Until(expiry) + 500*time.Millisecond)
	c.send(map[string]any{"type": "cancel"})
	if m := c.expect("error"); m.Code != "bad_request" {
		t.Fatalf("after the first token expired got %+v, want the socket still answering", m)
	}

	c.send(map[string]any{"type": "auth", "token": env.realm.Token(t, uuid.NewString(), nil)})
	c.expectClosed(4401)
}

func TestTokenExpiry(t *testing.T) {
	env := newEnv(t, newFakeHarness(t), nil)
	expiry := time.Now().Add(2 * time.Second)
	c, _, err := env.dial(t, http.Header{"Authorization": {"Bearer " + env.realm.Token(t, uuid.NewString(), map[string]any{"exp": expiry.Unix()})}})
	if err != nil {
		t.Fatal(err)
	}
	c.expect("ready")
	c.expectClosed(4401)
	if early := time.Until(expiry.Truncate(time.Second)); early > 100*time.Millisecond {
		t.Fatalf("socket closed %v before its token expired", early)
	}
}

func TestUnauthenticatedSocketLimit(t *testing.T) {
	env := newEnv(t, newFakeHarness(t), nil, func(cfg *config.Config) {
		cfg.WebSocket.MaxUnauthenticated = 1
		cfg.WebSocket.AuthTimeout = 5 * time.Second
	})
	if _, _, err := env.dial(t, nil); err != nil {
		t.Fatal(err)
	}
	if _, response, err := env.dial(t, nil); err == nil || response == nil || response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("second socket waiting to authenticate = %v, %v, want HTTP 503", response, err)
	}
	env.connect(t, uuid.NewString())
}

func TestBadMessages(t *testing.T) {
	env := newEnv(t, newFakeHarness(t), nil)
	c := env.connect(t, uuid.NewString())
	tests := []struct {
		name string
		raw  string
	}{
		{"not JSON", `hello`},
		{"an unknown type", `{"type":"dance"}`},
		{"a message whose id isn't a UUID", `{"type":"message","id":"1","text":"hi"}`},
		{"a message with no text", `{"type":"message","id":"` + uuid.NewString() + `","text":"  "}`},
		{"a resume without a runId", `{"type":"resume","lastEventId":3}`},
		{"a resume with a negative lastEventId", `{"type":"resume","runId":"r","lastEventId":-1}`},
		{"a cancel without a runId", `{"type":"cancel"}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := c.conn.Write(context.Background(), websocket.MessageText, []byte(test.raw)); err != nil {
				t.Fatal(err)
			}
			if m := c.expect("error"); m.Code != "bad_request" || m.Message == "" {
				t.Fatalf("got %+v, want a bad_request error", m)
			}
		})
	}
}

func TestRelay(t *testing.T) {
	stores := map[string]func(t *testing.T) channel.Runs{
		"in memory": func(*testing.T) channel.Runs { return &memoryRuns{runs: map[string]store.Run{}} },
		"in Postgres": func(t *testing.T) channel.Runs {
			return storetest.New(t)
		},
	}
	for name, newRuns := range stores {
		t.Run(name, func(t *testing.T) {
			h := newFakeHarness(t, "Hello", " there")
			env := newEnv(t, h, newRuns(t))
			userID, runID := uuid.NewString(), uuid.NewString()
			c := env.connect(t, userID)

			c.send(map[string]any{"type": "message", "id": runID, "text": "Hi Zumi"})
			c.expectEvents(runID, eventIDs(101, 104)...)
			c.conn.CloseNow()

			posts := h.recorded(http.MethodPost, "/sothoth")
			if len(posts) != 1 {
				t.Fatalf("harness got %d runs, want 1", len(posts))
			}
			wantBody := map[string]any{
				"threadId":       userID,
				"runId":          runID,
				"messages":       []any{map[string]any{"id": runID, "role": "user", "content": "Hi Zumi"}},
				"state":          map[string]any{},
				"tools":          []any{},
				"context":        []any{},
				"forwardedProps": map[string]any{},
			}
			if got, want := fmt.Sprint(posts[0].body), fmt.Sprint(wantBody); got != want {
				t.Fatalf("harness got body %s, want %s", got, want)
			}
			if posts[0].authorization != "Bearer "+fakekeycloak.HarnessToken(userID) {
				t.Fatalf("harness got Authorization %q, want the exchanged token", posts[0].authorization)
			}
			if run := runOf(t, env.runs, runID); run.UserID != userID || run.Agent != "sothoth" || run.Status != store.RunFinished || run.LastEventID != 104 {
				t.Fatalf("recorded run = %+v, want the user's sothoth run finished at event 104", run)
			}
		})
	}
}

func TestResumeAfterTheAppLeft(t *testing.T) {
	h := newFakeHarness(t, "one", "two", "three")
	h.postSends, h.postHolds = 2, true
	env := newEnv(t, h, nil)
	userID, runID := uuid.NewString(), uuid.NewString()

	first := env.connect(t, userID)
	first.send(map[string]any{"type": "message", "id": runID, "text": "Count to three"})
	first.expectEvents(runID, 101, 102)
	first.conn.CloseNow()
	select {
	case <-h.left:
	case <-time.After(5 * time.Second):
		t.Fatal("the relay kept the harness stream open after the app left")
	}
	waitFor(t, "event 102 to be recorded when the relay stopped", func() bool { return runOf(t, env.runs, runID).LastEventID == 102 })
	if run := runOf(t, env.runs, runID); run.Status != store.RunRunning {
		t.Fatalf("run after the app left = %+v, want still running", run)
	}

	second := env.connect(t, userID)
	second.send(map[string]any{"type": "resume", "runId": runID, "lastEventId": 102})
	second.expectEvents(runID, 103, 104, 105)
	gets := h.recorded(http.MethodGet, "/sothoth/runs/"+runID+"/events")
	if len(gets) != 1 || gets[0].lastEventID != "102" || gets[0].authorization != "Bearer "+fakekeycloak.HarnessToken(userID) {
		t.Fatalf("harness event requests = %+v, want one after event 102 with the exchanged token", gets)
	}
	waitFor(t, "the run's end to be recorded", func() bool { return runOf(t, env.runs, runID).Status == store.RunFinished })
}

func TestCancel(t *testing.T) {
	h := newFakeHarness(t)
	env := newEnv(t, h, nil)
	userID, runID, othersRunID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	for id, owner := range map[string]string{runID: userID, othersRunID: uuid.NewString()} {
		if err := env.runs.StartRun(context.Background(), store.Run{ID: id, UserID: owner, Agent: "sothoth"}); err != nil {
			t.Fatal(err)
		}
	}
	c := env.connect(t, userID)

	tests := []struct {
		name     string
		runID    string
		wantCode string
	}{
		{"an unknown run", uuid.NewString(), "not_found"},
		{"another user's run", othersRunID, "not_found"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			c.send(map[string]any{"type": "cancel", "runId": test.runID})
			if m := c.expect("error"); m.Code != test.wantCode || m.RunID != test.runID {
				t.Fatalf("got %+v, want a %s error", m, test.wantCode)
			}
		})
	}

	c.send(map[string]any{"type": "cancel", "runId": runID})
	waitFor(t, "the harness to get the cancel", func() bool {
		return len(h.recorded(http.MethodPost, "/sothoth/runs/"+runID+"/cancel")) == 1
	})
	if got := h.recorded(http.MethodPost, "/sothoth/runs/"+runID+"/cancel")[0].authorization; got != "Bearer "+fakekeycloak.HarnessToken(userID) {
		t.Fatalf("cancel Authorization = %q, want the exchanged token", got)
	}
	if n := len(h.recorded(http.MethodPost, "/sothoth/runs/"+othersRunID+"/cancel")); n != 0 {
		t.Fatalf("harness got %d cancels of another user's run", n)
	}
}

func TestReconnect(t *testing.T) {
	h := newFakeHarness(t, "one", "two")
	h.postSends, h.eventsFailures = 2, 2
	env := newEnv(t, h, nil)
	userID, runID := uuid.NewString(), uuid.NewString()
	c := env.connect(t, userID)

	c.send(map[string]any{"type": "message", "id": runID, "text": "Count to two"})
	c.expectEvents(runID, 101, 102)
	for attempt := 1; attempt <= 3; attempt++ {
		m := c.expect("status")
		if m.State != "reconnecting" || m.Attempt != attempt || m.RetryAt == nil || m.RunID != runID {
			t.Fatalf("got %+v, want reconnecting attempt %d with a retryAt", m, attempt)
		}
	}
	if m := c.expect("status"); m.State != "reconnected" {
		t.Fatalf("got %+v, want reconnected", m)
	}
	c.expectEvents(runID, 103, 104)
	for _, get := range h.recorded(http.MethodGet, "/sothoth/runs/"+runID+"/events") {
		if get.lastEventID != "102" {
			t.Fatalf("harness got Last-Event-ID %s, want 102", get.lastEventID)
		}
	}
}

func TestGiveUp(t *testing.T) {
	h := newFakeHarness(t, "one")
	h.postSends, h.eventsFailures = 1, -1
	env := newEnv(t, h, nil, func(cfg *config.Config) { cfg.Relay.GiveUpAfter = 150 * time.Millisecond })
	userID, runID := uuid.NewString(), uuid.NewString()
	c := env.connect(t, userID)

	c.send(map[string]any{"type": "message", "id": runID, "text": "Hi"})
	c.expectEvents(runID, 101)
	var m message
	for m = c.expect("status"); m.Type == "status"; m, _ = c.next() {
		if m.State != "reconnecting" {
			t.Fatalf("got %+v, want reconnecting", m)
		}
	}
	var event struct {
		Type, Code, Message string
	}
	if err := json.Unmarshal(m.Event, &event); err != nil {
		t.Fatal(err)
	}
	if m.Type != "event" || m.EventID != 0 || event.Type != "RUN_ERROR" || event.Code != "timeout" || event.Message != "The assistant stopped responding. Send your message again." {
		t.Fatalf("got %+v with %+v, want a timeout RUN_ERROR without an eventId", m, event)
	}
	waitFor(t, "the harness to get a cancel", func() bool {
		return len(h.recorded(http.MethodPost, "/sothoth/runs/"+runID+"/cancel")) == 1
	})
	if run := runOf(t, env.runs, runID); run.Status != store.RunError || run.LastEventID != 101 {
		t.Fatalf("recorded run = %+v, want error after event 101", run)
	}
}

func TestHarnessRefusesTheRun(t *testing.T) {
	h := newFakeHarness(t)
	h.postStatus = http.StatusConflict
	env := newEnv(t, h, nil)
	runID := uuid.NewString()
	c := env.connect(t, uuid.NewString())

	c.send(map[string]any{"type": "message", "id": runID, "text": "Hi"})
	if m := c.expect("error"); m.Code != "run_in_progress" || m.RunID != runID || !strings.Contains(m.Message, "already running") {
		t.Fatalf("got %+v, want run_in_progress with the harness's message", m)
	}
	if _, err := env.runs.Run(context.Background(), runID); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("a refused run was recorded: %v", err)
	}
}

func TestFanOut(t *testing.T) {
	env := newEnv(t, newFakeHarness(t, "Hello"), nil)
	userID, runID := uuid.NewString(), uuid.NewString()
	laptop, phone := env.connect(t, userID), env.connect(t, userID)
	stranger := env.connect(t, uuid.NewString())

	laptop.send(map[string]any{"type": "message", "id": runID, "text": "Hi"})
	laptop.expectEvents(runID, eventIDs(101, 103)...)
	phone.expectEvents(runID, eventIDs(101, 103)...)

	stranger.send(map[string]any{"type": "cancel"})
	if m := stranger.expect("error"); m.Code != "bad_request" {
		t.Fatalf("another user's socket got %+v, want only its own error", m)
	}
}

func TestCancelledOutcome(t *testing.T) {
	h := newFakeHarness(t, "Hel")
	h.events[len(h.events)-1] = `{"type":"RUN_FINISHED","outcome":{"type":"cancelled"},"metadata":{"agent":"sothoth"}}`
	env := newEnv(t, h, nil)
	userID, runID := uuid.NewString(), uuid.NewString()
	c := env.connect(t, userID)

	c.send(map[string]any{"type": "message", "id": runID, "text": "Hi"})
	c.expectEvents(runID, eventIDs(101, 103)...)
	if run := runOf(t, env.runs, runID); run.Status != store.RunCancelled || run.LastEventID != 103 {
		t.Fatalf("recorded run = %+v, want cancelled at event 103", run)
	}
}

type failingStart struct {
	*memoryRuns
}

func (failingStart) StartRun(context.Context, store.Run) error {
	return errors.New("the database is down")
}

func TestRecordingTheRunFails(t *testing.T) {
	env := newEnv(t, newFakeHarness(t, "Hello"), failingStart{&memoryRuns{runs: map[string]store.Run{}}})
	runID := uuid.NewString()
	c := env.connect(t, uuid.NewString())

	c.send(map[string]any{"type": "message", "id": runID, "text": "Hi"})
	c.expectEvents(runID, eventIDs(101, 103)...)
	if _, err := env.runs.Run(context.Background(), runID); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("Run() = %v, want the run unrecorded", err)
	}
}

func TestMessageIDReuse(t *testing.T) {
	userID := uuid.NewString()
	tests := []struct {
		name     string
		owner    string
		status   store.RunStatus
		wantCode string
	}{
		{"another user's run", uuid.NewString(), store.RunRunning, "not_found"},
		{"the user's finished run", userID, store.RunFinished, "run_ended"},
		{"the user's cancelled run", userID, store.RunCancelled, "run_ended"},
		{"the user's running run", userID, store.RunRunning, ""},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			h := newFakeHarness(t, "Hello")
			runs := &memoryRuns{runs: map[string]store.Run{}}
			runID := uuid.NewString()
			runs.runs[runID] = store.Run{ID: runID, UserID: test.owner, Agent: "sothoth", Status: test.status}
			env := newEnv(t, h, runs)
			c := env.connect(t, userID)

			c.send(map[string]any{"type": "message", "id": runID, "text": "Hi again"})
			if test.wantCode != "" {
				if m := c.expect("error"); m.Code != test.wantCode || m.RunID != runID {
					t.Fatalf("got %+v, want a %s error", m, test.wantCode)
				}
			} else {
				c.expectEvents(runID, eventIDs(101, 103)...)
				if gets := h.recorded(http.MethodGet, "/sothoth/runs/"+runID+"/events"); len(gets) != 1 || gets[0].lastEventID != "0" {
					t.Fatalf("harness event requests = %+v, want one from the start", gets)
				}
			}
			if posts := h.recorded(http.MethodPost, "/sothoth"); len(posts) != 0 {
				t.Fatalf("harness got %d runs, want none", len(posts))
			}
		})
	}
}

func TestMalformedEvent(t *testing.T) {
	tests := []struct {
		name string
		raw  string
	}{
		{"an event without an id", "data: {\"type\":\"TEXT_MESSAGE_CONTENT\"}\n\n"},
		{"an event with an id that isn't a number", "id: abc\ndata: {\"type\":\"TEXT_MESSAGE_CONTENT\"}\n\n"},
		{"an event that isn't JSON", "id: 102\ndata: hello\n\n"},
		{"an event over max_event_bytes", "id: 102\ndata: \"" + strings.Repeat("x", 4096) + "\"\n\n"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			h := newFakeHarness(t, "one")
			h.postSends, h.postRaw = 1, test.raw
			env := newEnv(t, h, nil, func(cfg *config.Config) { cfg.Harness.MaxEventBytes = 1024 })
			userID, runID := uuid.NewString(), uuid.NewString()
			c := env.connect(t, userID)

			c.send(map[string]any{"type": "message", "id": runID, "text": "Hi"})
			c.expectEvents(runID, 101)
			m := c.expect("event")
			var event struct {
				Type, Code, Message string
			}
			if err := json.Unmarshal(m.Event, &event); err != nil {
				t.Fatal(err)
			}
			if m.EventID != 0 || event.Type != "RUN_ERROR" || event.Code != "malformed_event" || event.Message != "The assistant sent a reply that could not be read. Send your message again." {
				t.Fatalf("got %+v with %+v, want a malformed_event RUN_ERROR", m, event)
			}
			waitFor(t, "the harness to get a cancel", func() bool {
				return len(h.recorded(http.MethodPost, "/sothoth/runs/"+runID+"/cancel")) == 1
			})
			if gets := h.recorded(http.MethodGet, "/sothoth/runs/"+runID+"/events"); len(gets) != 0 {
				t.Fatalf("relay reconnected %d times after a malformed event, want none", len(gets))
			}
			if run := runOf(t, env.runs, runID); run.Status != store.RunError || run.LastEventID != 101 {
				t.Fatalf("recorded run = %+v, want error after event 101", run)
			}
		})
	}
}

func TestReconnectsWithoutProgressGiveUp(t *testing.T) {
	h := newFakeHarness(t, "one", "two")
	h.postSends, h.eventsReplay = 2, true
	env := newEnv(t, h, nil, func(cfg *config.Config) { cfg.Relay.GiveUpAfter = 150 * time.Millisecond })
	userID, runID := uuid.NewString(), uuid.NewString()
	c := env.connect(t, userID)

	c.send(map[string]any{"type": "message", "id": runID, "text": "Count to two"})
	c.expectEvents(runID, 101, 102)
	attempts := 0
	m := c.expect("status")
	for ; m.Type == "status"; m, _ = c.next() {
		if m.State == "reconnecting" {
			if m.Attempt != attempts+1 {
				t.Fatalf("got reconnecting attempt %d, want %d: a stream without new events reset the backoff", m.Attempt, attempts+1)
			}
			attempts = m.Attempt
		}
	}
	var event struct{ Type, Code string }
	if err := json.Unmarshal(m.Event, &event); err != nil {
		t.Fatal(err)
	}
	if m.Type != "event" || m.EventID != 0 || event.Type != "RUN_ERROR" || event.Code != "timeout" {
		t.Fatalf("got %+v with %+v, want a timeout RUN_ERROR and no replayed event", m, event)
	}
	if gets := h.recorded(http.MethodGet, "/sothoth/runs/"+runID+"/events"); len(gets) < 2 || attempts < 2 {
		t.Fatalf("relay reconnected %d times over %d attempts, want at least 2", len(gets), attempts)
	}
	if run := runOf(t, env.runs, runID); run.Status != store.RunError || run.LastEventID != 102 {
		t.Fatalf("recorded run = %+v, want error after event 102", run)
	}
}

func TestSlowConsumerIsClosed(t *testing.T) {
	texts := make([]string, 64)
	for i := range texts {
		texts[i] = strings.Repeat("x", 256<<10)
	}
	env := newEnv(t, newFakeHarness(t, texts...), nil, func(cfg *config.Config) { cfg.WebSocket.SendBuffer = 2 })
	userID, runID := uuid.NewString(), uuid.NewString()
	c := env.connect(t, userID)
	c.conn.SetReadLimit(-1)

	c.send(map[string]any{"type": "message", "id": runID, "text": "Talk a lot"})
	waitFor(t, "the run's end to be recorded", func() bool {
		run, err := env.runs.Run(context.Background(), runID)
		return err == nil && run.Status == store.RunFinished
	})
	c.expectClosed(websocket.StatusPolicyViolation)
}
