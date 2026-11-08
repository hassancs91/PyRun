"""A run's stdout and stderr, the way PyRun shows them (2.5 Stage 2).

The executor masks output once, when the run ends: every secret the run was
given plus the injected AI credential, the same set it injected
(``core.executor``). It stores the masked text, capped at ``MAX_OUTPUT_BYTES``
per stream with the start and the end kept. The console run page and the
``runs.output`` API endpoint both read output through this module, so the API
can never show different bytes than the console.

Output is written when the run ends, so a run that is still going has none yet.
"""

STREAMS = ("stdout", "stderr")
# The marker line ``core.executor._truncate_output`` puts where it cut.
TRUNCATED_MARKER = "[OUTPUT TRUNCATED"


def output_text(run, stream="stdout") -> str:
    """The stored (already masked) text of one stream; "" when there is none."""
    if stream not in STREAMS:
        raise ValueError(f"Unknown output stream: {stream!r}")
    return getattr(run, stream) or ""


def is_truncated(text) -> bool:
    """Whether the stored text had its middle cut at the size cap."""
    return TRUNCATED_MARKER in text


def tail(text, max_bytes) -> tuple[str, bool]:
    """The last ``max_bytes`` UTF-8 bytes of ``text``, never splitting a
    character (so it can come out a few bytes shorter): ``(text, cut)``."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[len(encoded) - max_bytes :].decode("utf-8", errors="ignore"), True
