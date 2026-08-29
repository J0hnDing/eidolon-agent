import logging

from app.main import GOOGLE_OAUTH_CALLBACK_PATH, OAuthCallbackAccessLogFilter


def test_google_oauth_authorization_code_is_redacted_from_access_log() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:1234",
            "GET",
            f"{GOOGLE_OAUTH_CALLBACK_PATH}?state=safe&code=SECRET_AUTHORIZATION_CODE",
            "1.1",
            303,
        ),
        exc_info=None,
    )

    assert OAuthCallbackAccessLogFilter().filter(record) is True
    assert record.args[2] == GOOGLE_OAUTH_CALLBACK_PATH
    assert "SECRET_AUTHORIZATION_CODE" not in record.getMessage()
