package telemetry

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"

	"github.com/kchong0702/zumi/apps/channel-service/internal/config"
)

// Setup sends JSON or text logs to stderr and, when enabled, traces and metrics over OTLP HTTP.
// The returned function flushes and stops the exporters.
func Setup(ctx context.Context, app config.App, o config.Observability) (func(context.Context) error, error) {
	options := &slog.HandlerOptions{Level: o.LogLevel}
	var handler slog.Handler = slog.NewJSONHandler(os.Stderr, options)
	if o.LogFormat == "text" {
		handler = slog.NewTextHandler(os.Stderr, options)
	}
	slog.SetDefault(slog.New(handler).With("service", o.ServiceName, "env", app.Env))

	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	res := resource.NewWithAttributes(semconv.SchemaURL,
		semconv.ServiceName(o.ServiceName),
		semconv.ServiceVersion(app.Version),
		semconv.DeploymentEnvironmentNameKey.String(app.Env),
	)
	var shutdowns []func(context.Context) error
	if o.TracingEnabled {
		exporter, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(o.OTLPEndpoint+"/v1/traces"))
		if err != nil {
			return nil, fmt.Errorf("create the trace exporter: %w", err)
		}
		provider := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exporter), sdktrace.WithResource(res))
		otel.SetTracerProvider(provider)
		shutdowns = append(shutdowns, provider.Shutdown)
	}
	if o.MetricsEnabled {
		exporter, err := otlpmetrichttp.New(ctx, otlpmetrichttp.WithEndpointURL(o.OTLPEndpoint+"/v1/metrics"))
		if err != nil {
			return nil, fmt.Errorf("create the metric exporter: %w", err)
		}
		provider := sdkmetric.NewMeterProvider(
			sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter, sdkmetric.WithInterval(o.MetricsExportInterval))),
			sdkmetric.WithResource(res),
		)
		otel.SetMeterProvider(provider)
		shutdowns = append(shutdowns, provider.Shutdown)
	}
	return func(ctx context.Context) error {
		var errs []error
		for _, shutdown := range shutdowns {
			errs = append(errs, shutdown(ctx))
		}
		return errors.Join(errs...)
	}, nil
}
