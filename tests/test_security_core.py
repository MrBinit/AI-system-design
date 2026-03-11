import pytest

from app.core import security


def test_create_and_decode_access_token_with_audience():
    token = security.create_access_token(user_id="user-1", roles=["user"])

    payload = security.decode_access_token(token)

    assert payload["sub"] == "user-1"
    assert payload["aud"] == security.settings.security.jwt_audience


def test_decode_access_token_rejects_wrong_audience():
    token = security.create_access_token(
        user_id="user-1",
        roles=["user"],
        audience="unexpected-audience",
    )

    with pytest.raises(Exception) as exc_info:
        security.decode_access_token(token)

    assert security.is_jwt_error(exc_info.value) is True
