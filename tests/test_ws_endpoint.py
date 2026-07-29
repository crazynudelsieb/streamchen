"""The WebSocket endpoint end to end.

Uses Starlette's synchronous TestClient rather than the async fixtures: it
drives a real handshake, which is the part worth proving.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import create_app
from app.config import Settings
from tests.conftest import watch_url


@pytest.fixture
def ws_client(monkeypatch):
    monkeypatch.setattr(
        "app.api.make_redis",
        lambda _settings: fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        add_rate_limit=100,
    )
    with TestClient(create_app(settings)) as client:
        client.get("/api/meta")
        client.headers["X-CSRF-Token"] = client.cookies["sc_csrf"]
        yield client


def make_room(client: TestClient) -> dict:
    room = client.post("/api/rooms", json={"name": "Realtime"}).json()
    client.headers["X-Host-Secret"] = room["host_secret"]
    return room


def handshake(socket, token: str) -> None:
    """Consume the two frames every connection starts with.

    A client is told READY, and then hears about its own arrival — presence is
    broadcast to the whole room and the newcomer is already in it.
    """
    ready = socket.receive_json()
    assert ready["type"] == "READY"
    assert ready["data"]["room"] == token

    assert socket.receive_json()["type"] == "LISTENER_JOINED"


def collect(socket, count: int) -> set[str]:
    """Read exactly ``count`` frames. Reading more would block forever, so
    tests state how many they expect."""
    return {socket.receive_json()["type"] for _ in range(count)}


def test_connecting_to_an_unknown_room_is_refused(ws_client):
    # The endpoint closes with 4404 instead of accepting, so the client never
    # gets a usable socket.
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect("/api/rooms/nosuchroomtoken1234/ws") as socket:
            socket.receive_json()


def test_the_socket_announces_itself_then_relays_room_events(ws_client):
    room = make_room(ws_client)

    with ws_client.websocket_connect(f"/api/rooms/{room['token']}/ws") as socket:
        handshake(socket, room["token"])

        response = ws_client.post(
            f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)}
        )
        assert response.status_code == 201

        assert collect(socket, 2) == {"SONG_ADDED", "QUEUE_CHANGED"}


def test_the_socket_answers_a_ping(ws_client):
    room = make_room(ws_client)

    with ws_client.websocket_connect(f"/api/rooms/{room['token']}/ws") as socket:
        handshake(socket, room["token"])
        socket.send_text("ping")
        assert socket.receive_json()["type"] == "PONG"
