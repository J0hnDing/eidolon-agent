"""Trusted ingress and capability relay for no-internet web application containers."""

import asyncio
import os
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.request import Request as UrlRequest

import uvicorn
from fastapi import FastAPI, Request, Response

MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 5_000_000
CODEX_CAPABILITY_PATH = "/web-apps/capabilities/codex"

APP_HOST = os.environ.get("PERSONAL_AGENT_RELAY_APP_HOST", "")
APP_PORT = int(os.environ.get("PERSONAL_AGENT_RELAY_APP_PORT", "8000"))
BACKEND_URL = os.environ.get("PERSONAL_AGENT_RELAY_BACKEND_URL", "").rstrip("/")

capability_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


@capability_app.post(CODEX_CAPABILITY_PATH)
async def forward_codex_capability(request: Request) -> Response:
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_REQUEST_BYTES:
        return Response("Capability request is too large", status_code=413, media_type="text/plain")
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        return Response("Capability request is too large", status_code=413, media_type="text/plain")
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return Response("Bearer capability token is required", status_code=401, media_type="text/plain")
    if not BACKEND_URL.startswith("http://"):
        return Response("Trusted backend relay target is invalid", status_code=502, media_type="text/plain")
    upstream_request = UrlRequest(
        f"{BACKEND_URL}{CODEX_CAPABILITY_PATH}",
        data=body,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
            "User-Agent": "personal-agent-web-runtime-relay",
        },
        method="POST",
    )
    try:
        response_body, response_status, content_type = await asyncio.to_thread(_read_backend, upstream_request)
    except (OSError, URLError) as exc:
        return Response(f"Trusted backend capability is unavailable: {exc}", status_code=502, media_type="text/plain")
    return Response(response_body, status_code=response_status, media_type=content_type)


def _read_backend(request: UrlRequest) -> tuple[bytes, int, str]:
    try:
        upstream = build_opener(_NoRedirect()).open(request, timeout=120)
    except HTTPError as exc:
        upstream = exc
    try:
        body = upstream.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise OSError("Trusted backend capability response exceeded the size limit")
        status = int(upstream.status)
        if 300 <= status < 400:
            raise OSError("Trusted backend capability redirects are not supported")
        return body, status, upstream.headers.get_content_type()
    finally:
        upstream.close()


async def relay_application(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        app_reader, app_writer = await asyncio.open_connection(APP_HOST, APP_PORT)
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()
        return

    async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(64 * 1024):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.write_eof()
            except (AttributeError, OSError):
                pass

    try:
        await asyncio.gather(
            copy_stream(client_reader, app_writer),
            copy_stream(app_reader, client_writer),
        )
    finally:
        app_writer.close()
        client_writer.close()
        await asyncio.gather(app_writer.wait_closed(), client_writer.wait_closed(), return_exceptions=True)


async def main() -> None:
    if not APP_HOST:
        raise RuntimeError("PERSONAL_AGENT_RELAY_APP_HOST is required")
    ingress = await asyncio.start_server(relay_application, "0.0.0.0", 8000)
    capability = uvicorn.Server(
        uvicorn.Config(
            capability_app,
            host="0.0.0.0",
            port=8001,
            access_log=False,
            lifespan="off",
            log_level="warning",
        )
    )
    async with ingress:
        await asyncio.gather(ingress.serve_forever(), capability.serve())


if __name__ == "__main__":
    asyncio.run(main())
