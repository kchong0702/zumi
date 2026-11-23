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

    upkeep_pool_size: int = Field(default=2, gt=0)
    """Connections the engine that marks runs alive and sweeps keeps open"""


class RunSettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    heartbeat_s: float = Field(default=10, gt=0)
    """How often a process marks the runs it works on as alive"""

    sweep_s: float = Field(default=15, gt=0)
    """How often a process looks for runs whose process stopped marking them alive"""

    dead_after_s: float = Field(default=30, gt=0)
    """A running run not marked alive for this long is taken over"""

    resume_within_s: float = Field(default=600, gt=0)
    """A taken-over run started this recently resumes; an older one ends as a
    timeout"""

    poll_s: float = Field(default=5, gt=0)
    """Longest a reader waits for a run's next event before looking again, in case
    a notification was missed"""

    max_workers: int = Field(default=4, ge=1)
    """Most runs one process works on at once"""

    max_attempts: int = Field(default=3, ge=1)
    """Most times a run is taken over and resumed before it ends as a timeout"""

    retry_after_s: int = Field(default=5, gt=0)
    """The Retry-After a run refused by a process at max_workers gets"""

    @model_validator(mode="after")
    def _lease_outlives_heartbeats(self) -> Self:
        """Refuses a dead_after_s shorter than two heartbeats"""
        if self.dead_after_s < 2 * self.heartbeat_s:
            raise ValueError("runs.dead_after_s must be at least twice heartbeat_s")
        return self


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


class BrowserCommand(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    transport: Literal["stdio"] = "stdio"
    """Start Playwright MCP for each run and talk to it over stdin and stdout"""

    command: str = "npx"
    """The program that starts Playwright MCP"""

    args: tuple[str, ...] = (
        "-y",
        "@playwright/mcp@0.0.85",
        "--headless",
        "--isolated",
        "--snapshot-mode",
        "none",
    )
    """Playwright MCP's arguments, which pick the browser it drives, such as
    --cdp-endpoint for a Chrome that is already running"""


class BrowserServer(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    transport: Literal["streamable_http"]
    """Connect to a Playwright MCP server that is already running, such as one on the
    machine whose browser it drives"""

    url: str
    """The server's MCP endpoint, such as http://browser-host:8931/mcp"""


class SearchSettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    tavily_url: str = "https://api.tavily.com"
    """Tavily's API, for keyword search"""

    exa_url: str = "https://api.exa.ai"
    """Exa's API, for search by meaning"""

    searxng_url: str = "http://127.0.0.1:8310"
    """The SearXNG server, asked when Tavily or Exa has no key or fails"""

    max_results: int = Field(default=5, gt=0)
    """Most results a search shows the agent"""

    max_content_chars: int = Field(default=4_000, gt=0)
    """Most characters of each result's text a search shows the agent, the rest cut"""

    timeout_s: float = Field(default=20, gt=0)
    """Longest wait on any one network step of a search service, such as connecting
    or reading its answer"""


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

    runs: RunSettings = Field(default_factory=RunSettings)
    """How processes keep their runs alive and take over those of stopped ones"""

    auth: AuthSettings = Field(default_factory=AuthSettings)
    """How callers' Keycloak tokens are checked"""

    browser: BrowserCommand | BrowserServer = Field(
        default_factory=BrowserCommand, discriminator="transport"
    )
    """How the agents reach Playwright MCP, which drives their browser"""

    search: SearchSettings = Field(default_factory=SearchSettings)
    """The web search services and limits on their results"""

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

    tavily_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("TAVILY_API_KEY", "tavily_api_key")
    )
    """Key for Tavily, which is skipped without it"""

    exa_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("EXA_API_KEY", "exa_api_key")
    )
    """Key for Exa, which is skipped without it"""

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
