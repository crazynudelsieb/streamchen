"""Text that arrives from a browser and is rendered back into a fixed row.

Display names and chat lines are the same problem wearing two names, so the
rule lives here once and both call it.
"""

from __future__ import annotations


def fold_to_line(value: str | None, limit: int) -> str:
    """``value`` on one line, at most ``limit`` characters, or "" if what was
    sent amounts to nothing.

    Odd characters are cleaned up rather than rejected: text arriving with a
    stray line break is a paste, not an attack, and it has to render on one
    line either way. Whitespace is folded *before* unprintables are dropped, so
    a line break separates two words instead of welding them together, and
    zero-width characters cannot smuggle in invisible content. The final strip
    matters because the length limit can cut just after a space.
    """
    words = ("".join(char for char in word if char.isprintable()) for word in (value or "").split())
    return " ".join(word for word in words if word)[:limit].strip()
