"""Footer / legal helpers (the shared appchen standard)."""

from __future__ import annotations

from app.config import Settings
from app.contact import get_contact_links, legal_payload, split_email


def make_settings(**overrides) -> Settings:
    base = {
        "database_url": "sqlite+aiosqlite://",
        "redis_url": "redis://localhost:6379/0",
        "base_url": "http://test",
    }
    return Settings(**(base | overrides))


def test_no_channels_configured_means_no_links():
    assert get_contact_links(make_settings()) == []


def test_only_configured_channels_appear_and_order_is_fixed():
    links = get_contact_links(
        make_settings(
            contact_github="crazynudelsieb",
            contact_kofi="https://ko-fi.com/someone",
            contact_mastodon="https://mastodon.social/@someone",
        )
    )
    assert [link["key"] for link in links] == ["mastodon", "github", "kofi"]


def test_username_is_expanded_but_a_url_is_left_alone():
    links = get_contact_links(
        make_settings(contact_github="@crazynudelsieb", contact_kofi="https://ko-fi.com/x")
    )
    by_key = {link["key"]: link for link in links}
    assert by_key["github"]["url"] == "https://github.com/crazynudelsieb"
    assert by_key["kofi"]["url"] == "https://ko-fi.com/x"


def test_mastodon_link_carries_rel_me_for_verification():
    links = get_contact_links(make_settings(contact_mastodon="https://mastodon.social/@a"))
    assert "me" in links[0]["rel"].split()


def test_email_is_split_so_no_literal_address_is_rendered():
    assert split_email("hello@example.com") == ["hello", "example.com"]


def test_malformed_addresses_are_dropped():
    for value in ("", "nope", "two@at@signs.com", "@example.com", "user@"):
        assert split_email(value) is None


def test_imprint_is_enabled_only_once_a_name_is_set():
    assert legal_payload(make_settings())["imprint_enabled"] is False
    assert legal_payload(make_settings(imprint_name="Someone"))["imprint_enabled"] is True


def test_license_contact_is_always_present_and_split():
    assert legal_payload(make_settings())["license_email"] == ["appchen", "outlook.at"]
