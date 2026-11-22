import base64
import hashlib
import html
import http.cookiejar
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("KEYCLOAK_URL", "http://localhost:8180")
REALM = "zumi"
TOKEN_URL = f"{BASE}/realms/{REALM}/protocol/openid-connect/token"
REDIRECT_URI = "http://127.0.0.1:53682/callback"
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
SECRETS = {
    "zumi-channel": os.environ.get(
        "KEYCLOAK_CHANNEL_CLIENT_SECRET", "dev-zumi-channel-secret"
    ),
    "zumi-harness": os.environ.get(
        "KEYCLOAK_HARNESS_CLIENT_SECRET", "dev-zumi-harness-secret"
    ),
}


class _StopAtRedirectUri(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Follows redirects until the one back to the app's redirect URI"""
        if newurl.startswith(REDIRECT_URI):
            raise _Redirected(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Redirected(Exception):
    pass


class _LocalhostCookies(http.cookiejar.DefaultCookiePolicy):
    def return_ok_secure(self, cookie, request):
        """Sends Secure cookies over plain-HTTP localhost, as browsers do"""
        return True


def _post(url, form, client=None, token=None, body=None):
    """POSTs a form or JSON, as a client or with a bearer token, and returns the JSON reply"""
    headers = {}
    if client:
        credentials = base64.b64encode(f"{client}:{SECRETS[client]}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    else:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    with urllib.request.urlopen(urllib.request.Request(url, data, headers)) as reply:
        text = reply.read().decode()
        return json.loads(text) if text else None


def _claims(token):
    """The claims of a JWT, without checking its signature"""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _admin_token():
    """A token for the master realm's admin, from the bootstrap credentials"""
    return _post(
        f"{BASE}/realms/master/protocol/openid-connect/token",
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": os.environ.get("KEYCLOAK_ADMIN_USERNAME", "admin"),
            "password": os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin"),
        },
    )["access_token"]


def _sign_in(email, password):
    """Signs in through the zumi-app login page with PKCE and returns its tokens"""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(
            http.cookiejar.CookieJar(_LocalhostCookies())
        ),
        _StopAtRedirectUri,
    )
    query = urllib.parse.urlencode(
        {
            "client_id": "zumi-app",
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": "openid",
            "state": secrets.token_urlsafe(16),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    page = (
        opener.open(f"{BASE}/realms/{REALM}/protocol/openid-connect/auth?{query}")
        .read()
        .decode()
    )
    action = html.unescape(
        re.search(r'id="kc-form-login"[^>]*action="([^"]+)"', page, re.DOTALL).group(1)
    )
    try:
        opener.open(
            action,
            urllib.parse.urlencode({"username": email, "password": password}).encode(),
        )
    except _Redirected as redirected:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(str(redirected)).query)[
            "code"
        ][0]
    else:
        raise SystemExit("sign-in did not redirect back to the app")
    return _post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": "zumi-app",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )


def _exchange(client, subject_token, audience):
    """Exchanges subject_token, as client, for an access token aimed at audience"""
    return _post(
        TOKEN_URL,
        {
            "grant_type": EXCHANGE,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN,
            "requested_token_type": ACCESS_TOKEN,
            "audience": audience,
        },
        client=client,
    )["access_token"]


def _show(label, token):
    """Prints the claims a service checks"""
    claims = _claims(token)
    picked = {
        key: claims.get(key)
        for key in ("iss", "aud", "azp", "sub", "email", "zoneinfo", "locale")
    }
    picked["country"] = (claims.get("address") or {}).get("country")
    print(f"{label}: {json.dumps(picked)}")
    return claims


def main():
    """Checks sign-in, both token exchanges and client credentials against a running Keycloak"""
    admin = _admin_token()
    email, password = (
        f"verify-{secrets.token_hex(4)}@zumi.test",
        secrets.token_urlsafe(18),
    )
    users = f"{BASE}/admin/realms/{REALM}/users"
    _post(
        users,
        None,
        token=admin,
        body={
            "username": email,
            "email": email,
            "emailVerified": True,
            "enabled": True,
            "firstName": "Verify",
            "lastName": "User",
            "attributes": {
                "zoneinfo": ["Asia/Kuala_Lumpur"],
                "country": ["MY"],
                "locale": ["en"],
            },
            "credentials": [
                {"type": "password", "value": password, "temporary": False}
            ],
        },
    )
    request = urllib.request.Request(
        f"{users}?email={urllib.parse.quote(email)}&exact=true",
        headers={"Authorization": f"Bearer {admin}"},
    )
    user_id = json.load(urllib.request.urlopen(request))[0]["id"]
    failures = []
    try:
        app = _show("app token", _sign_in(email, password)["access_token"])
        app_token = _sign_in(email, password)["access_token"]
        harness = _show(
            "channel → harness", _exchange("zumi-channel", app_token, "zumi-harness")
        )
        connectors = _show(
            "harness → connectors",
            _exchange(
                "zumi-harness",
                _exchange("zumi-channel", app_token, "zumi-harness"),
                "zumi-connectors",
            ),
        )
        service = _show(
            "channel service account",
            _post(
                TOKEN_URL, {"grant_type": "client_credentials"}, client="zumi-channel"
            )["access_token"],
        )
        checks = {
            "app token is for the channel service": "zumi-channel" in app["aud"]
            if isinstance(app["aud"], list)
            else app["aud"] == "zumi-channel",
            "app token carries zoneinfo, locale and country": (
                app.get("zoneinfo"),
                app.get("locale"),
                (app.get("address") or {}).get("country"),
            )
            == ("Asia/Kuala_Lumpur", "en", "MY"),
            "harness token: aud zumi-harness, azp zumi-channel, same user": (
                harness["aud"],
                harness["azp"],
                harness["sub"],
            )
            == ("zumi-harness", "zumi-channel", app["sub"]),
            "connectors token: aud zumi-connectors, azp zumi-harness, same user": (
                connectors["aud"],
                connectors["azp"],
                connectors["sub"],
            )
            == ("zumi-connectors", "zumi-harness", app["sub"]),
            "service account token has no user email": service.get("email") is None,
        }
        try:
            _exchange("zumi-channel", app_token, "zumi-connectors")
            checks["channel cannot get a connectors token"] = False
        except urllib.error.HTTPError as error:
            print(
                f"channel → connectors refused: {error.code} {json.loads(error.read()).get('error_description')}"
            )
            checks["channel cannot get a connectors token"] = error.code == 400
        for name, passed in checks.items():
            print(f"{'PASS' if passed else 'FAIL'} {name}")
            if not passed:
                failures.append(name)
    finally:
        urllib.request.urlopen(
            urllib.request.Request(
                f"{users}/{user_id}",
                method="DELETE",
                headers={"Authorization": f"Bearer {admin}"},
            )
        )
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
