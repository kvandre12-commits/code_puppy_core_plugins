"""Tiered budget-aware packing for the system-prompt recall block.

Three priority classes, fed from the kennel, sized to fit a configurable
token budget without ever calling an LLM or shipping a tokenizer:

* **P0 - user preferences**: every drawer in ``user:default``. Short,
  durable, pervasive ("Mike hates emojis"). Capped at ~30% of budget.
* **P1 - sticky repo notes**: drawers in ``repo:<cwd>`` with ``role='note'``,
  i.e. content written via the ``kennel_remember`` tool. Highest signal-to-
  token ratio in the kennel. Capped at ~30% of budget.
* **P2 - recent assistant responses**: drawers in ``repo:<cwd>`` with
  ``role='assistant'``. Fills whatever budget remains after P0 + P1.

Token budget is enforced via the well-known 1-token approximate-4-chars
heuristic. Cheap, zero-dep, accurate to plus-or-minus 20% which is fine
for "do not blow the context."
"""

from __future__ import annotations

from dataclasses import dataclass

from . import kennel
from .config import (
    CAPSULE_BUDGET_CHARS,
    CAPSULE_ROOM,
    CHARS_PER_TOKEN,
    MAX_WING_DISPLAY_CHARS,
    MIN_DRAWER_CHARS,
    PROMPT_BUDGET_CHARS,
    PROMPT_BUDGET_TOKENS,
    STICKY_QUOTA,
    USER_PREFS_QUOTA,
)
from .kennel import Drawer
from .wings import USER_WING, detect_cwd, repo_wing


class KennelBudgetError(ValueError):
    """Raised when the configured protected-tier budgets plus bounded framing
    cannot fit within ``PROMPT_BUDGET_CHARS``.

    We raise instead of silently truncating protected content (capsule / P0 /
    P1): a memory system must never claim "guaranteed doctrine" while quietly
    slicing it. The caller (``retriever.build_recall_block``) turns this into a
    visible warning and emits no block.
    """


# Section titles in render order. Used to compute a deterministic upper bound on
# structural framing (headings + separators) for budget validation.
_SECTION_TITLES = (
    "Project Doctrine Capsule",
    "User Preferences",
    "Project Decisions",
    "Recent Context",
)

# Reserve ~50 tokens for rendered headers and section dividers.
_HEADER_SLACK_CHARS = 50 * CHARS_PER_TOKEN

# We over-fetch from SQLite then truncate to fit. Cheap, simple.
_FETCH_LIMIT = 50

# Below this remainder, a new drawer would be mostly "...truncated]".
_MIN_REMAINING_CHARS = 120


@dataclass(slots=True)
class PackSection:
    title: str
    lines: list[str]
    used_chars: int


def _agent_label(d: Drawer) -> str:
    meta = d.metadata or {}
    return str(meta.get("agent") or d.role or "?")


def _format_drawer(d: Drawer, max_chars: int) -> str:
    """Render one drawer to a markdown bullet, fitting within ``max_chars``.

    The bullet looks like ``- [ts] _agent_ : content...`` with the content
    truncated as needed. Returns an empty string if the bullet skeleton
    alone wouldn't fit.
    """
    head = f"- [{d.ts}] _{_agent_label(d)}_ : "
    if max_chars <= len(head) + 20:
        return ""
    body_budget = max_chars - len(head)
    body = d.content.strip().replace("\n", " ")
    if len(body) > body_budget:
        body = body[: body_budget - 1].rstrip() + "..."
    return head + body


def _pack_class(
    drawers: list[Drawer],
    budget_chars: int,
    min_chars: int = MIN_DRAWER_CHARS,
) -> PackSection:
    """Greedily pack drawers into ``budget_chars``, newest first.

    Skips drawers smaller than ``min_chars`` (probably noise). Truncates
    the last drawer if it would otherwise push us over. Stops once the
    remaining budget gets too small to be useful.
    """
    lines: list[str] = []
    used = 0
    for d in drawers:
        if len(d.content.strip()) < min_chars:
            continue
        remaining = budget_chars - used
        if remaining < _MIN_REMAINING_CHARS:
            break
        rendered = _format_drawer(d, max_chars=remaining)
        if not rendered:
            break
        lines.append(rendered)
        used += len(rendered) + 1  # +1 for the newline that joins them
    return PackSection(title="", lines=lines, used_chars=used)


def pack(cwd_override: str | None = None) -> str | None:
    """Build the system-prompt recall block under the configured budget.

    Returns ``None`` when there is nothing useful to surface (empty kennel,
    every drawer too short, etc.) - the ``load_prompt`` callback contract
    interprets ``None`` as "skip me".
    """
    cwd = cwd_override if cwd_override is not None else detect_cwd()
    repo_w = repo_wing(cwd)

    total_budget = max(0, PROMPT_BUDGET_CHARS - _HEADER_SLACK_CHARS)
    capsule_budget = min(CAPSULE_BUDGET_CHARS, total_budget)
    p0_budget = int(total_budget * USER_PREFS_QUOTA)
    p1_budget = int(total_budget * STICKY_QUOTA)

    # Deterministic config validation. The protected tiers (capsule + P0 + P1)
    # plus the bounded structural framing must fit within the hard ceiling. If
    # they cannot, fail loudly rather than ever slicing protected content.
    framing_bound = _max_framing_chars()
    protected_required = capsule_budget + p0_budget + p1_budget + framing_bound
    if protected_required > PROMPT_BUDGET_CHARS:
        raise KennelBudgetError(
            "puppy_kennel budget misconfiguration: capsule("
            f"{capsule_budget}) + P0({p0_budget}) + P1({p1_budget}) + framing("
            f"{framing_bound}) = {protected_required} exceeds PROMPT_BUDGET_CHARS("
            f"{PROMPT_BUDGET_CHARS}). Lower PUPPY_KENNEL_CAPSULE_BUDGET / quotas, "
            "shorten PUPPY_KENNEL_MAX_WING_DISPLAY, or raise PUPPY_KENNEL_PROMPT_BUDGET."
        )

    # Capsule tier (guaranteed, repo-scoped): pack the single designated
    # capsule FIRST, within its own bounded budget, so newest-first P1 notes
    # can never displace or truncate it. Its cost is charged to the P2
    # remainder below; P0 and P1 budgets are preserved exactly.
    capsule = kennel.capsule_drawer(repo_w) if capsule_budget > 0 else None
    cap_section: PackSection | None = None
    if capsule is not None:
        cap_section = _pack_class([capsule], capsule_budget, min_chars=0)
        cap_section.title = "Project Doctrine Capsule"

    # P0: user preferences; include every role because user notes may be assistant-authored.
    user_drawers = kennel.recent_drawers(USER_WING, limit=_FETCH_LIMIT)
    p0 = _pack_class(user_drawers, p0_budget)
    p0.title = "User Preferences"

    # P1: repo sticky notes (role='note' only), excluding the capsule room so
    # the designated capsule never renders twice.
    sticky = kennel.recent_drawers(
        repo_w, limit=_FETCH_LIMIT, role="note", exclude_room=CAPSULE_ROOM
    )
    p1 = _pack_class(sticky, p1_budget)
    p1.title = "Project Decisions"

    # P2: recent assistant responses fill whatever the hard ceiling leaves
    # after bounded framing + capsule + P0 + P1. Using the framing UPPER BOUND
    # here makes the fully serialized block <= PROMPT_BUDGET_CHARS by
    # construction, so protected content is never at risk of truncation.
    cap_used = cap_section.used_chars if cap_section else 0
    p2_budget = (
        PROMPT_BUDGET_CHARS - framing_bound - cap_used - p0.used_chars - p1.used_chars
    )
    assistant = kennel.recent_drawers(repo_w, limit=_FETCH_LIMIT, role="assistant")
    p2 = _pack_class(assistant, max(0, p2_budget))
    p2.title = "Recent Context"

    sections = [s for s in (cap_section, p0, p1, p2) if s and s.lines]
    if not sections:
        return None

    return _enforce_total_ceiling(sections, repo_w, p2)


def _abbrev_wing(wing: str, limit: int) -> str:
    """Deterministically bound the DISPLAYED wing string to ``limit`` chars.

    Only the header display is abbreviated (head + ``...`` + tail); the full
    wing string is always used for storage/retrieval, so abbreviation never
    affects which memory loads. Bounding this is what lets us give the framing
    a hard upper bound and therefore guarantee the total ceiling.
    """
    if limit <= 0 or len(wing) <= limit:
        return wing
    if limit <= 3:
        return wing[:limit]
    keep = limit - 3
    head = keep // 2
    tail = keep - head
    return wing[:head] + "..." + wing[len(wing) - tail :]


def _max_framing_chars() -> int:
    """Deterministic UPPER BOUND on non-content framing in the serialized block.

    Covers the title line, the wing header (with the wing display bounded to
    ``MAX_WING_DISPLAY_CHARS``), the leading blank, and — assuming every tier
    is present — each ``### heading`` plus its trailing blank separator. Each
    element is counted with its joining newline. Used both to validate the
    config and to size P2 so the full block fits by construction.
    """
    title_line = len("## Puppy Kennel - Memory") + 1
    wing_fixed = len(
        f"_Repo wing: `` | token budget: {PROMPT_BUDGET_TOKENS} "
        f"(~{PROMPT_BUDGET_CHARS} chars)_"
    )
    wing_line = wing_fixed + MAX_WING_DISPLAY_CHARS + 1
    leading_blank = 1
    section_frames = sum(len(f"### {t}") + 1 + 1 for t in _SECTION_TITLES)
    return title_line + wing_line + leading_blank + section_frames


def _enforce_total_ceiling(
    sections: list[PackSection], repo_w: str, flex: PackSection
) -> str:
    """Guarantee the fully serialized block never exceeds ``PROMPT_BUDGET_CHARS``
    WITHOUT ever cutting protected content.

    P2 was already sized against the framing upper bound, so a valid config
    fits by construction. This is a structure-aware safety net for the small
    ``+1``-per-line accounting slack: it sheds whole trailing lines from the
    flex tier (P2 / Recent Context) only — never the capsule, P0 or P1, and
    never a mid-line slice. If the block is still over after P2 is empty, the
    protected tiers plus framing genuinely do not fit and we raise rather than
    silently truncate guaranteed doctrine (should be unreachable given the
    config validation in ``pack``).
    """
    block = _render([s for s in sections if s.lines], repo_w)
    if len(block) <= PROMPT_BUDGET_CHARS:
        return block
    while flex.lines:
        flex.lines.pop()
        block = _render([s for s in sections if s.lines], repo_w)
        if len(block) <= PROMPT_BUDGET_CHARS:
            return block
    raise KennelBudgetError(
        "puppy_kennel: protected tiers (capsule + P0 + P1) plus framing render "
        f"to {len(block)} chars, exceeding PROMPT_BUDGET_CHARS({PROMPT_BUDGET_CHARS}) "
        "even with P2 emptied. Refusing to truncate protected content."
    )


def _render(sections: list[PackSection], repo_w: str) -> str:
    """Render the packed sections into the final markdown block.

    The wing path is abbreviated for DISPLAY only (see ``_abbrev_wing``); the
    full wing drives storage/retrieval elsewhere.
    """
    wing_display = _abbrev_wing(repo_w, MAX_WING_DISPLAY_CHARS)
    out: list[str] = [
        "## Puppy Kennel - Memory",
        (
            f"_Repo wing: `{wing_display}` | token budget: "
            f"{PROMPT_BUDGET_TOKENS} (~{PROMPT_BUDGET_CHARS} chars)_"
        ),
        "",
    ]
    for s in sections:
        out.append(f"### {s.title}")
        out.extend(s.lines)
        out.append("")
    return "\n".join(out)
