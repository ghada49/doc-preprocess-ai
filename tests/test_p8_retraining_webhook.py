"""
tests/test_p8_retraining_webhook.py
-------------------------------------
Packet 8.4 contract tests for POST /v1/retraining/webhook.

Tests cover:
  - 200 on firing alert with valid trigger_type → status="recorded"
  - resolved alert → status="skipped_resolved"
  - unknown trigger_type → status="skipped_unknown"
  - absent trigger_type label → status="skipped_unknown"
  - active cooldown → status="skipped_cooldown"; db.add not called
  - persistence_hours=24 for escalation_rate_anomaly, auto_accept_rate_collapse
  - persistence_hours=48 for structural_agreement_degradation,
    drift_alert_persistence, layout_confidence_degradation
  - cooldown_until = now + 7 days
  - fired_at parsed from alert startsAt
  - fired_at defaults to now when startsAt absent
  - metric_value and threshold_value parsed from labels
  - metric_name defaults to trigger_type when absent from labels
  - empty alerts list → processed=0, results=[]
  - multiple alerts processed independently
  - mixed firing+resolved in one payload
  - db.add called once per recorded alert only
  - all 5 trigger types accepted
  - no auth required (no 401 / 403)
  - response schema matches WebhookResponse contract
  - trigger_id is a valid UUID string
  - status field on DB row is "pending"

Uses a mini FastAPI app with only the webhook router. DB is mocked via
dependency overrides.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.eep.app.db.session import get_session
from services.eep.app.retraining_webhook import router

# ── Constants ─────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
_STARTS_AT = "2026-03-28T10:00:00Z"
_STARTS_AT_PARSED = datetime(2026, 3, 28, 10, 0, 0, tzinfo=timezone.utc)


# ── Mock session factory ──────────────────────────────────────────────────────


def _make_session(cooldown_result: Any = None) -> MagicMock:
    """
    Build a mock DB session.

    cooldown_result=None  → _is_in_cooldown returns False (no active cooldown)
    cooldown_result=<obj> → _is_in_cooldown returns True (cooldown active)
    """
    session = MagicMock()

    def _query(model):
        q = MagicMock()

        def _filter(*args):
            f = MagicMock()
            f.first.return_value = cooldown_result
            return f

        q.filter = _filter
        return q

    session.query = _query
    session.add = MagicMock()
    session.commit = MagicMock()
    session.refresh = MagicMock()
    return session


# ── Payload builder ───────────────────────────────────────────────────────────


def _firing_payload(
    trigger_type: str = "escalation_rate_anomaly",
    metric_name: str = "escalation_rate",
    metric_value: str = "0.27",
    threshold_value: str = "0.25",
    starts_at: str | None = None,
    alert_status: str = "firing",
) -> dict:
    alert: dict = {
        "status": alert_status,
        "labels": {
            "alertname": "TestAlert",
            "trigger_type": trigger_type,
            "metric_name": metric_name,
            "metric_value": metric_value,
            "threshold_value": threshold_value,
        },
    }
    if starts_at is not None:
        alert["startsAt"] = starts_at
    return {"version": "4", "status": "firing", "alerts": [alert]}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mini_app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def inject_session(mini_app: FastAPI):
    """Yield a factory: call with a mock session to get a TestClient."""

    def _setup(session: MagicMock) -> TestClient:
        mini_app.dependency_overrides[get_session] = lambda: session
        client = TestClient(mini_app, raise_server_exceptions=False)
        # Endpoint requires X-Webhook-Secret; default env value used in tests
        client.headers.update({"X-Webhook-Secret": "dev-webhook-secret-change-in-production"})
        return client

    yield _setup
    mini_app.dependency_overrides.pop(get_session, None)


# ── Basic recording ───────────────────────────────────────────────────────────


class TestRecording:
    def test_200_firing_alert_recorded(self, inject_session) -> None:
        client = inject_session(_make_session())
        resp = client.post("/v1/retraining/webhook", json=_firing_payload())
        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 1
        assert data["results"][0]["status"] == "recorded"
        assert data["results"][0]["trigger_id"] is not None

    def test_response_schema_fields(self, inject_session) -> None:
        client = inject_session(_make_session())
        resp = client.post("/v1/retraining/webhook", json=_firing_payload())
        data = resp.json()
        assert set(data.keys()) == {"processed", "results"}
        assert set(data["results"][0].keys()) == {"trigger_id", "trigger_type", "status"}

    def test_trigger_type_echoed_in_response(self, inject_session) -> None:
        client = inject_session(_make_session())
        resp = client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(trigger_type="auto_accept_rate_collapse"),
        )
        assert resp.json()["results"][0]["trigger_type"] == "auto_accept_rate_collapse"

    def test_empty_alerts_list(self, inject_session) -> None:
        client = inject_session(_make_session())
        resp = client.post(
            "/v1/retraining/webhook",
            json={"status": "firing", "alerts": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 0
        assert data["results"] == []

    def test_no_auth_required(self, inject_session) -> None:
        """No Authorization header must still return 200."""
        client = inject_session(_make_session())
        resp = client.post("/v1/retraining/webhook", json=_firing_payload())
        assert resp.status_code == 200


# ── Skip conditions ───────────────────────────────────────────────────────────


class TestSkipConditions:
    def test_resolved_alert_skipped(self, inject_session) -> None:
        client = inject_session(_make_session())
        resp = client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(alert_status="resolved"),
        )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["status"] == "skipped_resolved"

    def test_resolved_alert_has_null_trigger_id(self, inject_session) -> None:
        client = inject_session(_make_session())
        resp = client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(alert_status="resolved"),
        )
        assert resp.json()["results"][0]["trigger_id"] is None

    def test_unknown_trigger_type_skipped(self, inject_session) -> None:
        client = inject_session(_make_session())
        resp = client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(trigger_type="not_a_real_trigger"),
        )
        assert resp.json()["results"][0]["status"] == "skipped_unknown"

    def test_missing_trigger_type_label_skipped(self, inject_session) -> None:
        client = inject_session(_make_session())
        payload = {
            "status": "firing",
            "alerts": [{"status": "firing", "labels": {"metric_name": "some_metric"}}],
        }
        resp = client.post("/v1/retraining/webhook", json=payload)
        assert resp.json()["results"][0]["status"] == "skipped_unknown"

    def test_cooldown_active_skipped(self, inject_session) -> None:
        client = inject_session(_make_session(cooldown_result=MagicMock()))
        resp = client.post("/v1/retraining/webhook", json=_firing_payload())
        assert resp.json()["results"][0]["status"] == "skipped_cooldown"
        assert resp.json()["results"][0]["trigger_id"] is None

    def test_cooldown_skipped_does_not_write_to_db(self, inject_session) -> None:
        session = _make_session(cooldown_result=MagicMock())
        client = inject_session(session)
        client.post("/v1/retraining/webhook", json=_firing_payload())
        session.add.assert_not_called()
        session.commit.assert_not_called()


# ── All trigger types ─────────────────────────────────────────────────────────


class TestAllTriggerTypes:
    @pytest.mark.parametrize(
        "trigger_type",
        [
            "escalation_rate_anomaly",
            "auto_accept_rate_collapse",
            "structural_agreement_degradation",
            "drift_alert_persistence",
            "layout_confidence_degradation",
        ],
    )
    def test_trigger_type_recorded(self, inject_session, trigger_type: str) -> None:
        client = inject_session(_make_session())
        resp = client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(trigger_type=trigger_type),
        )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["status"] == "recorded"


# ── Persistence hours ─────────────────────────────────────────────────────────


class TestPersistenceHours:
    @pytest.mark.parametrize(
        "trigger_type",
        ["escalation_rate_anomaly", "auto_accept_rate_collapse"],
    )
    def test_24h_persistence(self, inject_session, trigger_type: str) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(trigger_type=trigger_type),
        )
        added_row = session.add.call_args[0][0]
        assert added_row.persistence_hours == 24.0

    @pytest.mark.parametrize(
        "trigger_type",
        [
            "structural_agreement_degradation",
            "drift_alert_persistence",
            "layout_confidence_degradation",
        ],
    )
    def test_48h_persistence(self, inject_session, trigger_type: str) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(trigger_type=trigger_type),
        )
        added_row = session.add.call_args[0][0]
        assert added_row.persistence_hours == 48.0


# ── Cooldown timestamp ────────────────────────────────────────────────────────


class TestCooldownTimestamp:
    def test_cooldown_until_is_7_days_from_now(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        with patch("services.eep.app.retraining_webhook.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.fromisoformat = datetime.fromisoformat
            client.post("/v1/retraining/webhook", json=_firing_payload())
        added_row = session.add.call_args[0][0]
        assert added_row.cooldown_until == _NOW + timedelta(days=7)


# ── fired_at parsing ──────────────────────────────────────────────────────────


class TestFiredAt:
    def test_fired_at_parsed_from_starts_at(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(starts_at=_STARTS_AT),
        )
        added_row = session.add.call_args[0][0]
        assert added_row.fired_at == _STARTS_AT_PARSED

    def test_fired_at_defaults_to_now_when_starts_at_absent(
        self, inject_session
    ) -> None:
        session = _make_session()
        client = inject_session(session)
        with patch("services.eep.app.retraining_webhook.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.fromisoformat = datetime.fromisoformat
            client.post("/v1/retraining/webhook", json=_firing_payload(starts_at=None))
        added_row = session.add.call_args[0][0]
        assert added_row.fired_at == _NOW

    def test_fired_at_defaults_to_now_on_malformed_starts_at(
        self, inject_session
    ) -> None:
        session = _make_session()
        client = inject_session(session)
        with patch("services.eep.app.retraining_webhook.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.fromisoformat.side_effect = ValueError("bad format")
            client.post(
                "/v1/retraining/webhook",
                json=_firing_payload(starts_at="not-a-date"),
            )
        added_row = session.add.call_args[0][0]
        assert added_row.fired_at == _NOW


# ── Metric value parsing ──────────────────────────────────────────────────────


class TestMetricParsing:
    def test_metric_value_parsed_from_label(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(metric_value="0.31", threshold_value="0.25"),
        )
        added_row = session.add.call_args[0][0]
        assert added_row.metric_value == pytest.approx(0.31)
        assert added_row.threshold_value == pytest.approx(0.25)

    def test_metric_name_from_label(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(metric_name="iep1_escalation_rate"),
        )
        added_row = session.add.call_args[0][0]
        assert added_row.metric_name == "iep1_escalation_rate"

    def test_metric_name_defaults_to_trigger_type_when_absent(
        self, inject_session
    ) -> None:
        session = _make_session()
        client = inject_session(session)
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "trigger_type": "escalation_rate_anomaly",
                        "metric_value": "0.27",
                        "threshold_value": "0.25",
                    },
                }
            ],
        }
        client.post("/v1/retraining/webhook", json=payload)
        added_row = session.add.call_args[0][0]
        assert added_row.metric_name == "escalation_rate_anomaly"

    def test_metric_value_defaults_to_zero_when_absent(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"trigger_type": "escalation_rate_anomaly"},
                }
            ],
        }
        client.post("/v1/retraining/webhook", json=payload)
        added_row = session.add.call_args[0][0]
        assert added_row.metric_value == pytest.approx(0.0)
        assert added_row.threshold_value == pytest.approx(0.0)


# ── DB record values ──────────────────────────────────────────────────────────


class TestDbRecordValues:
    def test_status_is_pending(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post("/v1/retraining/webhook", json=_firing_payload())
        added_row = session.add.call_args[0][0]
        assert added_row.status == "pending"

    def test_trigger_id_is_valid_uuid(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post("/v1/retraining/webhook", json=_firing_payload())
        added_row = session.add.call_args[0][0]
        # Raises ValueError if not a valid UUID
        uuid_mod.UUID(added_row.trigger_id)

    def test_trigger_type_on_db_row(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post(
            "/v1/retraining/webhook",
            json=_firing_payload(trigger_type="drift_alert_persistence"),
        )
        added_row = session.add.call_args[0][0]
        assert added_row.trigger_type == "drift_alert_persistence"

    def test_db_committed_after_add(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        client.post("/v1/retraining/webhook", json=_firing_payload())
        session.commit.assert_called_once()


# ── Multiple alerts ───────────────────────────────────────────────────────────


class TestMultipleAlerts:
    def test_two_firing_alerts_both_recorded(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "trigger_type": "escalation_rate_anomaly",
                        "metric_value": "0.27",
                        "threshold_value": "0.25",
                    },
                },
                {
                    "status": "firing",
                    "labels": {
                        "trigger_type": "auto_accept_rate_collapse",
                        "metric_value": "0.38",
                        "threshold_value": "0.40",
                    },
                },
            ],
        }
        resp = client.post("/v1/retraining/webhook", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 2
        statuses = {r["trigger_type"]: r["status"] for r in data["results"]}
        assert statuses["escalation_rate_anomaly"] == "recorded"
        assert statuses["auto_accept_rate_collapse"] == "recorded"

    def test_mixed_firing_and_resolved(self, inject_session) -> None:
        session = _make_session()
        client = inject_session(session)
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "trigger_type": "escalation_rate_anomaly",
                        "metric_value": "0.27",
                        "threshold_value": "0.25",
                    },
                },
                {
                    "status": "resolved",
                    "labels": {"trigger_type": "auto_accept_rate_collapse"},
                },
            ],
        }
        resp = client.post("/v1/retraining/webhook", json=payload)
        data = resp.json()
        assert data["processed"] == 2
        result_statuses = [r["status"] for r in data["results"]]
        assert "recorded" in result_statuses
        assert "skipped_resolved" in result_statuses

    def test_db_add_called_once_per_recorded_alert_only(
        self, inject_session
    ) -> None:
        session = _make_session()
        client = inject_session(session)
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "trigger_type": "escalation_rate_anomaly",
                        "metric_value": "0.27",
                        "threshold_value": "0.25",
                    },
                },
                {
                    "status": "resolved",
                    "labels": {"trigger_type": "auto_accept_rate_collapse"},
                },
            ],
        }
        client.post("/v1/retraining/webhook", json=payload)
        assert session.add.call_count == 1
