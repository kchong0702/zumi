-- +goose Up
CREATE TABLE agent_runs (
    id text PRIMARY KEY,
    user_id uuid NOT NULL,
    channel_id uuid,
    agent text NOT NULL,
    status text NOT NULL,
    last_event_id bigint NOT NULL DEFAULT 0,
    owner text,
    heartbeat_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    active_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX agent_runs_user_id_created_at_idx ON agent_runs (user_id, created_at);

CREATE INDEX agent_runs_heartbeat_at_idx ON agent_runs (heartbeat_at) WHERE status = 'running';

-- +goose Down
DROP TABLE agent_runs;
