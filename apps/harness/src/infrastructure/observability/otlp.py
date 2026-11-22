from typing import Literal

from opentelemetry.sdk.resources import Resource

from src.core.config import ObservabilitySettings, Settings


def build_resource(settings: Settings) -> Resource:
    """The service name, version and environment every span and metric carries"""
    return Resource.create(
        {
            "service.name": settings.observability.service_name,
            "service.version": settings.app.version,
            "deployment.environment": settings.app.env,
        }
    )


def build_endpoint(
    observability: ObservabilitySettings, signal: Literal["traces", "metrics"]
) -> str:
    """The OTLP HTTP endpoint of a signal under the configured base endpoint"""
    endpoint = str(observability.otlp_endpoint).rstrip("/")
    suffix = f"/v1/{signal}"
    return endpoint if endpoint.endswith(suffix) else f"{endpoint}{suffix}"


def parse_headers(observability: ObservabilitySettings) -> dict[str, str] | None:
    """The configured OTLP headers by name, or None when there are none"""
    if observability.otlp_headers is None:
        return None
    pairs = (
        pair.partition("=")
        for pair in observability.otlp_headers.get_secret_value().split(",")
    )
    headers = {key.strip(): value.strip() for key, _, value in pairs if key.strip()}
    return headers or None
