from typing import Annotated, Literal
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.config import AuthSettings


class Claims(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, use_attribute_docstrings=True
    )

    user_id: UUID = Field(alias="sub")
    """The user the token speaks for, its sub claim"""

    client_id: str = Field(alias="azp")
    """The client calling the harness, its azp claim, such as zumi-channel"""

    token_type: Literal["Bearer"] = Field(alias="typ")
    """The token's typ claim, Bearer on a Keycloak access token and ID on an ID
    token, which is refused"""


def verified_claims(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> Claims:
    """The claims of the request's bearer access token, checked against the realm's
    signing keys, issuer, audience and lifetime; 401 when it fails any check"""
    settings: AuthSettings = request.app.state.auth
    token = credentials.credentials
    try:
        return Claims.model_validate(
            jwt.decode(
                token,
                request.app.state.jwks.get_signing_key_from_jwt(token),
                algorithms=list(settings.algorithms),
                audience=settings.audience,
                issuer=settings.issuer,
                leeway=settings.leeway_s,
                options={"require": ["exp", "iss", "aud", "sub", "azp", "typ"]},
            )
        )
    except jwt.PyJWKClientConnectionError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The realm's signing keys are unreachable",
        ) from error
    except (jwt.PyJWTError, ValidationError) as error:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"Invalid token: {error}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
