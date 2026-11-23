package harness_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
	"github.com/kchong0702/zumi/apps/channel-service/internal/harness"
)

func TestEvents(t *testing.T) {
	tests := []struct {
		name          string
		chunks        []string
		wantIDs       []int64
		wantOutcome   string
		wantMalformed bool
		wantDropped   bool
	}{
		{
			name:        "events with ids and a cancelled outcome",
			chunks:      []string{"id: 7\ndata: {\"type\":\"RUN_STARTED\"}\n\n", ": ping\n\nid: 8\ndata: {\"type\":\"RUN_FINISHED\",\"outcome\":{\"type\":\"cancelled\"}}\n\n"},
			wantIDs:     []int64{7, 8},
			wantOutcome: "cancelled",
		},
		{
			name:          "a first event without an id",
			chunks:        []string{"data: {\"type\":\"RUN_STARTED\"}\n\n"},
			wantMalformed: true,
		},
		{
			name:          "a later event without an id",
			chunks:        []string{"id: 7\ndata: {\"type\":\"RUN_STARTED\"}\n\ndata: {\"type\":\"TEXT_MESSAGE_CONTENT\"}\n\n"},
			wantIDs:       []int64{7},
			wantMalformed: true,
		},
		{
			name:          "an event over max_event_bytes",
			chunks:        []string{"id: 7\ndata: \"" + strings.Repeat("x", 2048) + "\"\n\n"},
			wantMalformed: true,
		},
		{
			name:        "a stream silent past the idle timeout",
			chunks:      []string{"id: 7\ndata: {\"type\":\"RUN_STARTED\"}\n\n", "hold"},
			wantIDs:     []int64{7},
			wantDropped: true,
		},
		{
			name:    "pings that keep a slow stream alive",
			chunks:  []string{"id: 7\ndata: {\"type\":\"RUN_STARTED\"}\n\n", "pause", ": ping\n\n", "pause", ": ping\n\n", "pause", "id: 8\ndata: {\"type\":\"RUN_FINISHED\"}\n\n"},
			wantIDs: []int64{7, 8},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				for _, chunk := range test.chunks {
					switch chunk {
					case "hold":
						<-r.Context().Done()
					case "pause":
						time.Sleep(60 * time.Millisecond)
					default:
						_, _ = io.WriteString(w, chunk)
						w.(http.Flusher).Flush()
					}
				}
			}))
			t.Cleanup(server.Close)
			client := harness.NewClient(http.DefaultClient, config.Harness{URL: server.URL, IdleTimeout: 100 * time.Millisecond, MaxEventBytes: 1024})
			stream, err := client.Events(context.Background(), "token", "sothoth", "run", 6)
			if err != nil {
				t.Fatal(err)
			}
			defer stream.Close()

			var ids []int64
			var outcome string
			var streamErr error
			started := time.Now()
			for event, err := range stream.Events() {
				if err != nil {
					streamErr = err
					break
				}
				ids, outcome = append(ids, event.ID), event.Outcome
			}
			if !slices.Equal(ids, test.wantIDs) || outcome != test.wantOutcome {
				t.Fatalf("event ids = %v, last outcome %q, want %v and %q", ids, outcome, test.wantIDs, test.wantOutcome)
			}
			if got := errors.Is(streamErr, harness.ErrMalformed); got != test.wantMalformed {
				t.Fatalf("error = %v, want malformed %v", streamErr, test.wantMalformed)
			}
			if test.wantDropped && (streamErr == nil || time.Since(started) > 2*time.Second) {
				t.Fatalf("error = %v after %v, want the stream dropped soon after the idle timeout", streamErr, time.Since(started))
			}
			if !test.wantMalformed && !test.wantDropped && streamErr != nil {
				t.Fatalf("error = %v, want the stream to end cleanly", streamErr)
			}
		})
	}
}
