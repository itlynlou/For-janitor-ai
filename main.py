"""
OpenAI -> NVIDIA NIM API Proxy Server
======================================

Exposes an OpenAI-compatible surface (`/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`, `/v1/models`) and forwards requests to an NVIDIA NIM
deployment (hosted at build.nvidia.com / integrate.api.nvidia.com, or a
self-hosted NIM container), which itself speaks a near-identical dialect.

Why a proxy at all, if NIM is already OpenAI-compatible?
- Centralize/rotate the real NVIDIA API key so client apps never see it.
- Translate OpenAI model names (gpt-4o, gpt-4o-mini, ...) into NIM model
  names (meta/llama-3.1-405b-instruct, ...) so existing OpenAI SDK code
  works unmodified.
- Add auth, logging, retries, and rate limiting in one place.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000

Then point any OpenAI SDK client at http://localhost:8000/v1 with any
api_key (or PROXY_API_KEY if you set one).
"""
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import FREE_NIM_MODELS, Settings, get_settings, resolve_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nim-proxy")

app = FastAPI(title="OpenAI-to-NIM Proxy", version="1.0.0")

_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup() -> None:
    settings = get_settings()
    global _client
    _client = httpx.AsyncClient(
        base_url=settings.nim_base_url,
        timeout=httpx.Timeout(settings.request_timeout_seconds),
    )
    logger.info("Proxy started. Forwarding to %s", settings.nim_base_url)
    logger.info(
        "Default model: %s | %d model alias(es) configured | %d curated free NIM models known",
        settings.default_model,
        len(settings.model_map),
        len(FREE_NIM_MODELS),
    )
    if not settings.nim_api_key:
        logger.warning(
            "NIM_API_KEY is not set. Get a free key (no credit card) at "
            "https://build.nvidia.com/settings/api-keys and set it in .env"
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    if _client is not None:
        await _client.aclose()


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------

def _check_client_auth(settings: Settings, authorization: Optional[str], x_api_key: Optional[str]) -> None:
    """Validate the caller's credential against PROXY_API_KEY, if configured."""
    if not settings.proxy_api_key:
        return  # open proxy (e.g. local dev) — no client-side check
    supplied = None
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization.split(" ", 1)[1].strip()
    elif x_api_key:
        supplied = x_api_key.strip()
    if supplied != settings.proxy_api_key:
        raise HTTPException(status_code=401, detail={"error": {"message": "Invalid API key", "type": "invalid_request_error"}})


def _nim_headers(settings: Settings) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.nim_api_key:
        headers["Authorization"] = f"Bearer {settings.nim_api_key}"
    return headers


# --------------------------------------------------------------------------
# Request translation
# --------------------------------------------------------------------------

# Fields OpenAI clients commonly send that NIM/vLLM-based backends may not
# accept. Stripped defensively rather than allowed to cause a 422 upstream.
_UNSUPPORTED_FIELDS = {"service_tier", "parallel_tool_calls", "store", "metadata", "user"}


def _translate_chat_request(body: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    payload = dict(body)
    model = payload.get("model") or settings.default_model
    payload["model"] = resolve_model(model, settings)
    for field in _UNSUPPORTED_FIELDS:
        payload.pop(field, None)
    return payload


async def _forward(
    method: str,
    path: str,
    settings: Settings,
    json_body: Optional[Dict[str, Any]] = None,
    stream: bool = False,
) -> httpx.Response:
    assert _client is not None
    headers = _nim_headers(settings)
    last_exc: Optional[Exception] = None
    for attempt in range(settings.max_retries + 1):
        try:
            if stream:
                req = _client.build_request(method, path, json=json_body, headers=headers)
                return await _client.send(req, stream=True)
            return await _client.request(method, path, json=json_body, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            logger.warning("Upstream request failed (attempt %s/%s): %s", attempt + 1, settings.max_retries + 1, exc)
            time.sleep(0.5 * (attempt + 1))
    raise HTTPException(status_code=502, detail={"error": {"message": f"Upstream NIM request failed: {last_exc}", "type": "upstream_error"}})


async def _sse_relay(resp: httpx.Response) -> AsyncIterator[bytes]:
    """Stream NIM's SSE response straight through to the client, byte for byte."""
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    finally:
        await resp.aclose()


def _error_response(exc: httpx.HTTPStatusError) -> JSONResponse:
    try:
        detail = exc.response.json()
    except Exception:
        detail = {"error": {"message": exc.response.text, "type": "upstream_error"}}
    return JSONResponse(status_code=exc.response.status_code, content=detail)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/health")
async def health() -> Dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "upstream": settings.nim_base_url,
        "default_model": settings.default_model,
        "nim_api_key_configured": bool(settings.nim_api_key),
    }


@app.get("/v1/models")
async def list_models(
    settings: Settings = None,  # type: ignore[assignment]
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    """Live catalog, proxied straight from NIM (includes free + partner models)."""
    settings = get_settings()
    _check_client_auth(settings, authorization, x_api_key)
    resp = await _forward("GET", "/models", settings)
    if resp.status_code >= 400:
        return _error_response(httpx.HTTPStatusError("error", request=resp.request, response=resp))
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.get("/free-models")
async def list_free_models(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    """Curated, hand-checked list of NVIDIA NIM's currently free-tier chat
    models this proxy ships pre-mapped, in OpenAI's /v1/models response shape
    (plus a `description` field). NVIDIA's free catalog changes over time —
    verify against https://build.nvidia.com/models?filters=free_endpoint if
    something here looks stale, and update FREE_NIM_MODELS in config.py.
    """
    settings = get_settings()
    _check_client_auth(settings, authorization, x_api_key)
    data = [
        {
            "id": model_id,
            "object": "model",
            "owned_by": model_id.split("/", 1)[0],
            "description": meta["description"],
            "category": meta["category"],
        }
        for model_id, meta in FREE_NIM_MODELS.items()
    ]
    return JSONResponse(content={"object": "list", "data": data})


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    settings = get_settings()
    _check_client_auth(settings, authorization, x_api_key)

    body = await request.json()
    payload = _translate_chat_request(body, settings)
    stream = bool(payload.get("stream", False))
    request_id = str(uuid.uuid4())
    logger.info("[%s] chat.completions model=%s stream=%s", request_id, payload["model"], stream)

    if stream:
        resp = await _forward("POST", "/chat/completions", settings, json_body=payload, stream=True)
        if resp.status_code >= 400:
            body_bytes = await resp.aread()
            await resp.aclose()
            try:
                detail = httpx.Response(resp.status_code, content=body_bytes, request=resp.request).json()
            except Exception:
                detail = {"error": {"message": body_bytes.decode(errors="replace"), "type": "upstream_error"}}
            return JSONResponse(status_code=resp.status_code, content=detail)
        return StreamingResponse(
            _sse_relay(resp),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Request-Id": request_id},
        )

    resp = await _forward("POST", "/chat/completions", settings, json_body=payload)
    if resp.status_code >= 400:
        return _error_response(httpx.HTTPStatusError("error", request=resp.request, response=resp))
    return JSONResponse(status_code=resp.status_code, content=resp.json(), headers={"X-Request-Id": request_id})


@app.post("/v1/completions")
async def completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    settings = get_settings()
    _check_client_auth(settings, authorization, x_api_key)

    body = await request.json()
    payload = _translate_chat_request(body, settings)  # same translation logic applies
    stream = bool(payload.get("stream", False))

    if stream:
        resp = await _forward("POST", "/completions", settings, json_body=payload, stream=True)
        if resp.status_code >= 400:
            body_bytes = await resp.aread()
            await resp.aclose()
            return JSONResponse(status_code=resp.status_code, content={"error": {"message": body_bytes.decode(errors="replace")}})
        return StreamingResponse(_sse_relay(resp), media_type="text/event-stream")

    resp = await _forward("POST", "/completions", settings, json_body=payload)
    if resp.status_code >= 400:
        return _error_response(httpx.HTTPStatusError("error", request=resp.request, response=resp))
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.post("/v1/embeddings")
async def embeddings(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    settings = get_settings()
    _check_client_auth(settings, authorization, x_api_key)

    body = await request.json()
    payload = dict(body)
    model = payload.get("model") or settings.default_model
    payload["model"] = resolve_model(model, settings)

    resp = await _forward("POST", "/embeddings", settings, json_body=payload)
    if resp.status_code >= 400:
        return _error_response(httpx.HTTPStatusError("error", request=resp.request, response=resp))
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error while proxying %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"message": str(exc), "type": "proxy_internal_error"}},
    )


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, log_level=settings.log_level)
