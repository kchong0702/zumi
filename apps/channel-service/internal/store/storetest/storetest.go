package storetest

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/kchong0702/zumi/apps/channel-service/internal/store"
)

// DatabaseURLEnv names the variable that points the Postgres tests at a database.
const DatabaseURLEnv = "ZUMI_CHANNEL_TEST_DATABASE_URL"

// New returns a migrated Store in a scratch schema that is dropped when the test ends.
// It skips the test when DatabaseURLEnv is unset.
func New(t testing.TB) *store.Store {
	url := os.Getenv(DatabaseURLEnv)
	if url == "" {
		t.Skipf("%s is not set", DatabaseURLEnv)
	}
	schema := "channel_test_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	ctx := context.Background()
	s, err := store.Open(ctx, url, schema)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		s.Close()
		conn, err := pgx.Connect(ctx, url)
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.Close(ctx)
		if _, err := conn.Exec(ctx, "DROP SCHEMA "+pgx.Identifier{schema}.Sanitize()+" CASCADE"); err != nil {
			t.Error(err)
		}
	})
	if err := s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	return s
}
