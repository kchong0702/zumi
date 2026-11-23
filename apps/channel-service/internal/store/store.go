package store

import (
	"context"
	"embed"
	"errors"
	"fmt"
	"io/fs"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"
)

//go:embed migrations/*.sql
var migrations embed.FS

// RunStatus is where a run is in its life.
type RunStatus string

// The statuses a run moves through: running, then finished, cancelled or error. A run stays running
// when no relay sees its end, until the lease sweeper reconciles it.
const (
	RunRunning   RunStatus = "running"
	RunFinished  RunStatus = "finished"
	RunCancelled RunStatus = "cancelled"
	RunError     RunStatus = "error"
)

// Run is one agent run the channel service relays.
type Run struct {
	// ID is the run's id, the same at the harness.
	ID string
	// UserID is the Keycloak id of the user who started it.
	UserID string
	// Agent is the harness agent running it.
	Agent string
	// Status is where the run is in its life.
	Status RunStatus
	// LastEventID is the id of the last event relayed.
	LastEventID int64
}

// ErrNotFound reports a run that isn't recorded.
var ErrNotFound = errors.New("run not found")

// Store keeps the channel service's tables in one Postgres schema.
type Store struct {
	pool   *pgxpool.Pool
	schema string
}

// Open connects to Postgres with schema first on the search path.
func Open(ctx context.Context, url, schema string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, fmt.Errorf("parse the database url: %w", err)
	}
	cfg.ConnConfig.RuntimeParams["search_path"] = schema
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect to the database: %w", err)
	}
	return &Store{pool: pool, schema: schema}, nil
}

// Close closes every connection.
func (s *Store) Close() {
	s.pool.Close()
}

func (s *Store) migrate(ctx context.Context, apply func(*goose.Provider) error) error {
	if _, err := s.pool.Exec(ctx, "CREATE SCHEMA IF NOT EXISTS "+pgx.Identifier{s.schema}.Sanitize()); err != nil {
		return fmt.Errorf("create schema %s: %w", s.schema, err)
	}
	files, err := fs.Sub(migrations, "migrations")
	if err != nil {
		return err
	}
	db := stdlib.OpenDBFromPool(s.pool)
	defer db.Close()
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files)
	if err != nil {
		return err
	}
	return apply(provider)
}

// Migrate creates the schema when missing and applies every migration it hasn't had yet.
func (s *Store) Migrate(ctx context.Context) error {
	return s.migrate(ctx, func(provider *goose.Provider) error {
		_, err := provider.Up(ctx)
		return err
	})
}

// Reset rolls back every migration, dropping the service's tables.
func (s *Store) Reset(ctx context.Context) error {
	return s.migrate(ctx, func(provider *goose.Provider) error {
		_, err := provider.DownTo(ctx, 0)
		return err
	})
}

// Run returns the recorded run with id, or ErrNotFound.
func (s *Store) Run(ctx context.Context, id string) (Run, error) {
	var run Run
	err := s.pool.QueryRow(ctx,
		`SELECT id, user_id, agent, status, last_event_id FROM agent_runs WHERE id = $1`, id,
	).Scan(&run.ID, &run.UserID, &run.Agent, &run.Status, &run.LastEventID)
	if errors.Is(err, pgx.ErrNoRows) {
		return Run{}, ErrNotFound
	}
	return run, err
}

// StartRun records a run the harness accepted, and leaves a run already recorded as it is.
func (s *Store) StartRun(ctx context.Context, run Run) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO agent_runs (id, user_id, agent, status) VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO NOTHING`,
		run.ID, run.UserID, run.Agent, RunRunning,
	)
	return err
}

// RelayedEvent records that the run's events up to eventID reached the app.
func (s *Store) RelayedEvent(ctx context.Context, id string, eventID int64) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE agent_runs SET last_event_id = GREATEST(last_event_id, $2), active_at = now() WHERE id = $1`,
		id, eventID,
	)
	return err
}

// FinishRun records the run's end with status, after relaying its events up to eventID.
func (s *Store) FinishRun(ctx context.Context, id string, status RunStatus, eventID int64) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE agent_runs
		SET status = $2, last_event_id = GREATEST(last_event_id, $3), active_at = now(), finished_at = now()
		WHERE id = $1`,
		id, status, eventID,
	)
	return err
}
