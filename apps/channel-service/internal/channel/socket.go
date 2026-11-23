package channel

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"time"

	"github.com/coder/websocket"
)

const statusUnauthorized websocket.StatusCode = 4401

type errorCode string

const (
	codeBadRequest    errorCode = "bad_request"
	codeUnauthorized  errorCode = "unauthorized"
	codeNotFound      errorCode = "not_found"
	codeRunInProgress errorCode = "run_in_progress"
	codeRunEnded      errorCode = "run_ended"
	codeHarness       errorCode = "harness_error"
	codeInternal      errorCode = "internal"
)

var errorMessages = map[errorCode]string{
	codeBadRequest:    "The harness refused the request as invalid.",
	codeUnauthorized:  "The app's token was refused. Sign in again.",
	codeNotFound:      "There is no such run.",
	codeRunInProgress: "Another run is still going.",
	codeRunEnded:      "This run has ended. Send a new message with a new id.",
	codeHarness:       "The assistant could not take the request. Try again.",
	codeInternal:      "Something went wrong in the channel service. Try again.",
}

type inbound struct {
	Type        string `json:"type"`
	ID          string `json:"id"`
	Text        string `json:"text"`
	RunID       string `json:"runId"`
	LastEventID int64  `json:"lastEventId"`
	Token       string `json:"token"`
}

type outbound struct {
	Type    string          `json:"type"`
	RunID   string          `json:"runId,omitempty"`
	EventID int64           `json:"eventId,omitempty"`
	Event   json.RawMessage `json:"event,omitempty"`
	State   string          `json:"state,omitempty"`
	Attempt int             `json:"attempt,omitempty"`
	RetryAt *time.Time      `json:"retryAt,omitempty"`
	Code    errorCode       `json:"code,omitempty"`
	Message string          `json:"message,omitempty"`
}

type socket struct {
	conn   *websocket.Conn
	userID string
	out    chan []byte
	slow   sync.Once
	token  string
	expiry time.Time
}

func (sock *socket) push(data []byte) {
	select {
	case sock.out <- data:
	default:
		sock.slow.Do(func() { go sock.conn.Close(websocket.StatusPolicyViolation, "too many messages waiting") })
	}
}

func (sock *socket) reply(m outbound) {
	data, err := json.Marshal(m)
	if err != nil {
		slog.Error("could not encode a message", "type", m.Type, "error", err)
		return
	}
	sock.push(data)
}

func (sock *socket) fail(runID string, code errorCode, message string) {
	sock.reply(outbound{Type: "error", RunID: runID, Code: code, Message: message})
}

func (sock *socket) write(ctx context.Context, timeout, pingInterval time.Duration) {
	defer sock.conn.CloseNow()
	ping := time.NewTicker(pingInterval)
	defer ping.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case data := <-sock.out:
			writeCtx, cancel := context.WithTimeout(ctx, timeout)
			err := sock.conn.Write(writeCtx, websocket.MessageText, data)
			cancel()
			if err != nil {
				return
			}
		case <-ping.C:
			pingCtx, cancel := context.WithTimeout(ctx, timeout)
			err := sock.conn.Ping(pingCtx)
			cancel()
			if err != nil {
				return
			}
		}
	}
}
