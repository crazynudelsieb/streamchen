"""Cat avatars, drawn here rather than fetched from anybody.

There are free avatar services that would do this in one line of template —
and every one of them means the browser announcing a per-listener identifier
to a third party on every page load, which is the one thing this app promises
not to do (concept §12). So the cats are generated: a hash of the listener's
id picks the fur, the eyes and the markings, and the result is a kilobyte of
SVG that never leaves the instance.

Deterministic on purpose. The same listener is the same cat on every page, in
every browser, for as long as the room lasts — that is what makes an avatar
worth having next to a name, and it is why the seed can be cached forever.
"""

from __future__ import annotations

import hashlib
import uuid

# Fur, and the darker shade its markings are drawn in.
_FUR = (
    ("#f59e0b", "#b45309"),  # ginger
    ("#94a3b8", "#475569"),  # grey
    ("#e2e8f0", "#94a3b8"),  # white
    ("#a16207", "#713f12"),  # brown
    ("#475569", "#1e293b"),  # charcoal
    ("#fbbf24", "#d97706"),  # marmalade
    ("#7dd3fc", "#0284c7"),  # russian blue, flattered
    ("#fda4af", "#e11d48"),  # nobody has this cat
)

_EYES = ("#22c55e", "#f59e0b", "#38bdf8", "#a3e635", "#c084fc")

# Deliberately close to the card background: an avatar is a face, not a tile.
_BACKGROUND = ("#0f172a", "#1e293b", "#111827", "#164e63", "#1e3a8a", "#3f2937")

_MARKINGS = ("plain", "tabby", "patch", "socks")


def avatar_seed(value: uuid.UUID | str) -> str:
    """The short, stable token a listener's cat is drawn from.

    Hashed rather than the id itself: the avatar URL is public to everyone in
    the room and there is no reason for it to carry a database key around.
    """
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _picks(seed: str) -> list[int]:
    """Bytes to make choices from. Any seed at all, always the same choices."""
    return list(hashlib.sha256(seed.encode("utf-8")).digest())


def cat_svg(seed: str) -> str:
    """One cat, as a standalone SVG document."""
    picks = _picks(seed)
    fur, shade = _FUR[picks[0] % len(_FUR)]
    eye = _EYES[picks[1] % len(_EYES)]
    background = _BACKGROUND[picks[2] % len(_BACKGROUND)]
    marking = _MARKINGS[picks[3] % len(_MARKINGS)]
    # Small variations that stop two cats of the same colour looking identical.
    tilt = (picks[4] % 5) - 2
    ear_lift = picks[5] % 3
    pupil = 1.4 + (picks[6] % 3) * 0.5

    # The head fills most of the tile. These are rendered at 18-34 pixels next
    # to a name, and a small face on a large background is a smudge — at that
    # size the circle has to *be* the cat.
    clip = f"c{seed}"
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" '
        'role="img" aria-label="cat avatar">',
        f'<defs><clipPath id="{clip}">'
        f'<ellipse cx="32" cy="35" rx="26" ry="24"/></clipPath></defs>',
        f'<rect width="64" height="64" rx="32" fill="{background}"/>',
        # Ears, behind the head so the join never shows.
        f'<path d="M10 30 L15 {6 + ear_lift} L30 20 Z" fill="{fur}"/>',
        f'<path d="M54 30 L49 {6 + ear_lift} L34 20 Z" fill="{fur}"/>',
        f'<path d="M15 26 L18 {13 + ear_lift} L26 21 Z" fill="{shade}"/>',
        f'<path d="M49 26 L46 {13 + ear_lift} L38 21 Z" fill="{shade}"/>',
        f'<ellipse cx="32" cy="35" rx="26" ry="24" fill="{fur}"/>',
    ]

    if marking == "tabby":
        parts.append(
            f'<g clip-path="url(#{clip})" stroke="{shade}" stroke-width="3" '
            f'stroke-linecap="round" fill="none">'
            f'<path d="M32 12 v8"/><path d="M24 14 l3 7"/><path d="M40 14 l-3 7"/>'
            f'<path d="M6 32 h9"/><path d="M58 32 h-9"/></g>'
        )
    elif marking == "patch":
        parts.append(
            f'<g clip-path="url(#{clip})">'
            f'<ellipse cx="12" cy="28" rx="16" ry="18" fill="{shade}" opacity=".6"/></g>'
        )
    elif marking == "socks":
        parts.append(
            f'<g clip-path="url(#{clip})">'
            f'<ellipse cx="32" cy="60" rx="18" ry="11" fill="{shade}" opacity=".5"/></g>'
        )

    parts += [
        # Eyes. Big, because at 18 pixels they are the whole face; the tilt is
        # the whole personality.
        f'<g transform="rotate({tilt} 32 33)">',
        '<ellipse cx="22" cy="33" rx="6" ry="7" fill="#f8fafc"/>',
        '<ellipse cx="42" cy="33" rx="6" ry="7" fill="#f8fafc"/>',
        f'<ellipse cx="22" cy="33" rx="4.6" ry="5.6" fill="{eye}"/>',
        f'<ellipse cx="42" cy="33" rx="4.6" ry="5.6" fill="{eye}"/>',
        f'<ellipse cx="22" cy="33" rx="{pupil}" ry="5.4" fill="#0f172a"/>',
        f'<ellipse cx="42" cy="33" rx="{pupil}" ry="5.4" fill="#0f172a"/>',
        "</g>",
        # Nose and mouth.
        '<path d="M32 46 L36 42 L28 42 Z" fill="#fb7185"/>',
        '<path d="M32 46 v3 M32 49 q-3.5 3-6 .5 M32 49 q3.5 3 6 .5" '
        'stroke="#0f172a" stroke-width="1.8" fill="none" stroke-linecap="round"/>',
        # Whiskers.
        '<g stroke="#f8fafc" stroke-width="1.4" stroke-linecap="round" opacity=".85">'
        '<path d="M26 46 L8 43"/><path d="M26 49 L9 51"/>'
        '<path d="M38 46 L56 43"/><path d="M38 49 L55 51"/></g>',
        "</svg>",
    ]
    return "".join(parts)


__all__ = ["avatar_seed", "cat_svg"]
