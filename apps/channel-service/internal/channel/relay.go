package channel

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/cenkalti/backoff/v5"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/oauth2"

	"github.com/kchong0702/zumi/apps/channel-service/internal/harness"
	"github.com/kchong0702/zumi/apps/channel-service/internal/store"
)

type relay struct {
	run          store.Run
	text         string
	lastEventID  int64
	savedEventID int64
	savedAt      time.Time
	cancel       context.CancelFunc
}

func refusal(err error) (errorCode, string, bool) {
	var status *harness.StatusError
	if errors.As(err, &status) {
		switch status.Code {
		case http.StatusUnauthorized, http.StatusForbidden:
			return codeUnauthorized, status.Message, true
		case http.StatusNotFound:
			return codeNotFound, status.Message, true
		case http.StatusConflict:
			return codeRunInProgress, status.Message, true
		case http.StatusUnprocessableEntity:
			return codeBadRequest, status.Message, true
		case http.StatusRequestTimeout, http.StatusTooManyRequests:
			return codeHarness, status.Message, false
		}
		return codeHarness, status.Message, status.Code < 500
	}
	var exchange *oauth2.RetrieveError
	if errors.As(err, &exchange) && exchange.Response != nil && exchange.Response.StatusCode < 500 {
		return codeUnauthorized, errorMessages[codeUnauthorized], false
	}
	return codeHarness, errorMessages[codeHarness], false
}

func (s *Service) connect(ctx context.Context, r *relay) (*harness.Stream, error) {
	token, err := s.harnessToken(ctx, r.run.UserID)
	if err != nil {
		return nil, err
	}
	if r.text == "" {
		return s.harness.Events(ctx, token, r.run.Agent, r.run.ID, r.lastEventID)
	}
	stream, err := s.harness.Start(ctx, token, r.run.Agent, r.run.ID, r.run.UserID, r.text)
	if err != nil {
		return nil, err
	}
	r.text = ""
	if err := s.runs.StartRun(context.WithoutCancel(ctx), r.run); err != nil {
		slog.ErrorContext(ctx, "could not record a run", "run_id", r.run.ID, "error", err)
	}
	return stream, nil
}

func (s *Service) save(ctx context.Context, r *relay) {
	if err := s.runs.RelayedEvent(ctx, r.run.ID, r.lastEventID); err != nil {
		slog.ErrorContext(ctx, "could not record a relayed event", "run_id", r.run.ID, "event_id", r.lastEventID, "error", err)
	}
	r.savedEventID, r.savedAt = r.lastEventID, time.Now()
}

func (s *Service) forward(ctx context.Context, r *relay, stream *harness.Stream) (bool, error) {
	for event, err := range stream.Events() {
		if err != nil {
			return false, err
		}
		if event.ID <= r.lastEventID {
			continue
		}
		r.lastEventID = event.ID
		if event.Terminal() {
			status := store.RunFinished
			switch {
			case event.Type == "RUN_ERROR":
				status = store.RunError
			case event.Outcome == "cancelled":
				status = store.RunCancelled
			}
			if err := s.runs.FinishRun(ctx, r.run.ID, status, event.ID); err != nil {
				slog.ErrorContext(ctx, "could not record a run's end", "run_id", r.run.ID, "event_id", event.ID, "error", err)
			}
			r.savedEventID = event.ID
		} else if time.Since(r.savedAt) >= s.cfg.Relay.PersistInterval {
			s.save(ctx, r)
		}
		s.send(r.run.UserID, outbound{Type: "event", RunID: r.run.ID, EventID: event.ID, Event: event.Data})
		if event.Terminal() {
			return true, nil
		}
	}
	return false, errors.New("the stream ended before the run did")
}

func (s *Service) failRun(ctx context.Context, r *relay, code, message string) {
	if err := s.runs.FinishRun(ctx, r.run.ID, store.RunError, r.lastEventID); err != nil {
		slog.ErrorContext(ctx, "could not record a run's end", "run_id", r.run.ID, "error", err)
	}
	r.savedEventID = r.lastEventID
	event, err := json.Marshal(map[string]any{
		"type":      "RUN_ERROR",
		"code":      code,
		"message":   message,
		"timestamp": time.Now().UnixMilli(),
		"metadata":  map[string]string{"agent": r.run.Agent},
	})
	if err == nil {
		s.send(r.run.UserID, outbound{Type: "event", RunID: r.run.ID, Event: event})
	}
	cancelCtx, cancel := context.WithTimeout(ctx, s.cfg.Harness.CancelTimeout)
	defer cancel()
	if err := s.cancelAtHarness(cancelCtx, r.run); err != nil {
		slog.WarnContext(ctx, "could not cancel the run at the harness", "run_id", r.run.ID, "error", err)
	}
}

func (s *Service) relayRun(ctx context.Context, r *relay) {
	defer s.relaysDone.Done()
	ctx, span := otel.Tracer("channel-service").Start(ctx, "relay run", trace.WithAttributes(
		attribute.String("zumi.run.id", r.run.ID),
		attribute.String("zumi.agent", r.run.Agent),
	))
	defer span.End()
	defer func() {
		s.mu.Lock()
		if s.relays[r.run.ID] == r {
			delete(s.relays, r.run.ID)
		}
		s.mu.Unlock()
		r.cancel()
		if r.lastEventID > r.savedEventID {
			s.save(context.WithoutCancel(ctx), r)
		}
	}()

	wait := &backoff.ExponentialBackOff{
		InitialInterval:     s.cfg.Relay.ReconnectInitial,
		RandomizationFactor: 0.2,
		Multiplier:          2,
		MaxInterval:         s.cfg.Relay.ReconnectMax,
	}
	attempt := 0
	var droppedAt time.Time
	for {
		relayed := r.lastEventID
		stream, err := s.connect(ctx, r)
		if err == nil {
			if attempt > 0 {
				s.send(r.run.UserID, outbound{Type: "status", RunID: r.run.ID, State: "reconnected"})
			}
			var ended bool
			ended, err = s.forward(ctx, r, stream)
			stream.Close()
			if ended {
				return
			}
			if r.lastEventID > relayed {
				attempt, droppedAt = 0, time.Time{}
				wait.Reset()
			}
		}
		if ctx.Err() != nil {
			return
		}
		if errors.Is(err, harness.ErrMalformed) {
			slog.ErrorContext(ctx, "the harness sent an event that can't be read", "run_id", r.run.ID, "error", err)
			s.failRun(ctx, r, "malformed_event", "The assistant sent a reply that could not be read. Send your message again.")
			return
		}
		if code, message, refused := refusal(err); refused {
			slog.WarnContext(ctx, "the harness refused the run", "run_id", r.run.ID, "error", err)
			s.send(r.run.UserID, outbound{Type: "error", RunID: r.run.ID, Code: code, Message: message})
			return
		}
		slog.WarnContext(ctx, "lost the run's stream", "run_id", r.run.ID, "attempt", attempt, "error", err)
		if droppedAt.IsZero() {
			droppedAt = time.Now()
		}
		delay := wait.NextBackOff()
		if time.Since(droppedAt)+delay > s.cfg.Relay.GiveUpAfter {
			slog.ErrorContext(ctx, "gave up reconnecting to the run", "run_id", r.run.ID, "after", s.cfg.Relay.GiveUpAfter)
			s.failRun(ctx, r, "timeout", "The assistant stopped responding. Send your message again.")
			return
		}
		attempt++
		retryAt := time.Now().Add(delay)
		s.send(r.run.UserID, outbound{Type: "status", RunID: r.run.ID, State: "reconnecting", Attempt: attempt, RetryAt: &retryAt})
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
	}
}

func (s *Service) startRelay(r *relay, replace bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ctx.Err() != nil || len(s.sockets[r.run.UserID]) == 0 {
		return
	}
	if current, ok := s.relays[r.run.ID]; ok {
		if !replace {
			return
		}
		current.cancel()
	}
	ctx, cancel := context.WithCancel(s.ctx)
	r.cancel = cancel
	s.relays[r.run.ID] = r
	s.relaysDone.Add(1)
	go s.relayRun(ctx, r)
}
