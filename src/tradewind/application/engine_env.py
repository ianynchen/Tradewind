"""The auth environment scrub for engine processes
(docs/components/06-workspace-safety.md §4).

With a subscription profile, an engine process must not switch to an API
key or another provider through a variable in the caller's environment.
Each backend declares those variables (`Backend.auth_env_vars()`); this
helper decides which to remove for one profile.

Mechanism: neither SDK can delete a variable from the child environment
(Claude builds `{**os.environ, ..., **options.env}`; Codex does
`os.environ.copy()` then `update(config.env)`), so each removed variable
is passed as "". Both engines read "" as unset (task-4 report, Step 0):
- Claude CLI 2.1.258: the provider switches go through a truthy check
  (`if(!t)return!1`), the API key and token through truthy checks or the
  Anthropic client's `process.env[e]?.trim()||void 0`.
- Codex 0.147.0 (`codex-rs/login/src/auth/manager.rs`): all three
  variables are read through `.filter(|value| !value.is_empty())`.
Alternative considered: a wrapper executable that runs `env -u NAME ...`
(true removal). Rejected: it needs a generated file per turn, and for
Codex it changes the SDK's launch (a set `codex_bin` skips the bundled
PATH entry). Revisit if an engine starts to treat "" as a set value.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from tradewind.domain.models import SubscriptionAuth

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineEnv:
    """The scrub for one engine process.

    `removed` holds the names, sorted, of the auth variables the caller has
    set that the engine must not see; empty when nothing is removed.
    """

    removed: tuple[str, ...]

    @property
    def overrides(self) -> dict[str, str]:
        """The values to merge over the inherited environment (the SDK's
        `env` option): "" for each removed name, which the engine reads as
        unset. Empty when nothing is removed."""
        return dict.fromkeys(self.removed, "")


def engine_env(
    profile_auth: object, auth_vars: frozenset[str], base: Mapping[str, str]
) -> EngineEnv:
    """The scrub for an engine process started from `base` (the caller's
    environment, normally `os.environ`).

    A no-op unless `profile_auth` is `SubscriptionAuth` (D6: an API-key
    profile keeps its variables). Otherwise every name in `auth_vars` that
    is set in `base` is removed. Each removed name is logged at DEBUG,
    never its value. `base` is only read; `os.environ` is never changed.
    """
    if not isinstance(profile_auth, SubscriptionAuth):
        return EngineEnv(removed=())
    removed = tuple(sorted(name for name in auth_vars if name in base))
    for name in removed:
        _logger.debug("auth variable removed from the engine environment", extra={"env_var": name})
    return EngineEnv(removed=removed)
