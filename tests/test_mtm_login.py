"""POST /system/mtm only answers a trusted service that proves its own secret.

The route used to ignore the client_id and client_secret it was sent and hand
this API's service-account token (realm admin) to anyone. Keycloak is stubbed
at its seams: `keycloak.KeycloakOpenID` is the caller's own client, checked
with the caller's secret, and `kutils.KEYCLOAK_OPENID_CLIENT` is this API's.
"""
from unittest import mock

import pytest
from httpx import ASGITransport, AsyncClient
from keycloak import KeycloakAuthenticationError, KeycloakConnectionError, KeycloakPostError

import kutils
import main
from exceptions import AuthenticationError

API_TOKEN = {"access_token": "api-service-account-token", "token_type": "Bearer"}


@pytest.fixture
def keycloak():
    """The caller's client (constructor + token) and this API's own client."""
    with mock.patch("keycloak.KeycloakOpenID") as caller_cls, mock.patch.object(
        kutils, "KEYCLOAK_OPENID_CLIENT"
    ) as api_client:
        api_client.return_value.token.return_value = API_TOKEN
        yield caller_cls, api_client.return_value


def test_untrusted_client_is_refused_without_asking_keycloak(keycloak):
    caller_cls, api = keycloak

    with pytest.raises(AuthenticationError):
        kutils.get_client_token(client_id="anything", client_secret="anything")

    caller_cls.assert_not_called()
    api.token.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        KeycloakAuthenticationError(error_message="invalid_client", response_code=401),
        KeycloakPostError(error_message="unauthorized_client", response_code=400),
    ],
    ids=["wrong-secret", "no-service-account"],
)
def test_trusted_client_with_a_bad_secret_gets_nothing(keycloak, error):
    caller_cls, api = keycloak
    caller_cls.return_value.token.side_effect = error

    with pytest.raises(AuthenticationError):
        kutils.get_client_token(client_id="foodchat", client_secret="wrong")

    api.token.assert_not_called()


def test_keycloak_being_down_is_not_reported_as_bad_credentials(keycloak):
    caller_cls, api = keycloak
    caller_cls.return_value.token.side_effect = KeycloakConnectionError("down")

    with pytest.raises(Exception) as raised:
        kutils.get_client_token(client_id="foodchat", client_secret="right")

    assert not isinstance(raised.value, AuthenticationError)
    api.token.assert_not_called()


def test_trusted_client_proves_its_own_secret_before_the_token_is_issued(keycloak):
    caller_cls, api = keycloak

    token = kutils.get_client_token(client_id="foodscholar", client_secret="right")

    assert token == API_TOKEN
    kwargs = caller_cls.call_args.kwargs
    assert kwargs["client_id"] == "foodscholar"
    assert kwargs["client_secret_key"] == "right"
    caller_cls.return_value.token.assert_called_once_with(grant_type="client_credentials")


async def test_route_answers_401_to_an_anonymous_caller(keycloak):
    caller_cls, api = keycloak
    caller_cls.return_value.token.side_effect = KeycloakAuthenticationError(
        error_message="invalid_client", response_code=401
    )

    transport = ASGITransport(app=main.api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        untrusted = await client.post(
            "/api/v1/system/mtm", json={"client_id": "x", "client_secret": "y"}
        )
        wrong_secret = await client.post(
            "/api/v1/system/mtm", json={"client_id": "foodchat", "client_secret": "y"}
        )

    assert untrusted.status_code == 401
    assert wrong_secret.status_code == 401
    assert "api-service-account-token" not in untrusted.text + wrong_secret.text
    api.token.assert_not_called()
