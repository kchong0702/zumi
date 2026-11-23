package store_test

import (
	"context"
	"errors"
	"testing"

	"github.com/google/uuid"

	"github.com/kchong0702/zumi/apps/channel-service/internal/store"
	"github.com/kchong0702/zumi/apps/channel-service/internal/store/storetest"
)

func TestRuns(t *testing.T) {
	s := storetest.New(t)
	ctx := context.Background()
	run := store.Run{ID: uuid.NewString(), UserID: uuid.NewString(), Agent: "sothoth", Status: store.RunRunning}

	if _, err := s.Run(ctx, run.ID); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("Run() of an unrecorded run = %v, want ErrNotFound", err)
	}
	for range 2 {
		if err := s.StartRun(ctx, run); err != nil {
			t.Fatal(err)
		}
	}
	for _, eventID := range []int64{3, 7, 5} {
		if err := s.RelayedEvent(ctx, run.ID, eventID); err != nil {
			t.Fatal(err)
		}
	}
	got, err := s.Run(ctx, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	if want := (store.Run{ID: run.ID, UserID: run.UserID, Agent: "sothoth", Status: store.RunRunning, LastEventID: 7}); got != want {
		t.Fatalf("Run() = %+v, want %+v", got, want)
	}
	if err := s.FinishRun(ctx, run.ID, store.RunFinished, 9); err != nil {
		t.Fatal(err)
	}
	if got, err = s.Run(ctx, run.ID); err != nil || got.Status != store.RunFinished || got.LastEventID != 9 {
		t.Fatalf("Run() after FinishRun = %+v, %v, want finished at event 9", got, err)
	}
}

func TestMigrations(t *testing.T) {
	s := storetest.New(t)
	ctx := context.Background()
	if err := s.Reset(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Run(ctx, "any"); err == nil || errors.Is(err, store.ErrNotFound) {
		t.Fatalf("Run() after Reset = %v, want a missing table", err)
	}
	if err := s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	if err := s.Migrate(ctx); err != nil {
		t.Fatalf("a second Migrate() = %v, want nothing to do", err)
	}
	if _, err := s.Run(ctx, "any"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("Run() after Migrate = %v, want ErrNotFound", err)
	}
}
