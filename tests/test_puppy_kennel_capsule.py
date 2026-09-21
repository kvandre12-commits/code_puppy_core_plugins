"""Tests for the Project Doctrine Capsule tier (Plan B).

Proves the guaranteed, repo-scoped capsule:
  * loads from the repo root, a nested subdirectory, and a linked worktree
    (all resolving to the same git-root wing);
  * survives competing newer P1 notes (cannot be displaced/truncated by them);
  * truncates deterministically to its bounded budget;
  * never leaks across repositories;
  * preserves the existing global P0 user-preference behavior and is packed
    before ordinary P1 notes;
  * is de-duplicated out of the ordinary P1 note output.

All tests run against a throwaway kennel (PUPPY_KENNEL_ROOT in a tmp dir) and
synthetic git repos; no real kennel data is touched.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

CAPSULE_MARKER = "RESTART-CAPSULE v-test"
CAPSULE_TAIL = "END-OF-CAPSULE-SENTINEL"
CAPSULE_TEXT = (
    f"{CAPSULE_MARKER} :: distilled doctrine for this repo. "
    "Identity, doctrine, architecture, state, next-steps all live here so the "
    "agent opens its eyes already oriented. " + ("doctrine " * 40) + CAPSULE_TAIL
)


@pytest.fixture
def kroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated, enabled kennel with config/state/kennel/packer reloaded so the
    capsule-tier constants and env overrides take effect per test."""
    root = tmp_path / "kennel"
    monkeypatch.setenv("PUPPY_KENNEL_ROOT", str(root))

    from code_puppy_core_plugins.puppy_kennel import config, kennel, packer, state

    importlib.reload(config)
    importlib.reload(state)
    importlib.reload(kennel)
    importlib.reload(packer)
    kennel.initialize()
    state.set_enabled(True)
    return {"root": root, "config": config, "kennel": kennel, "packer": packer}


# --------------------------------------------------------------------------- #
# Synthetic git topology helpers
# --------------------------------------------------------------------------- #
def _make_repo(base: Path, name: str) -> Path:
    root = base / name
    (root / ".git").mkdir(parents=True)
    return root.resolve()


def _make_worktree(base: Path, main_root: Path, wt_name: str) -> Path:
    """Create a linked worktree of ``main_root`` (a ``.git`` FILE pointing into
    ``<main>/.git/worktrees/<wt_name>``), exactly like real git worktrees."""
    admin = main_root / ".git" / "worktrees" / wt_name
    admin.mkdir(parents=True, exist_ok=True)
    wt_dir = base / wt_name
    wt_dir.mkdir(parents=True, exist_ok=True)
    (wt_dir / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return wt_dir.resolve()


# --------------------------------------------------------------------------- #
# 1. Root / nested / worktree loading
# --------------------------------------------------------------------------- #
def test_capsule_loads_root_nested_worktree(kroot, tmp_path: Path) -> None:
    kennel = kroot["kennel"]
    packer = kroot["packer"]
    from code_puppy_core_plugins.puppy_kennel import wings

    main = _make_repo(tmp_path / "repos", "projA")
    nested = main / "pkg" / "deep"
    nested.mkdir(parents=True)
    worktree = _make_worktree(tmp_path / "repos", main, "projA-wt")

    wing = wings.repo_wing(main)
    kennel.write_capsule(wing, CAPSULE_TEXT)

    for label, cwd in [("root", main), ("nested", nested), ("worktree", worktree)]:
        assert wings.repo_wing(cwd) == wing, f"{label}: wing mismatch"
        block = packer.pack(cwd_override=str(cwd)) or ""
        assert "### Project Doctrine Capsule" in block, f"{label}: no capsule section"
        assert CAPSULE_MARKER in block, f"{label}: capsule head missing"


# --------------------------------------------------------------------------- #
# 2. Survival under competing newer P1 notes
# --------------------------------------------------------------------------- #
def test_capsule_survives_newer_notes(kroot, tmp_path: Path) -> None:
    kennel = kroot["kennel"]
    packer = kroot["packer"]
    from code_puppy_core_plugins.puppy_kennel import wings

    main = _make_repo(tmp_path / "repos", "projB")
    wing = wings.repo_wing(main)
    kennel.write_capsule(wing, CAPSULE_TEXT)

    # Flood the wing with NEWER ordinary notes (newest-first P1 would evict a
    # plain note capsule). Each comfortably exceeds MIN_DRAWER_CHARS.
    for i in range(25):
        kennel.write_note(
            wing,
            room_name="notes",
            content=f"Competing sticky note #{i} " + ("x" * 120),
            role="note",
        )

    block = packer.pack(cwd_override=str(main)) or ""
    assert "### Project Doctrine Capsule" in block
    assert CAPSULE_MARKER in block
    # Full capsule intact (tail sentinel present) despite budget contention.
    assert CAPSULE_TAIL in block, "capsule was truncated/displaced by newer notes"


# --------------------------------------------------------------------------- #
# 3. Deterministic truncation to bounded budget
# --------------------------------------------------------------------------- #
def test_capsule_deterministic_truncation(kroot, tmp_path: Path, monkeypatch) -> None:
    kennel = kroot["kennel"]
    packer = kroot["packer"]
    from code_puppy_core_plugins.puppy_kennel import wings

    # Shrink the capsule budget so a long capsule must truncate.
    monkeypatch.setattr(packer, "CAPSULE_BUDGET_CHARS", 240, raising=True)

    main = _make_repo(tmp_path / "repos", "projC")
    wing = wings.repo_wing(main)
    long_capsule = CAPSULE_MARKER + " " + ("y" * 4000) + CAPSULE_TAIL
    kennel.write_capsule(wing, long_capsule)

    block1 = packer.pack(cwd_override=str(main)) or ""
    block2 = packer.pack(cwd_override=str(main)) or ""

    assert block1 == block2, "capsule packing must be deterministic"
    assert CAPSULE_MARKER in block1  # head survives
    assert CAPSULE_TAIL not in block1  # tail truncated under tight budget
    # Extract the capsule bullet line and assert it is bounded by the budget.
    cap_line = next(
        ln for ln in block1.splitlines() if ln.startswith("- [") and CAPSULE_MARKER in ln
    )
    assert len(cap_line) <= 240 + 60, "capsule exceeded its bounded budget"
    assert cap_line.rstrip().endswith("..."), "truncation marker missing"


# --------------------------------------------------------------------------- #
# 4. No cross-repository leakage
# --------------------------------------------------------------------------- #
def test_no_cross_repo_leakage(kroot, tmp_path: Path) -> None:
    kennel = kroot["kennel"]
    packer = kroot["packer"]
    from code_puppy_core_plugins.puppy_kennel import wings

    repo_a = _make_repo(tmp_path / "repos", "alpha")
    repo_b = _make_repo(tmp_path / "repos", "beta")
    wing_a = wings.repo_wing(repo_a)
    wing_b = wings.repo_wing(repo_b)
    assert wing_a != wing_b

    kennel.write_capsule(wing_a, CAPSULE_TEXT)

    block_a = packer.pack(cwd_override=str(repo_a)) or ""
    block_b = packer.pack(cwd_override=str(repo_b)) or ""

    assert CAPSULE_MARKER in block_a
    assert CAPSULE_MARKER not in block_b, "capsule leaked into a different repo"
    assert kennel.capsule_drawer(wing_b) is None
    assert kennel.capsule_drawer(wing_a) is not None


# --------------------------------------------------------------------------- #
# 5. P0 preserved + capsule packed before P1 + de-duplicated from P1
# --------------------------------------------------------------------------- #
def test_p0_preserved_capsule_first_and_deduped(kroot, tmp_path: Path) -> None:
    kennel = kroot["kennel"]
    packer = kroot["packer"]
    from code_puppy_core_plugins.puppy_kennel import wings
    from code_puppy_core_plugins.puppy_kennel.wings import USER_WING

    main = _make_repo(tmp_path / "repos", "projD")
    wing = wings.repo_wing(main)

    pref = "USER-PREF-SENTINEL: Kurtis prefers legible seam-fixes. " + ("p" * 60)
    kennel.write_note(USER_WING, room_name="notes", content=pref, role="note")
    kennel.write_capsule(wing, CAPSULE_TEXT)
    # A normal P1 note that should still render under Project Decisions.
    kennel.write_note(
        wing, room_name="notes", content="P1-NOTE-SENTINEL " + ("q" * 90), role="note"
    )

    block = packer.pack(cwd_override=str(main)) or ""

    # P0 preserved (global user preference still surfaces).
    assert "### User Preferences" in block
    assert "USER-PREF-SENTINEL" in block
    # Capsule present and packed BEFORE ordinary P1 notes.
    i_cap = block.index("### Project Doctrine Capsule")
    i_p1 = block.index("### Project Decisions")
    assert i_cap < i_p1, "capsule must be packed before P1 notes"
    # De-duplication: the capsule text appears exactly once (capsule section
    # only), never echoed inside the P1 note output.
    assert block.count(CAPSULE_MARKER) == 1
    # Ordinary P1 note still shows.
    assert "P1-NOTE-SENTINEL" in block


# --------------------------------------------------------------------------- #
# 6. Hard ceiling: fully serialized block never exceeds the total char budget
# --------------------------------------------------------------------------- #
def test_serialized_block_never_exceeds_total_budget(kroot, tmp_path: Path) -> None:
    config = kroot['config']
    kennel = kroot['kennel']
    packer = kroot['packer']
    from code_puppy_core_plugins.puppy_kennel import wings
    from code_puppy_core_plugins.puppy_kennel.wings import USER_WING

    # Deliberately long, deeply-nested repo path -> a long wing header line,
    # maximizing framing overhead alongside maximally filled tiers.
    long_name = 'repo_' + ('d' * 40)
    main = _make_repo(tmp_path / 'deep' / ('n' * 30) / ('m' * 30), long_name)
    wing = wings.repo_wing(main)

    # Capsule larger than its budget -> forces capsule-tier truncation too.
    kennel.write_capsule(wing, CAPSULE_MARKER + ' ' + ('z' * 6000) + CAPSULE_TAIL)
    # Saturate P0, P1, P2 well beyond their budgets.
    for i in range(60):
        kennel.write_note(USER_WING, 'notes', f'pref {i} ' + ('p' * 200), role='note')
    for i in range(60):
        kennel.write_note(wing, 'notes', f'note {i} ' + ('q' * 200), role='note')
    for i in range(60):
        kennel.write_note(wing, 'sess', f'turn {i} ' + ('a' * 200), role='assistant')

    block1 = packer.pack(cwd_override=str(main)) or ''
    block2 = packer.pack(cwd_override=str(main)) or ''

    # Hard ceiling on the ENTIRE serialized block (headings, separators,
    # truncation markers, capsule framing all included).
    assert len(block1) <= config.PROMPT_BUDGET_CHARS, (
        f'block {len(block1)} > ceiling {config.PROMPT_BUDGET_CHARS}'
    )
    assert block1 == block2, 'ceiling enforcement must be deterministic'
    # Guarantees preserved even under maximum pressure: capsule + P0 + P1 stay.
    assert '### Project Doctrine Capsule' in block1
    assert CAPSULE_MARKER in block1
    assert '### User Preferences' in block1
    assert '### Project Decisions' in block1


def test_ceiling_holds_across_valid_capsule_budgets(kroot, tmp_path: Path, monkeypatch) -> None:
    """The ceiling must hold for every VALID (bounded) capsule budget."""
    config = kroot['config']
    kennel = kroot['kennel']
    packer = kroot['packer']
    from code_puppy_core_plugins.puppy_kennel import wings
    from code_puppy_core_plugins.puppy_kennel.wings import USER_WING

    main = _make_repo(tmp_path / 'repos', 'projF')
    wing = wings.repo_wing(main)
    kennel.write_capsule(wing, CAPSULE_MARKER + ' ' + ('z' * 5000) + CAPSULE_TAIL)
    for i in range(60):
        kennel.write_note(USER_WING, 'notes', f'pref {i} ' + ('p' * 200), role='note')
    for i in range(60):
        kennel.write_note(wing, 'notes', f'note {i} ' + ('q' * 200), role='note')
    for i in range(60):
        kennel.write_note(wing, 'sess', f'turn {i} ' + ('a' * 200), role='assistant')

    for cap_budget in (200, 800, 2000):  # all valid: cap+P0+P1+framing <= ceiling
        monkeypatch.setattr(packer, 'CAPSULE_BUDGET_CHARS', cap_budget, raising=True)
        block = packer.pack(cwd_override=str(main)) or ''
        assert len(block) <= config.PROMPT_BUDGET_CHARS, (
            f'cap_budget={cap_budget}: block {len(block)} > {config.PROMPT_BUDGET_CHARS}'
        )


# --------------------------------------------------------------------------- #
# 7. Extreme wing path is bounded/abbreviated (framing stays bounded)
# --------------------------------------------------------------------------- #
def test_extreme_wing_path_abbreviated(kroot, tmp_path: Path) -> None:
    config = kroot['config']
    kennel = kroot['kennel']
    packer = kroot['packer']
    from code_puppy_core_plugins.puppy_kennel import wings

    # Build a repo at an absurdly long, deeply-nested path.
    deep = tmp_path
    for seg in range(8):
        deep = deep / (f'segment_{seg}_' + ('x' * 25))
    main = _make_repo(deep, 'repo_' + ('y' * 40))
    wing = wings.repo_wing(main)
    assert len(wing) > config.MAX_WING_DISPLAY_CHARS  # precondition: needs abbreviating

    kennel.write_capsule(wing, CAPSULE_TEXT)
    block = packer.pack(cwd_override=str(main)) or ''

    # Displayed wing (between backticks on the header line) is bounded.
    wing_line = next(ln for ln in block.splitlines() if ln.startswith('_Repo wing:'))
    shown = wing_line.split('`')[1]
    assert len(shown) <= config.MAX_WING_DISPLAY_CHARS
    assert '...' in shown  # was abbreviated
    # Block within ceiling despite the pathological path.
    assert len(block) <= config.PROMPT_BUDGET_CHARS
    # Full wing still used for retrieval -> capsule loads regardless of display.
    assert CAPSULE_MARKER in block
    assert kennel.capsule_drawer(wing) is not None


# --------------------------------------------------------------------------- #
# 8. P2 exhaustion never alters capsule/P0/P1 bytes; output stays structural
# --------------------------------------------------------------------------- #
def _protected_prefix(block: str) -> str:
    """Everything before the Recent Context (P2) section — the protected part."""
    marker = '### Recent Context'
    return block.split(marker)[0]


def test_p2_exhaustion_preserves_protected_bytes(kroot, tmp_path: Path) -> None:
    config = kroot['config']
    kennel = kroot['kennel']
    packer = kroot['packer']
    from code_puppy_core_plugins.puppy_kennel import wings
    from code_puppy_core_plugins.puppy_kennel.wings import USER_WING

    main = _make_repo(tmp_path / 'repos', 'projG')
    wing = wings.repo_wing(main)

    # Byte-complete protected content (each item well under its tier budget).
    cap = CAPSULE_MARKER + ' distilled doctrine kept whole. ' + CAPSULE_TAIL
    kennel.write_capsule(wing, cap)
    prefs = [f'PREF-{i}-SENTINEL ' + ('p' * 90) for i in range(3)]
    notes = [f'NOTE-{i}-SENTINEL ' + ('q' * 90) for i in range(3)]
    for p in prefs:
        kennel.write_note(USER_WING, 'notes', p, role='note')
    for n in notes:
        kennel.write_note(wing, 'notes', n, role='note')

    # Baseline: NO P2 content at all.
    baseline = packer.pack(cwd_override=str(main)) or ''
    base_protected = _protected_prefix(baseline)

    # Now flood P2 with huge assistant turns to force shedding under the ceiling.
    for i in range(80):
        kennel.write_note(wing, 'sess', f'turn {i} ' + ('a' * 300), role='assistant')
    pressured = packer.pack(cwd_override=str(main)) or ''

    # Ceiling respected.
    assert len(pressured) <= config.PROMPT_BUDGET_CHARS
    # Protected prefix is byte-identical to the no-P2 baseline (ignoring only
    # the trailing section separator): shedding P2 NEVER altered capsule/P0/P1
    # bytes.
    assert _protected_prefix(pressured).rstrip() == base_protected.rstrip()
    # And every protected item is present verbatim (byte-complete, no '...').
    assert cap in pressured
    for p in prefs:
        assert p in pressured
    for n in notes:
        assert n in pressured


def test_output_structurally_complete_under_pressure(kroot, tmp_path: Path) -> None:
    config = kroot['config']
    kennel = kroot['kennel']
    packer = kroot['packer']
    from code_puppy_core_plugins.puppy_kennel import wings
    from code_puppy_core_plugins.puppy_kennel.wings import USER_WING

    main = _make_repo(tmp_path / 'repos', 'projH')
    wing = wings.repo_wing(main)
    kennel.write_capsule(wing, CAPSULE_TEXT)
    for i in range(60):
        kennel.write_note(USER_WING, 'notes', f'pref {i} ' + ('p' * 160), role='note')
    for i in range(60):
        kennel.write_note(wing, 'notes', f'note {i} ' + ('q' * 160), role='note')
    for i in range(60):
        kennel.write_note(wing, 'sess', f'turn {i} ' + ('a' * 200), role='assistant')

    block = packer.pack(cwd_override=str(main)) or ''
    lines = block.splitlines()
    assert lines[0] == '## Puppy Kennel - Memory'
    assert block.endswith('\n')  # clean terminal newline, not a mid-line slice
    # No dangling heading: every '### ' section is followed by >=1 content bullet.
    for idx, ln in enumerate(lines):
        if ln.startswith('### '):
            assert idx + 1 < len(lines) and lines[idx + 1].startswith('- ['), (
                f'section {ln!r} has no content line'
            )


# --------------------------------------------------------------------------- #
# 9. Impossible budget configs fail explicitly (never silently slice)
# --------------------------------------------------------------------------- #
def test_impossible_capsule_budget_raises(kroot, tmp_path: Path, monkeypatch) -> None:
    packer = kroot['packer']
    from code_puppy_core_plugins.puppy_kennel import wings

    main = _make_repo(tmp_path / 'repos', 'projI')
    wing = wings.repo_wing(main)
    kennel = kroot['kennel']
    kennel.write_capsule(wing, CAPSULE_TEXT)

    # Capsule budget so large that capsule+P0+P1+framing cannot fit the ceiling.
    monkeypatch.setattr(packer, 'CAPSULE_BUDGET_CHARS', 10_000, raising=True)
    with pytest.raises(packer.KennelBudgetError):
        packer.pack(cwd_override=str(main))


def test_impossible_total_budget_raises(kroot, tmp_path: Path, monkeypatch) -> None:
    packer = kroot['packer']
    from code_puppy_core_plugins.puppy_kennel import wings

    main = _make_repo(tmp_path / 'repos', 'projJ')
    monkeypatch.setattr(packer, 'PROMPT_BUDGET_CHARS', 120, raising=True)
    with pytest.raises(packer.KennelBudgetError):
        packer.pack(cwd_override=str(main))


def test_retriever_swallows_budget_error_returns_none(kroot, tmp_path, monkeypatch) -> None:
    """Impossible config must not crash the host: retriever emits no block."""
    packer = kroot['packer']
    from code_puppy_core_plugins.puppy_kennel import retriever

    monkeypatch.setattr(packer, 'CAPSULE_BUDGET_CHARS', 10_000, raising=True)
    # is_enabled() is True (fixture); pack() raises -> retriever returns None.
    assert retriever.build_recall_block() is None


# --------------------------------------------------------------------------- #
# 10. Retrieval helper: newest capsule is authoritative
# --------------------------------------------------------------------------- #
def test_capsule_drawer_returns_written(kroot, tmp_path: Path) -> None:
    kennel = kroot["kennel"]
    from code_puppy_core_plugins.puppy_kennel import wings

    main = _make_repo(tmp_path / "repos", "projE")
    wing = wings.repo_wing(main)
    assert kennel.capsule_drawer(wing) is None
    kennel.write_capsule(wing, CAPSULE_TEXT)
    got = kennel.capsule_drawer(wing)
    assert got is not None
    assert CAPSULE_MARKER in got.content
