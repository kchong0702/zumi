import json
import os
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.language_models import BaseChatModel, LangSmithParams
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from src.core.config import Settings, get_settings

ISSUER = "http://127.0.0.1/realms/zumi"
USER = "4f8e2a6c-1b3d-4c5e-9f7a-2b4c6d8e0a1f"


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keeps the developer's .env and ZUMI_HARNESS_* variables out of tests"""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in list(os.environ):
        if key.startswith("ZUMI_HARNESS_") and key != "ZUMI_HARNESS_TEST_DATABASE_URL":
            monkeypatch.delenv(key)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class ScriptedChatModel(BaseChatModel):
    replies: list[AIMessage]
    requests: list[list[BaseMessage]] = Field(default_factory=list)
    offered_tools: list[set[str]] = Field(default_factory=list)
    tool_descriptions: dict[str, str] = Field(default_factory=dict)

    @property
    @override
    def _llm_type(self) -> str:
        return "scripted"

    @override
    def _get_ls_params(
        self, stop: list[str] | None = None, **kwargs: Any
    ) -> LangSmithParams:
        return LangSmithParams(ls_provider="openai", ls_model_name="gpt-6-luna")

    @override
    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable:
        functions = [convert_to_openai_tool(tool)["function"] for tool in tools]
        self.offered_tools.append({function["name"] for function in functions})
        self.tool_descriptions.update(
            {
                function["name"]: function.get("description", "")
                for function in functions
            }
        )
        return self

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.requests.append(messages)
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        return ChatResult(generations=[ChatGeneration(message=reply)])

    @override
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        reply = self._generate(messages, stop, run_manager, **kwargs).generations[0]
        message = reply.message
        assert isinstance(message, AIMessage)
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=message.content,
                usage_metadata=message.usage_metadata,
                tool_call_chunks=[
                    tool_call_chunk(
                        name=call["name"],
                        args=json.dumps(call["args"]),
                        id=call["id"],
                        index=index,
                    )
                    for index, call in enumerate(message.tool_calls)
                ],
            )
        )


@dataclass
class Realm:
    issuer: str
    jwks_url: str
    key: rsa.RSAPrivateKey
    kid: str
    fetches: list[str] = field(default_factory=list)

    def token(self, **claims: object) -> str:
        """A token signed by the realm's key, for USER through zumi-channel unless
        claims say otherwise"""
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": ["zumi-harness"],
                "azp": "zumi-channel",
                "sub": USER,
                "typ": "Bearer",
                "iat": now,
                "exp": now + 300,
                **claims,
            },
            self.key,
            algorithm="RS256",
            headers={"kid": self.kid},
        )

    def headers(self, **claims: object) -> dict[str, str]:
        """An Authorization header carrying token(**claims)"""
        return {"Authorization": f"Bearer {self.token(**claims)}"}


class _JwksHandler(BaseHTTPRequestHandler):
    realm: Realm

    def do_GET(self) -> None:
        self.realm.fetches.append(self.path)
        key = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(self.realm.key.public_key())
        )
        body = json.dumps(
            {"keys": [{**key, "kid": self.realm.kid, "use": "sig", "alg": "RS256"}]}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def realm(monkeypatch: pytest.MonkeyPatch) -> Iterator[Realm]:
    """A stand-in for the Keycloak realm, serving its own key's JWKS, which the
    app's settings point at"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    found = Realm(
        issuer=ISSUER,
        jwks_url=f"http://127.0.0.1:{server.server_port}/certs",
        key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
        kid="test-key",
    )
    _JwksHandler.realm = found
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ZUMI_HARNESS_AUTH__ISSUER", found.issuer)
    monkeypatch.setenv("ZUMI_HARNESS_AUTH__JWKS_URL", found.jwks_url)
    get_settings.cache_clear()
    yield found
    server.shutdown()
