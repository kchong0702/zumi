package harness

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"iter"
	"net/http"
	"net/url"
	"strconv"
	"time"

	sse "github.com/tmaxmax/go-sse"

	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
)

// Event is one AG-UI event of a run, as the harness recorded it.
type Event struct {
	// ID is the harness's event id, increasing within a run.
	ID int64
	// Type is the AG-UI event type, such as RUN_STARTED.
	Type string
	// Outcome is the type of a RUN_FINISHED event's outcome, such as cancelled, or empty.
	Outcome string
	// Data is the AG-UI event's JSON.
	Data json.RawMessage
}

// Terminal reports whether the event ends its run.
func (e Event) Terminal() bool {
	return e.Type == "RUN_FINISHED" || e.Type == "RUN_ERROR"
}

// StatusError is a response the harness refused with.
type StatusError struct {
	// Code is the HTTP status code.
	Code int
	// Message is the harness's detail message, or the status text.
	Message string
}

// Error says what status the harness answered with and why.
func (e *StatusError) Error() string {
	return fmt.Sprintf("harness answered %d: %s", e.Code, e.Message)
}

// ErrMalformed reports an event that can't be read: one without an id, with a bad id or JSON,
// or larger than the stream allows.
var ErrMalformed = errors.New("malformed event")

type idleReader struct {
	body    io.Reader
	idle    *time.Timer
	timeout time.Duration
}

func (r idleReader) Read(p []byte) (int, error) {
	n, err := r.body.Read(p)
	if n > 0 {
		r.idle.Reset(r.timeout)
	}
	return n, err
}

// Stream is a run's server-sent events, dropped when it stays silent past the idle timeout.
type Stream struct {
	body          io.ReadCloser
	idle          *time.Timer
	idleTimeout   time.Duration
	cancel        context.CancelFunc
	maxEventBytes int
}

// Events yields the stream's events in order until it ends or fails.
// A malformed event ends it with an error wrapping ErrMalformed.
func (s *Stream) Events() iter.Seq2[Event, error] {
	return func(yield func(Event, error) bool) {
		previousID := ""
		for e, err := range sse.Read(idleReader{body: s.body, idle: s.idle, timeout: s.idleTimeout}, &sse.ReadConfig{MaxEventSize: s.maxEventBytes}) {
			if errors.Is(err, bufio.ErrTooLong) {
				err = fmt.Errorf("%w: an event is larger than %d bytes", ErrMalformed, s.maxEventBytes)
			}
			if err != nil {
				yield(Event{}, err)
				return
			}
			if e.LastEventID == previousID {
				yield(Event{}, fmt.Errorf("%w: an event after id %q has no id of its own", ErrMalformed, previousID))
				return
			}
			previousID = e.LastEventID
			id, err := strconv.ParseInt(e.LastEventID, 10, 64)
			if err != nil {
				yield(Event{}, fmt.Errorf("%w: event id %q: %w", ErrMalformed, e.LastEventID, err))
				return
			}
			var head struct {
				Type    string `json:"type"`
				Outcome struct {
					Type string `json:"type"`
				} `json:"outcome"`
			}
			if err := json.Unmarshal([]byte(e.Data), &head); err != nil {
				yield(Event{}, fmt.Errorf("%w: event %d: %w", ErrMalformed, id, err))
				return
			}
			if !yield(Event{ID: id, Type: head.Type, Outcome: head.Outcome.Type, Data: json.RawMessage(e.Data)}, nil) {
				return
			}
		}
	}
}

// Close stops the stream.
func (s *Stream) Close() {
	s.idle.Stop()
	s.cancel()
	s.body.Close()
}

// Client calls the harness's AG-UI routes.
type Client struct {
	client *http.Client
	cfg    config.Harness
}

// NewClient calls the harness at cfg.URL through client.
func NewClient(client *http.Client, cfg config.Harness) *Client {
	return &Client{client: client, cfg: cfg}
}

func (c *Client) do(ctx context.Context, token, method, path string, body io.Reader, header http.Header) (*http.Response, error) {
	request, err := http.NewRequestWithContext(ctx, method, c.cfg.URL+"/"+path, body)
	if err != nil {
		return nil, err
	}
	for key, values := range header {
		request.Header[key] = values
	}
	request.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.client.Do(request)
	if err != nil {
		return nil, err
	}
	if response.StatusCode >= 200 && response.StatusCode < 300 {
		return response, nil
	}
	defer response.Body.Close()
	var refusal struct {
		Detail struct {
			Message string `json:"message"`
		} `json:"detail"`
	}
	message := http.StatusText(response.StatusCode)
	if json.NewDecoder(io.LimitReader(response.Body, 64<<10)).Decode(&refusal) == nil && refusal.Detail.Message != "" {
		message = refusal.Detail.Message
	}
	return nil, &StatusError{Code: response.StatusCode, Message: message}
}

func (c *Client) stream(ctx context.Context, token, method, path string, body io.Reader, header http.Header) (*Stream, error) {
	ctx, cancel := context.WithCancel(ctx)
	idle := time.AfterFunc(c.cfg.IdleTimeout, cancel)
	response, err := c.do(ctx, token, method, path, body, header)
	if err != nil {
		idle.Stop()
		cancel()
		return nil, err
	}
	return &Stream{body: response.Body, idle: idle, idleTimeout: c.cfg.IdleTimeout, cancel: cancel, maxEventBytes: c.cfg.MaxEventBytes}, nil
}

// Start posts a run of the user's text to agent and returns its event stream.
// Posting a run id the harness already has attaches to that run.
func (c *Client) Start(ctx context.Context, token, agent, runID, userID, text string) (*Stream, error) {
	type message struct {
		ID      string `json:"id"`
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	body, err := json.Marshal(struct {
		ThreadID       string    `json:"threadId"`
		RunID          string    `json:"runId"`
		Messages       []message `json:"messages"`
		State          struct{}  `json:"state"`
		Tools          []any     `json:"tools"`
		Context        []any     `json:"context"`
		ForwardedProps struct{}  `json:"forwardedProps"`
	}{
		ThreadID: userID,
		RunID:    runID,
		Messages: []message{{ID: runID, Role: "user", Content: text}},
		Tools:    []any{},
		Context:  []any{},
	})
	if err != nil {
		return nil, err
	}
	return c.stream(ctx, token, http.MethodPost, url.PathEscape(agent), bytes.NewReader(body), nil)
}

// Events returns the stream of a run's events after lastEventID: the recorded ones, then live ones.
func (c *Client) Events(ctx context.Context, token, agent, runID string, lastEventID int64) (*Stream, error) {
	header := http.Header{"Last-Event-ID": {strconv.FormatInt(lastEventID, 10)}}
	return c.stream(ctx, token, http.MethodGet, url.PathEscape(agent)+"/runs/"+url.PathEscape(runID)+"/events", nil, header)
}

// Cancel asks the harness to stop a run.
func (c *Client) Cancel(ctx context.Context, token, agent, runID string) error {
	response, err := c.do(ctx, token, http.MethodPost, url.PathEscape(agent)+"/runs/"+url.PathEscape(runID)+"/cancel", nil, nil)
	if err != nil {
		return err
	}
	return response.Body.Close()
}
