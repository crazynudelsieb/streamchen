"""The WebSocket endpoint end to end.

Uses Starlette's synchronous TestClient rather than the async fixtures: it
drives a real handshake, which is the part worth proving.
"""

from __future__ import annotations

import uuid

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import create_app
from app.api.routers.ws import CLOSE_REMOVED
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
        client.get("/api/healthz")
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


class Browsers:
    """Two anonymous listeners out of one test client.

    A session is a cookie and nothing else, so swapping it is enough to be
    somebody else. The host secret goes with it: presenting it would make
    whoever is holding the cookie a host.
    """

    def __init__(self, client: TestClient, secret: str):
        self.client = client
        self.secret = secret
        self.host_session = client.cookies["sc_session"]
        self.guest_session = str(uuid.uuid4())

    def as_host(self) -> TestClient:
        self.client.cookies.set("sc_session", self.host_session)
        self.client.headers["X-Host-Secret"] = self.secret
        return self.client

    def as_guest(self) -> TestClient:
        self.client.cookies.set("sc_session", self.guest_session)
        self.client.headers.pop("X-Host-Secret", None)
        return self.client


def two_browsers(ws_client: TestClient) -> tuple[dict, Browsers]:
    room = ws_client.post("/api/rooms", json={"name": "Realtime"}).json()
    return room, Browsers(ws_client, room["host_secret"])


def ban_the_guest(browsers: Browsers, token: str) -> None:
    client = browsers.as_host()
    rows = client.get(f"/api/rooms/{token}/listeners").json()
    victim = next(row for row in rows if not row["is_host"])
    assert client.post(f"/api/rooms/{token}/bans", json={"listener_id": victim["id"]}).status_code


def test_removing_a_listener_hangs_up_their_socket(ws_client):
    """Otherwise the socket lives on, and its presence heartbeat keeps a
    listener nobody can see in the room's count."""
    room, browsers = two_browsers(ws_client)
    token = room["token"]

    with browsers.as_guest().websocket_connect(f"/api/rooms/{token}/ws") as socket:
        handshake(socket, token)

        ban_the_guest(browsers, token)

        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()

    assert closed.value.code == CLOSE_REMOVED


def test_a_removed_listener_cannot_open_another_socket(ws_client):
    """The socket joins the room like every other door into it, so it has to
    refuse at the same place -- joining again would undo the removal."""
    room, browsers = two_browsers(ws_client)
    token = room["token"]

    guest = browsers.as_guest()
    assert guest.get(f"/api/rooms/{token}").status_code == 200
    ban_the_guest(browsers, token)

    with pytest.raises(WebSocketDisconnect) as closed:
        with browsers.as_guest().websocket_connect(f"/api/rooms/{token}/ws") as socket:
            socket.receive_json()

    assert closed.value.code == CLOSE_REMOVED
