# OpenAI → NVIDIA NIM Proxy (pre-wired to free models)

A drop-in proxy that lets you point any OpenAI SDK / client at NVIDIA NIM
(hosted at `build.nvidia.com` or a self-hosted NIM container). It exposes
`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, and `/v1/models`,
translates OpenAI model names to NIM model names, forwards the request, and
relays streaming (SSE) responses through unmodified.

It ships pre-configured with a curated set of NVIDIA NIM's currently **free**
models (no per-token charge, rate-limited), so `gpt-4o`, `gpt-4o-mini`, etc.
resolve to a real free NIM model with zero config beyond an API key.

## Why

- Keep your real NVIDIA API key server-side; issue your own proxy key to clients.
- Let existing code written against `openai` Python/JS SDKs work against NIM
  without changes — just swap `base_url`.
- One place to add logging, retries, and model-name aliasing.
- Works out of the box against free models, no need to look up model IDs first.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: set NIM_API_KEY (get one free, no credit card, at
# https://build.nvidia.com/settings/api-keys). MODEL_MAP_JSON can stay blank —
# it'll use the built-in free-model defaults.
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Free models included by default

Checked against `build.nvidia.com/models` (filtered to "Free Endpoint") on
2026-07-03. NVIDIA adds/renames/deprecates free models over time, so verify
against the live catalog — or hit `GET /free-models` on this proxy, which
serves this same list — before depending on any of these long-term.

| OpenAI alias | Resolves to (NIM model ID) | Notes |
|---|---|---|
| `gpt-4o` | `z-ai/glm-5.2` | Flagship, agentic/coding/reasoning |
| `gpt-4o-mini` | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | Omni-modal (text/image/video/speech) |
| `gpt-4-turbo` / `gpt-4.1` | `deepseek-ai/deepseek-v4-pro` | 1M-token context, strong at coding |
| `gpt-4.1-mini` | `mistralai/mistral-medium-3.5-128b` | General-purpose, coding, agentic |
| `gpt-3.5-turbo` | `stepfun-ai/step-3.7-flash` | Fast sparse-MoE reasoning |
| `o1` | `nvidia/nemotron-3-ultra-550b-a55b` | 1M context, planning/tool-calling |
| `o1-mini` | `moonshotai/kimi-k2.6` | 1T-param MoE, long-horizon agentic |

You can also send a native NIM model ID directly (e.g.
`minimaxai/minimax-m3`) — unmapped names pass straight through by default.
The full curated set lives in `FREE_NIM_MODELS` in `config.py`.

Free-tier NIM access is still gated by an API key and a shared rate limit
(roughly ~40 requests/min per account as of this writing) — "free" means no
per-token charge, not unauthenticated or unlimited.

## Usage

Point any OpenAI client at the proxy:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="whatever-you-set-as-PROXY_API_KEY-or-anything-if-unset",
)

resp = client.chat.completions.create(
    model="gpt-4o",  # translated to a free NIM model, e.g. z-ai/glm-5.2
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

Browse what's available:

```bash
curl http://localhost:8000/free-models   # curated free models this proxy knows about
curl http://localhost:8000/v1/models     # live catalog proxied straight from NIM
curl http://localhost:8000/health        # confirms upstream + default model + key presence
```

## Configuration (env vars)

| Variable | Description | Default |
|---|---|---|
| `NIM_BASE_URL` | Target NIM OpenAI-compatible base URL | `https://integrate.api.nvidia.com/v1` |
| `NIM_API_KEY` | NVIDIA API key sent upstream | *(none)* |
| `PROXY_API_KEY` | If set, clients must present this key to the proxy | *(open)* |
| `MODEL_MAP_JSON` | JSON dict mapping OpenAI model names → NIM model names | `{}` |
| `PASSTHROUGH_UNMAPPED_MODELS` | Forward unmapped model names as-is instead of erroring | `true` |
| `DEFAULT_MODEL` | Used when the client omits `model` | `meta/llama-3.1-8b-instruct` |
| `REQUEST_TIMEOUT_SECONDS` | Upstream request timeout | `120` |
| `MAX_RETRIES` | Retries on transport-level failure | `2` |

## Notes

- Streaming responses are relayed byte-for-byte from NIM's SSE stream, so no
  chunk-format translation is needed — NIM already emits OpenAI-shaped SSE chunks.
- A handful of OpenAI-only request fields (`service_tier`, `parallel_tool_calls`,
  `store`, `metadata`, `user`) are stripped before forwarding, since NIM/vLLM
  backends may reject unknown fields. Extend `_UNSUPPORTED_FIELDS` in `main.py`
  if you hit others.
- This is a minimal reference implementation — for production, add rate
  limiting, structured request logging/metrics, and TLS termination (or run
  behind a reverse proxy that provides it).
