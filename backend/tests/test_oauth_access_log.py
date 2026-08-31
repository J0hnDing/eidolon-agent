import logging

import pytest

from app.main import GOOGLE_OAUTH_CALLBACK_PATHS, OAuthCallbackAccessLogFilter


@pytest.mark.parametrize("callback_path", GOOGLE_OAUTH_CALLBACK_PATHS)
def test_google_oauth_authorization_code_is_redacted_from_access_log(callback_path: str) -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:1234",
            "GET",
            f"{callback_path}?state=safe&code=SECRET_AUTHORIZATION_CODE",
            "1.1",
            303,
        ),
        exc_info=None,
    )

    assert OAuthCallbackAccessLogFilter().filter(record) is True
    assert record.args[2] == callback_path
    assert "SECRET_AUTHORIZATION_CODE" not in record.getMessage()
