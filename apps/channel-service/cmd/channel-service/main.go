package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/kchong0702/zumi/apps/channel-service/internal/auth"
	"github.com/kchong0702/zumi/apps/channel-service/internal/channel"
	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
	"github.com/kchong0702/zumi/apps/channel-service/internal/harness"
	"github.com/kchong0702/zumi/apps/channel-service/internal/store"
	"github.com/kchong0702/zumi/apps/channel-service/internal/telemetry"
)

func run(args []string) error {
	cfg, err := config.Load("config.toml")
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	shutdownTelemetry, err := telemetry.Setup(ctx, cfg.App, cfg.Observability)
	if err != nil {
		return err
	}
	defer func() {
		flushCtx, cancel := context.WithTimeout(context.Background(), cfg.App.ShutdownTimeout)
		defer cancel()
		if err := shutdownTelemetry(flushCtx); err != nil {
			slog.Warn("could not flush telemetry", "error", err)
		}
	}()
	db, err := store.Open(ctx, cfg.Database.URL, cfg.Database.SchemaName)
	if err != nil {
		return err
	}
	defer db.Close()

	switch {
	case len(args) == 1 && args[0] == "migrate":
		return db.Migrate(ctx)
	case len(args) == 2 && args[0] == "migrate" && args[1] == "down":
		return db.Reset(ctx)
	case len(args) > 0:
		return fmt.Errorf("unknown command %q: run with no arguments, migrate, or migrate down", args)
	}

	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = cfg.App.HeaderTimeout
	client := &http.Client{Transport: otelhttp.NewTransport(transport)}
	service := channel.New(cfg,
		auth.NewVerifier(ctx, client, cfg.Auth),
		auth.NewExchanger(client, cfg.Auth),
		harness.NewClient(client, cfg.Harness),
		db,
	)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "version": cfg.App.Version})
	})
	mux.Handle("GET /ws", service)
	server := &http.Server{
		Addr: net.JoinHostPort(cfg.App.Host, strconv.Itoa(cfg.App.Port)),
		Handler: otelhttp.NewHandler(mux, "channel-service",
			otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string { return r.Method + " " + r.Pattern }),
			otelhttp.WithFilter(func(r *http.Request) bool { return r.URL.Path != "/ws" }),
		),
		ReadHeaderTimeout: cfg.App.HeaderTimeout,
	}
	served := make(chan error, 1)
	go func() { served <- server.ListenAndServe() }()
	slog.Info("channel service listening", "addr", server.Addr, "version", cfg.App.Version)
	select {
	case err := <-served:
		return err
	case <-ctx.Done():
	}
	slog.Info("channel service stopping")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.App.ShutdownTimeout)
	defer cancel()
	err = server.Shutdown(shutdownCtx)
	service.Close()
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		slog.Error("channel service stopped", "error", err)
		os.Exit(1)
	}
}
