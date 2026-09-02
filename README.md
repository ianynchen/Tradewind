# Tradewind

A Python library that drives four LLM agent backends — the Anthropic API (via
`langchain-anthropic`), the Claude Agent SDK, the OpenAI Codex SDK, and the Cursor SDK — behind
one port interface, with a durable, backend-neutral session store and honest, machine-readable
capability reporting. Embedded as a library (no daemon, no config files of its own); the caller
supplies a single config object.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — functional and non-functional requirements
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, session schema, key interactions
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — conformance evidence, live flakiness, operational notes
- [GUIDELINES.md](GUIDELINES.md) — working and delivery standards

## Install

```bash
uv add "tradewind @ git+https://example.com/tradewind.git"
# or
pip install "tradewind @ git+https://example.com/tradewind.git"
```

Requires Python ≥3.13. There is no PyPI release yet; install from the git source above (adjust
the URL to wherever this repo is hosted).

## Embedding example

```python
import uuid
from pathlib import Path

from tradewind import Tradewind, TradewindConfig
from tradewind.domain.models import SessionOptions

config = TradewindConfig.model_validate(
    {
        "profiles": {
            "default": {
                "backend": "langchain",
                "auth": {"kind": "api_key", "api_key": "sk-..."},
                "models": {"standard": {"model": "claude-sonnet-4-5"}},
            }
        },
        "default_profile": "default",
        "store": {"sqlite_path": Path("./sessions.db")},
    }
)
# Equivalently, build Profile/ModelSpec/ApiKeyAuth objects directly instead of a dict.

async def main() -> None:
    tw = Tradewind(config)
    session_id = str(uuid.uuid4())  # caller mints the id (FR-5.6); UUIDv7 recommended
    session = await tw.ensure(session_id, SessionOptions(system_prompt="Be concise."))
    result = await session.run("What's 2+2?")
    print(result.status, result.final_text)

    for msg in await tw.history(session_id):
        print(msg.role, msg.kind, msg.content)
```

`ensure` is get-or-create (idempotent); use `create`/`resume` for strict create-only/resume-only
intent (FR-5.6). `session.stream(prompt)` yields the normalized event stream directly instead of
collecting a `TurnResult`. See `docs/components/01-configuration-and-client.md` for the full
config surface (permission brokers, tools, MCP servers, event hooks).

## Capability matrix

Each backend declares what it actually supports (`Backend.capabilities()`); Tradewind never
emulates a capability silently — an unsupported call raises `Unsupported` (R-1, honest
capabilities over false uniformity). Read directly from the four adapters:

| Capability | langchain | claude | codex | cursor (EXPERIMENTAL) |
|---|---|---|---|---|
| `supports_system_prompt` | yes | yes | yes | no |
| `supports_structured_output` | no | no | yes | no |
| `supports_interactive_permissions` | yes | yes | yes | no |
| `supports_in_process_tools` | yes | yes | no | yes |
| `supports_native_resume` | no | yes | yes | yes |
| `supports_fork` | no | yes | yes | no |
| `supports_transcript_read` | no | yes | yes | no |

Notes:

- **langchain** is the only backend where Tradewind's own SQLite mirror is the system of
  record (FR-5.1); the other three treat it as a mirror of their native store.
- **codex** has no in-process tool bridge — every tool reaches it through the stdio MCP shim
  (`ToolHost.shim_server_def()`), never as a direct in-process call.
- **cursor** ships marked `EXPERIMENTAL` (module docstring: `EXPERIMENTAL — never
  live-verified (no Cursor subscription; P-5 open)`). It has full unit-test coverage and is
  wired end to end, but has never run against a live Cursor session — see
  [docs/RUNBOOK.md](docs/RUNBOOK.md#cursor--p-5-blocked-task-15). Cursor's missing
  `supports_system_prompt` is emulated one layer up (never inside the adapter, per R-1): the
  Turn Runner writes `.cursor/rules/tradewind-session.mdc` under the session's `cwd`, falling
  back to folding the instructions into the first prompt.

## Integration-test environment variables

None of these are read by the library itself — only by the test suite, and only when opted in:

| Variable | Enables |
|---|---|
| `TRADEWIND_RUN_CLAUDE_INTEGRATION=1` | Live conformance + smoke tests against a real Claude Code subscription. |
| `TRADEWIND_RUN_CODEX_INTEGRATION=1` | Live conformance against a real Codex/ChatGPT subscription. |
| `ANTHROPIC_API_KEY` | Live langchain tests against the real Anthropic API. |
| `GROQ_API_KEY` | Live langchain tests against the free-tier Groq API (`ChatGroq`). |

Cursor's live conformance suite (`tests/conformance/test_cursor.py`) has no env gate: it
self-skips unconditionally until a Cursor subscription exists and the skip is removed by hand.

## Development

```bash
uv sync
bash scripts/check.sh   # ruff format, ruff lint, mypy strict, import-linter, pytest
```

`scripts/check.sh` is hermetic — no network calls, no credentials required. See
[docs/RUNBOOK.md](docs/RUNBOOK.md) for the full conformance matrix (all four backends × seven
scenarios) and known live-run flakiness.
