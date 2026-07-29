"""Tokens, secrets, CSRF."""

from __future__ import annotations

from app.security import (
    hash_secret,
    is_valid_room_token,
    new_host_secret,
    new_room_token,
    tokens_equal,
    verify_secret,
)


def test_room_tokens_are_url_safe_and_unguessable():
    tokens = {new_room_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert is_valid_room_token(token)
        assert "/" not in token and "+" not in token and "=" not in token


def test_obvious_rubbish_is_not_a_room_token():
    for value in ("", "short", "../../etc/passwd", "a" * 100, "has spaces here"):
        assert not is_valid_room_token(value)


def test_host_secret_verifies_only_against_itself():
    secret = new_host_secret()
    stored = hash_secret(secret)

    assert verify_secret(stored, secret)
    assert not verify_secret(stored, new_host_secret())


def test_the_plaintext_secret_is_not_recoverable_from_the_hash():
    secret = new_host_secret()
    assert secret not in hash_secret(secret)


def test_a_corrupt_stored_hash_reads_as_no_match():
    assert not verify_secret("not-an-argon2-hash", "anything")


def test_token_comparison_rejects_empties():
    assert tokens_equal("abc", "abc")
    assert not tokens_equal("abc", "abd")
    assert not tokens_equal(None, None)
    assert not tokens_equal("", "")
