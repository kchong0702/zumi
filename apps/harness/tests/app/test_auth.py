import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from src.app.app import create_app
from src.core.config import get_settings
from tests.conftest import Realm

PATH = "/sothoth/runs/run-1"


@pytest.fixture
def client(realm: Realm) -> TestClient:
    return TestClient(create_app())


def _refused(response) -> str:
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    return response.json()["detail"]


def test_a_request_without_a_token_is_refused(client: TestClient):
    _refused(client.get(PATH))


def test_a_token_signed_by_another_key_is_refused(client: TestClient, realm: Realm):
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        jwt.decode(realm.token(), options={"verify_signature": False}),
        stranger,
        algorithm="RS256",
        headers={"kid": realm.kid},
    )

    assert "Signature verification failed" in _refused(
        client.get(PATH, headers={"Authorization": f"Bearer {token}"})
    )


def test_a_token_from_another_issuer_is_refused(client: TestClient, realm: Realm):
    headers = realm.headers(iss="http://localhost:8180/realms/master")

    assert "Invalid issuer" in _refused(client.get(PATH, headers=headers))


def test_a_token_for_another_audience_is_refused(client: TestClient, realm: Realm):
    headers = realm.headers(aud=["zumi-connectors"])

    assert "Audience doesn't match" in _refused(client.get(PATH, headers=headers))


def test_an_expired_token_is_refused(client: TestClient, realm: Realm):
    headers = realm.headers(exp=int(time.time()) - 60)

    assert "Signature has expired" in _refused(client.get(PATH, headers=headers))


def test_a_token_not_valid_yet_is_refused(client: TestClient, realm: Realm):
    headers = realm.headers(nbf=int(time.time()) + 120)

    assert "The token is not yet valid" in _refused(client.get(PATH, headers=headers))


def test_a_token_without_a_caller_is_refused(client: TestClient, realm: Realm):
    token = jwt.encode(
        {
            key: value
            for key, value in jwt.decode(
                realm.token(), options={"verify_signature": False}
            ).items()
            if key != "azp"
        },
        realm.key,
        algorithm="RS256",
        headers={"kid": realm.kid},
    )

    assert 'missing the "azp" claim' in _refused(
        client.get(PATH, headers={"Authorization": f"Bearer {token}"})
    )


def test_a_token_signed_with_a_shared_secret_is_refused(
    client: TestClient, realm: Realm
):
    token = jwt.encode(
        jwt.decode(realm.token(), options={"verify_signature": False}),
        "a shared secret long enough for HMAC SHA-256 keys",
        algorithm="HS256",
        headers={"kid": realm.kid},
    )

    _refused(client.get(PATH, headers={"Authorization": f"Bearer {token}"}))


def test_an_id_token_is_refused(client: TestClient, realm: Realm):
    headers = realm.headers(typ="ID")

    assert "Input should be 'Bearer'" in _refused(client.get(PATH, headers=headers))


def test_a_token_without_a_type_is_refused(client: TestClient, realm: Realm):
    token = jwt.encode(
        {
            key: value
            for key, value in jwt.decode(
                realm.token(), options={"verify_signature": False}
            ).items()
            if key != "typ"
        },
        realm.key,
        algorithm="RS256",
        headers={"kid": realm.kid},
    )

    assert 'missing the "typ" claim' in _refused(
        client.get(PATH, headers={"Authorization": f"Bearer {token}"})
    )


def test_a_token_signed_with_an_algorithm_not_allowed_is_refused(
    client: TestClient, realm: Realm
):
    token = jwt.encode(
        jwt.decode(realm.token(), options={"verify_signature": False}),
        realm.key,
        algorithm="RS512",
        headers={"kid": realm.kid},
    )

    assert "The specified alg value is not allowed" in _refused(
        client.get(PATH, headers={"Authorization": f"Bearer {token}"})
    )


def test_the_allowed_algorithms_come_from_the_settings(
    realm: Realm, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ZUMI_HARNESS_AUTH__ALGORITHMS", '["RS512"]')
    get_settings.cache_clear()

    response = TestClient(create_app()).get(PATH, headers=realm.headers())

    assert "The specified alg value is not allowed" in _refused(response)
