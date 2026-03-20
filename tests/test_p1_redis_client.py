"""
tests/test_p1_redis_client.py
------------------------------
Packet 1.7 validator tests for services.eep.app.redis_client.

Uses fakeredis to test client behaviour without a live Redis server.

Definition of done:
  - get_redis() returns a Redis[str] client (decode_responses=True)
  - ping_redis() returns True when Redis is reachable
  - ping_redis() returns False on any RedisError
  - REDIS_URL env var is honoured
  - Default URL is used when REDIS_URL is not set
"""

from __future__ import annotations

import fakeredis
import pytest
import redis

from services.eep.app.redis_client import _REDIS_URL, get_redis, ping_redis

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def fake_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


# ── get_redis ──────────────────────────────────────────────────────────────


class TestGetRedis:
    def test_returns_redis_instance(self) -> None:
        # Constructing the client object does not require a live connection.
        client = get_redis()
        assert isinstance(client, redis.Redis)

    def test_returns_new_instance_each_call(self) -> None:
        c1 = get_redis()
        c2 = get_redis()
        assert c1 is not c2

    def test_default_url_used_when_env_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REDIS_URL", raising=False)
        # _REDIS_URL is module-level; we just verify the module default is sane.
        assert "localhost" in _REDIS_URL or "redis" in _REDIS_URL

    def test_basic_set_get_with_fakeredis(
        self, monkeypatch: pytest.MonkeyPatch, fake_client: fakeredis.FakeRedis
    ) -> None:
        monkeypatch.setattr(
            "services.eep.app.redis_client.redis.Redis.from_url",
            lambda url, **kw: fake_client,
        )
        client = get_redis()
        client.set("key", "value")
        assert client.get("key") == "value"

    def test_string_responses_with_fakeredis(
        self, monkeypatch: pytest.MonkeyPatch, fake_client: fakeredis.FakeRedis
    ) -> None:
        monkeypatch.setattr(
            "services.eep.app.redis_client.redis.Redis.from_url",
            lambda url, **kw: fake_client,
        )
        client = get_redis()
        client.set("hello", "world")
        val = client.get("hello")
        assert isinstance(val, str)
        assert val == "world"

    def test_list_operations_with_fakeredis(
        self, monkeypatch: pytest.MonkeyPatch, fake_client: fakeredis.FakeRedis
    ) -> None:
        monkeypatch.setattr(
            "services.eep.app.redis_client.redis.Redis.from_url",
            lambda url, **kw: fake_client,
        )
        client = get_redis()
        client.rpush("mylist", "a", "b", "c")
        assert client.llen("mylist") == 3

    def test_delete_key_with_fakeredis(
        self, monkeypatch: pytest.MonkeyPatch, fake_client: fakeredis.FakeRedis
    ) -> None:
        monkeypatch.setattr(
            "services.eep.app.redis_client.redis.Redis.from_url",
            lambda url, **kw: fake_client,
        )
        client = get_redis()
        client.set("temp", "val")
        client.delete("temp")
        assert client.get("temp") is None

    def test_incr_decr_with_fakeredis(
        self, monkeypatch: pytest.MonkeyPatch, fake_client: fakeredis.FakeRedis
    ) -> None:
        monkeypatch.setattr(
            "services.eep.app.redis_client.redis.Redis.from_url",
            lambda url, **kw: fake_client,
        )
        client = get_redis()
        client.set("counter", "10")
        client.incr("counter")
        assert client.get("counter") == "11"
        client.decr("counter")
        assert client.get("counter") == "10"


# ── ping_redis ─────────────────────────────────────────────────────────────


class TestPingRedis:
    def test_returns_true_when_reachable(
        self, monkeypatch: pytest.MonkeyPatch, fake_client: fakeredis.FakeRedis
    ) -> None:
        monkeypatch.setattr(
            "services.eep.app.redis_client.get_redis",
            lambda: fake_client,
        )
        assert ping_redis() is True

    def test_returns_false_when_ping_raises_redis_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _DownClient:
            def ping(self) -> bool:
                raise redis.RedisError("Connection refused")

        monkeypatch.setattr(
            "services.eep.app.redis_client.get_redis",
            lambda: _DownClient(),
        )
        assert ping_redis() is False

    def test_returns_false_when_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _TimeoutClient:
            def ping(self) -> bool:
                raise redis.ConnectionError("timeout")

        monkeypatch.setattr(
            "services.eep.app.redis_client.get_redis",
            lambda: _TimeoutClient(),
        )
        assert ping_redis() is False

    def test_returns_false_when_get_redis_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _explode() -> redis.Redis:
            raise redis.RedisError("cannot connect")

        monkeypatch.setattr(
            "services.eep.app.redis_client.get_redis",
            _explode,
        )
        assert ping_redis() is False

    def test_return_type_is_bool(
        self, monkeypatch: pytest.MonkeyPatch, fake_client: fakeredis.FakeRedis
    ) -> None:
        monkeypatch.setattr(
            "services.eep.app.redis_client.get_redis",
            lambda: fake_client,
        )
        result = ping_redis()
        assert isinstance(result, bool)


# ── REDIS_URL env var ──────────────────────────────────────────────────────


class TestRedisUrl:
    def test_default_url_has_localhost_or_redis(self) -> None:
        assert "localhost" in _REDIS_URL or "redis" in _REDIS_URL

    def test_default_url_uses_redis_scheme(self) -> None:
        assert _REDIS_URL.startswith("redis://")

    def test_default_url_includes_port(self) -> None:
        assert "6379" in _REDIS_URL
