"""Contact / social link helpers (shared standard across the appchen apps).

Turns the CONTACT_* config into the list of links to render (only the channels
an operator actually configured), and splits an email address so no literal
address — and no ``mailto:`` — ever appears in the page source. The client
reassembles the address (see the ``useMailto`` hook in the frontend), which
defeats the naive harvesters that scrape pages for ``\\S+@\\S+`` or ``mailto:``
links.
"""

from __future__ import annotations

from app.config import Settings

_SCHEMES = ("http://", "https://")


def _as_url(value: str, base: str) -> str:
    """A full URL is used as-is; anything else is treated as a username that
    hangs off ``base`` (so operators can set either)."""
    value = value.strip()
    if value.startswith(_SCHEMES):
        return value
    return base + value.lstrip("@/")


def get_contact_links(settings: Settings) -> list[dict[str, str]]:
    """``[{key, label, icon, url, rel}]`` for every configured channel.

    Order is fixed; channels with no value are omitted, so the footer shows
    exactly what the operator opted into. Icons are Bootstrap Icons names.
    """
    links: list[dict[str, str]] = []

    if settings.contact_mastodon:
        links.append(
            {
                "key": "mastodon",
                "label": "Mastodon",
                "icon": "bi-mastodon",
                # A full profile URL is expected. rel="me" lets Mastodon verify
                # this link as belonging back to that profile.
                "url": settings.contact_mastodon,
                "rel": "me noopener noreferrer",
            }
        )

    if settings.contact_github:
        links.append(
            {
                "key": "github",
                "label": "GitHub",
                "icon": "bi-github",
                "url": _as_url(settings.contact_github, "https://github.com/"),
                "rel": "noopener noreferrer",
            }
        )

    if settings.contact_kofi:
        links.append(
            {
                "key": "kofi",
                "label": "Ko-fi",
                "icon": "bi-cup-straw",
                "url": _as_url(settings.contact_kofi, "https://ko-fi.com/"),
                "rel": "noopener noreferrer",
            }
        )

    if settings.contact_buymeacoffee:
        links.append(
            {
                "key": "buymeacoffee",
                "label": "Buy Me a Coffee",
                "icon": "bi-cup-hot",
                "url": _as_url(settings.contact_buymeacoffee, "https://www.buymeacoffee.com/"),
                "rel": "noopener noreferrer",
            }
        )

    return links


def split_email(email: str) -> list[str] | None:
    """``[user, domain]`` for a valid address, else ``None``.

    The client renders these two parts separately (never joined with ``@``) and
    rebuilds the real address and the ``mailto:`` link at runtime.
    """
    email = (email or "").strip()
    if email.count("@") != 1:
        return None
    user, _, domain = email.partition("@")
    if user and domain:
        return [user, domain]
    return None


def legal_payload(settings: Settings) -> dict:
    """Everything the footer and the legal pages need, computed from config.

    Served from ``/api/meta`` rather than baked into the bundle so an operator
    can change their contact details by restarting a container.
    """
    return {
        "contact_links": get_contact_links(settings),
        "contact_email": split_email(settings.contact_email),
        "imprint_enabled": settings.imprint_enabled,
        # Commercial-licensing contact, split so no literal address appears in
        # the page source; the client reassembles it like the others.
        "license_email": split_email("appchen@outlook.at"),
    }


def imprint_payload(settings: Settings) -> dict:
    """The site notice itself (EU: TMG / ECG). Empty fields are dropped by the
    page, so an operator only fills in what applies to them."""
    return {
        "enabled": settings.imprint_enabled,
        "name": settings.imprint_name,
        "address": settings.imprint_address,
        "email": split_email(settings.imprint_contact_email),
        "phone": settings.imprint_phone,
        "vat": settings.imprint_vat,
        "extra": settings.imprint_extra,
    }
