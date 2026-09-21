"""Retriever - surfaces kennel context into the system prompt.

Fires on ``load_prompt``. The actual packing logic lives in ``packer.py``;
this module is a thin wrapper that enforces the enable/disable toggle and
swallows any storage-layer hiccups so the host app never breaks.
"""

from __future__ import annotations

from . import packer
from .state import is_enabled

_EMPTY_RETURN = None  # Returning None tells the callback system "skip me."


def build_recall_block() -> str | None:
    """Return a system-prompt fragment built by the tiered packer.

    Returns ``None`` when the kennel is disabled, empty, or otherwise has
    nothing worth surfacing this turn.
    """
    if not is_enabled():
        return _EMPTY_RETURN
    try:
        return packer.pack()
    except packer.KennelBudgetError as exc:
        # A misconfigured budget must be LOUD, not silently truncated. Surface a
        # clear warning to the operator, but still never break the host: emit no
        # recall block this turn rather than shipping sliced doctrine.
        try:
            from code_puppy.messaging.bus import emit_warning

            emit_warning(f"[puppy_kennel] recall block skipped: {exc}")
        except Exception:
            pass
        return _EMPTY_RETURN
    except Exception:
        # Storage isn't ready yet, or the kennel is unhappy. Stay quiet.
        return _EMPTY_RETURN
