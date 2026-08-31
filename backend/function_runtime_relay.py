"""Trusted allowlisted backend relay for no-internet bounded function containers."""

import asyncio
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.request import Request as UrlRequest

import uvicorn
from fastapi import FastAPI, Request, Response

MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 5_000_000
UPSTREAM_TIMEOUT_SECONDS = 300
BACKEND_URL = os.environ.get("PERSONAL_AGENT_RELAY_BACKEND_URL", "").rstrip("/")
SAFE_FUNCTION_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/functions")
async def forward_discovery(request: Request) -> Response:
    return await _forward(request, "/functions", b"", "GET")


@app.post("/functions/{function_name}/invoke")
async def forward_invocation(function_name: str, request: Request) -> Response:
    if not SAFE_FUNCTION_NAME.fullmatch(function_name):
        return Response("Invalid function name", status_code=400, media_type="text/plain")
    body = await request.body()
    return await _forward(request, f"/functions/{function_name}/invoke", body, "POST")


@app.post("/integrations/capabilities/invoke")
async def forward_integration_invocation(request: Request) -> Response:
    body = await request.body()
    return await _forward(request, "/integrations/capabilities/invoke", body, "POST")


@app.post("/functions/capabilities/codex")
async def forward_codex_invocation(request: Request) -> Response:
    body = await request.body()
    return await _forward(request, "/functions/capabilities/codex", body, "POST")


async def _forward(request: Request, path: str, body: bytes, method: str) -> Response:
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_REQUEST_BYTES:
        return Response("Capability request is too large", status_code=413, media_type="text/plain")
    if len(body) > MAX_REQUEST_BYTES:
        return Response("Capability request is too large", status_code=413, media_type="text/plain")
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return Response("Bearer capability token is required", status_code=401, media_type="text/plain")
    if not BACKEND_URL.startswith("http://"):
        return Response("Trusted backend relay target is invalid", status_code=502, media_type="text/plain")
    upstream_request = UrlRequest(
        f"{BACKEND_URL}{path}",
        data=body if method == "POST" else None,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
            "User-Agent": "personal-agent-function-runtime-relay",
        },
        method=method,
    )
    try:
        response_body, response_status, content_type = await asyncio.to_thread(_read_backend, upstream_request)
    except (OSError, URLError) as exc:
        return Response(f"Trusted backend capability is unavailable: {exc}", status_code=502, media_type="text/plain")
    return Response(response_body, status_code=response_status, media_type=content_type)


def _read_backend(request: UrlRequest) -> tuple[bytes, int, str]:
    try:
        upstream = build_opener(_NoRedirect()).open(request, timeout=UPSTREAM_TIMEOUT_SECONDS)
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False, log_level="warning")
