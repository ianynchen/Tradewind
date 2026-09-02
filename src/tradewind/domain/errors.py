"""Domain errors: typed exceptions callers can branch on (GUIDELINES §9)."""


class TradewindError(Exception):
    """Base class for all Tradewind domain errors."""


class SessionExists(TradewindError):
    """`create()` was called with a session id that already exists."""


class SessionNotFound(TradewindError):
    """`resume()` was called with a session id that does not exist."""


class TurnInProgress(TradewindError):
    """A second `run()`/`stream()` was issued on a session with an unfinished
    turn (I-5: one in-flight turn per session)."""


class Unsupported(TradewindError):
    """An adapter was asked for a capability its flags deny (R-1)."""


class ToolMismatch(TradewindError):
    """Tools re-supplied at resume do not match the snapshot's tool names
    (I-2): the embedder must re-supply the same tools it snapshotted."""


class ConfigError(TradewindError):
    """Configuration failed validation."""


class TurnExecutionFailed(TradewindError):
    """`Session.run()` raises this when its turn's event stream ends in a
    `TurnFailed` event (task-9 brief): the message is that event's `error`
    string, verbatim."""
