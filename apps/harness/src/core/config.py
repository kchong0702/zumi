import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self, override

from dotenv import load_dotenv
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

_ROOT_PATH = Path(__file__).resolve().parents[2]
_DEFAULT_TOML = _ROOT_PATH / "config.toml"
_DEFAULT_ENV_FILE = _ROOT_PATH / ".env"

load_dotenv(_DEFAULT_ENV_FILE, override=False)

Env = Literal["dev", "staging", "prod"]
"""Where the app runs"""

Family = Literal["claude", "glm", "openai"]
"""A model family, which picks the provider its models run on"""


class AppSettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    name: str = "harness"
    """Application name"""

    env: Env = "dev"
    """Where the app runs: dev on a developer's machine, staging for integration
    tests, prod for users"""

    version: str = "0.1.0"
    """Application's version"""

    host: str = "127.0.0.1"
    """Host address to bind the server to"""

    port: int = Field(default=8300, gt=0)
    """Port to listen on"""

    workers: int = Field(default=1, gt=0)
    """Number of uvicorn worker processes"""

    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    """Uvicorn log level"""

    access_log: bool = True
    """Whether uvicorn logs each request"""


class TraceRedactionSettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    hide_inputs: bool = False
    """Hide everything a span received: its input value, LLM input messages,
    prompts, tool definitions and input images"""

    hide_outputs: bool = False
    """Hide everything a span produced: its output value, LLM output messages,
    choices and output images"""

    hide_input_messages: bool = False
    """Drop LLM input messages from spans, keeping the span's input value"""

    hide_output_messages: bool = False
    """Drop LLM output messages from spans, keeping the span's output value"""

    hide_input_text: bool = False
    """Redact only the text of LLM input messages, keeping roles and structure"""

    hide_output_text: bool = False
    """Redact only the text of LLM output messages, keeping roles and structure"""

    hide_input_images: bool = False
    """Drop images from LLM input messages"""

    hide_embedding_vectors: bool = False
    """Redact embedding vectors"""

    hide_embeddings_text: bool = False
    """Redact the text that was embedded"""

    hide_llm_invocation_parameters: bool = False
    """Drop LLM invocation parameters, such as the model name and temperature"""

    base64_image_max_length: int = Field(default=32_000, gt=0)
    """Longest base64 image data URI kept on a span; longer images are redacted"""


class ObservabilitySettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    service_name: str = "harness"
    """Service name on every log line, span and metric"""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    """Lowest level the app logs"""

    log_format: Literal["json", "console"] = "json"
    """JSON lines for machines, or coloured text for people"""

    tracing_enabled: bool = False
    """Whether to export traces over OTLP to otlp_endpoint"""

    metrics_enabled: bool = False
    """Whether to export metrics over OTLP to otlp_endpoint"""

    metrics_export_interval_ms: int = Field(default=60_000, gt=0)
    """Time between metric exports, in milliseconds"""

    otlp_endpoint: HttpUrl | None = None
    """OTLP HTTP base endpoint shared by traces and metrics, such as
    http://localhost:4318"""

    otlp_headers: SecretStr | None = None
    """OTLP headers as comma-separated key=value pairs"""

    instrumentations: tuple[Literal["langchain"], ...] = ()
    """OpenInference instrumentations to turn on when tracing is enabled"""

    trace_redaction: TraceRedactionSettings = Field(
        default_factory=TraceRedactionSettings
    )
    """What instrumented spans hide or redact"""

    @model_validator(mode="after")
    def _exporters_have_endpoint(self) -> Self:
        """Refuses an enabled exporter without otlp_endpoint"""
        for signal, enabled in (
            ("tracing", self.tracing_enabled),
            ("metrics", self.metrics_enabled),
        ):
            if enabled and self.otlp_endpoint is None:
                raise ValueError(
                    f"observability.{signal}_enabled is true but otlp_endpoint is not set"
                )
        return self


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    url: SecretStr = SecretStr("postgresql+psycopg://zumi:zumi@localhost:5433/zumi")
    """SQLAlchemy async URL of the harness's Postgres, on psycopg"""

    schema_name: str = Field(default="harness", pattern=r"^[a-z_][a-z0-9_]*$")
    """The schema that holds every table of the harness"""

    pool_size: int = Field(default=5, gt=0)
    """Connections each pool keeps open"""


class AuthSettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    issuer: str = "http://localhost:8180/realms/zumi"
    """The Keycloak realm every token must be issued by, its iss claim"""

    jwks_url: str = "http://localhost:8180/realms/zumi/protocol/openid-connect/certs"
    """Where the realm's signing keys are fetched from, which inside Docker is
    Keycloak's own address rather than the issuer's"""

    audience: str = "zumi-harness"
    """The audience every token must name in its aud claim"""

    leeway_s: float = Field(default=30, ge=0)
    """Seconds of clock skew allowed when checking exp and nbf"""

    algorithms: tuple[str, ...] = ("RS256",)
    """The signing algorithms a token may use"""


class AgentSettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    name: str
    """Agent name, such as sothoth"""

    path: Path
    """The agent's profile.yaml, relative to the app folder; its folder holds
    system.md and skills/"""

    family: Family
    """Model family the model belongs to, such as openai, which picks its provider"""

    model: str
    """Exact model the agent runs on, such as gpt-6-luna; one [models] offers"""

    @field_validator("path")
    @classmethod
    def _from_app_folder(cls, path: Path) -> Path:
        """Reads a relative path from the app folder"""
        return path if path.is_absolute() else _ROOT_PATH / path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZUMI_HARNESS_",
        env_nested_delimiter="__",
        env_file=str(_DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        frozen=True,
        validate_default=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    """Application and uvicorn server settings"""

    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    """Logging, tracing and metrics settings"""

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    """The Postgres that keeps threads, runs, transcripts and checkpoints"""

    auth: AuthSettings = Field(default_factory=AuthSettings)
    """How callers' Keycloak tokens are checked"""

    models: dict[Family, tuple[str, ...]] = {}
    """The models agents can run on, by family, read from config.toml's [models]"""

    agents: tuple[AgentSettings, ...] = ()
    """Every agent the app builds, read from config.toml's [[agents]] tables"""

    reasoning_effort: Literal["low", "medium", "high"] = "low"
    """How hard claude and openai models reason before answering"""

    recursion_limit: int = Field(default=100, gt=0)
    """Most graph steps one run may take before it fails; each model call and each
    tool call is a step"""

    gateway_url: HttpUrl | None = Field(
        default=None, validation_alias=AliasChoices("AIGW_BASE_URL", "gateway_url")
    )
    """OpenAI-compatible gateway that serves the glm model family"""

    gateway_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("AIGW_API_KEY", "gateway_key")
    )
    """Key for the model gateway"""

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Reads init values, then the environment, then .env, then config.toml, then
        secret files"""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(
                settings_cls,
                toml_file=os.environ.get("ZUMI_HARNESS_CONFIG", _DEFAULT_TOML),
            ),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _agents_run_offered_models(self) -> Self:
        """Refuses an agent whose model [models] does not offer"""
        for agent in self.agents:
            if agent.model not in self.models.get(agent.family, ()):
                raise ValueError(
                    f"{agent.name} runs {agent.family} {agent.model}, which [models] does not offer"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The settings, read once per process"""
    return Settings()
