FROM golang:1.27.4-alpine AS channel-service-build

WORKDIR /src

COPY apps/channel-service/go.mod apps/channel-service/go.sum ./

RUN go mod download

COPY apps/channel-service ./

RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/channel-service ./cmd/channel-service

FROM gcr.io/distroless/static-debian12:nonroot AS channel-service

ENV ZUMI_CHANNEL_APP__HOST=0.0.0.0 \
    ZUMI_CHANNEL_APP__PORT=8000

WORKDIR /app

COPY --from=channel-service-build /out/channel-service /app/channel-service

COPY apps/channel-service/config.toml /app/config.toml

USER nonroot:nonroot

EXPOSE 8000

ENTRYPOINT ["/app/channel-service"]
