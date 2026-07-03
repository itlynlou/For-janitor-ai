# NIM-to-OpenAI Proxy

An OpenAI-compatible API server that forwards requests to **NVIDIA NIM** —
either the NVIDIA cloud API (`integrate.api.nvidia.com`) or a self-hosted NIM
container — so any tool or SDK that speaks the OpenAI API can point at NIM
without modification.

## Features

- **OpenAI-compatible routes**: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`
- **Mixed backends**: route different model aliases to NVIDIA's cloud API *and* self-hosted NIM containers side by side
- **Model name mapping**: expose friendly aliases (`llama3-70b`) that map to real NIM model IDs (`meta/llama3-70b-instruct`)
- **Streaming (SSE) passthrough**: `stream: true` requests are proxied byte-for-byte to the client
- **Retries with backoff**: transient network errors, `429`, and `5xx` responses are retried with exponential backoff + jitter before any bytes reach the client
- **Multi-key / multi-user auth**: issue your own proxy API keys, each with an optional allow-list of models
- **Structured logging**: request/response logging via pino, including latency and token usage where available

## Quick start

```bash
npm install
cp .env.example .env
# edit .env: set NVIDIA_API_KEY (and NIM_KEY_LOCAL if a self-hosted container needs one)
npm start
```

The server listens on `PORT` (default `3000`).

## Configuration

Three JSON files in `config/` control routing; `.env` controls secrets and
runtime behavior.

### `config/backends.json` — named upstreams

```json
{
  "cloud": {
    "baseUrl": "https://integrate.api.nvidia.com/v1",
    "apiKeyEnv": "NVIDIA_API_KEY"
  },
  "local-llama": {
    "baseUrl": "http://localhost:8000/v1",
    "apiKeyEnv": "NIM_KEY_LOCAL"
  }
}
```

Add one entry per backend. `apiKeyEnv` names an environment variable holding
the key for that backend — leave it unset (empty string) for self-hosted NIM
containers that don't require auth, which is the common case.

### `config/models.json` — model aliases

```json
{
  "llama3-70b": { "backend": "cloud", "target": "meta/llama3-70b-instruct" },
  "llama3-8b-local": { "backend": "local-llama", "target": "meta/llama3-8b-instruct" }
}
```

Clients request `model: "llama3-70b"`; the proxy rewrites it to
`meta/llama3-70b-instruct` and routes to whichever backend you assigned. The
response's `model` field is rewritten back to the alias the client used, so
client-side logic never sees your internal routing.

**Unmapped models**: if a client requests a model not listed here, the proxy
falls back to `DEFAULT_BACKEND` / `DEFAULT_BASE_URL` from `.env` and passes
the model name through unchanged — handy for self-hosted setups where you
don't want to register every model up front.

### `config/clients.json` — proxy-facing API keys

```json
[
  { "apiKey": "sk-proxy-demo-000000000000000000000000", "name": "demo-user", "allowedModels": ["*"] },
  { "apiKey": "sk-proxy-limited-11111111111111111111111", "name": "limited-user", "allowedModels": ["llama3-8b-local"] }
]
```

These are the keys *your* clients use against the proxy (`Authorization:
Bearer sk-proxy-...`) — separate from the upstream NIM keys in `.env`.
`allowedModels: ["*"]` permits any registered alias; otherwise list specific
aliases. Generate real keys with something like:

```bash
node -e "console.log('sk-proxy-' + require('crypto').randomBytes(24).toString('hex'))"
```

### `.env` — secrets and runtime tuning

See `.env.example` for the full list: server port, log level/format, default
fallback backend, retry attempts/backoff, and upstream timeout.

## Usage examples

Point any OpenAI SDK at the proxy's base URL and use one of your proxy keys:

```bash
curl http://localhost:3000/v1/chat/completions \
  -H "Authorization: Bearer sk-proxy-demo-000000000000000000000000" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

Streaming:

```bash
curl -N http://localhost:3000/v1/chat/completions \
  -H "Authorization: Bearer sk-proxy-demo-000000000000000000000000" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [{"role": "user", "content": "Count to 5"}],
    "stream": true
  }'
```

Python (`openai` SDK):

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:3000/v1",
    api_key="sk-proxy-demo-000000000000000000000000",
)

resp = client.chat.completions.create(
    model="llama3-8b-local",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

## How requests flow

1. `authenticate` middleware validates the proxy API key against `config/clients.json`.
2. `authorizeModel` middleware checks the client's `allowedModels` against the requested model.
3. `modelRouter.resolveModel` looks up the model alias in `config/models.json`, resolving a backend + real upstream model name (or falls back to the default backend for unmapped models).
4. `retryFetch` sends the request upstream, retrying on network failure / `429` / `5xx` with exponential backoff — retries only ever happen before any response bytes reach the client.
5. Non-streaming responses are parsed, logged (including `usage` token counts when the upstream returns them), have their `model` field rewritten back to the client-facing alias, and returned as JSON.
6. Streaming responses are piped through as raw SSE once the upstream connection is established successfully.

## Notes & limitations

- Self-hosted NIM containers already speak the OpenAI schema for most endpoints, so this proxy mostly adds routing, auth, retries, and logging on top — it does not reformat request/response bodies beyond the `model` field.
- Streaming requests are retried only while establishing the connection; once bytes start flowing, a mid-stream failure ends the client's stream rather than silently retrying (retrying a partially-consumed stream would risk duplicate output).
- Token-usage logging for streaming responses is best-effort: it depends on whether the upstream NIM model includes a `usage` field in its final SSE chunk (pass `stream_options: {"include_usage": true}` in the request body if the backend supports it).
