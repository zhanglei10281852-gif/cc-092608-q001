from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import get_connection
from app.network.rules import DEFAULT_RULES
from app.network.service import NetworkAccelerationService

SCENARIO = {"code": "gdh-rail", "name": "广深高铁", "scene_type": "railway", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 100, "capacity_mbps": 1000}
SEGMENT = {"code": "gz-sz-01", "name": "广州南至虎门", "sequence_no": 1, "expected_dwell_seconds": 900, "capacity_mbps": 1200}
APP = {"app_code": "video-call", "name": "视频通话", "category": "video_call", "latency_target_ms": 100, "packet_loss_target": 0.01, "min_downlink_mbps": 8, "min_uplink_mbps": 4, "default_priority": 70}

T_SEP20 = datetime(2026, 9, 20, tzinfo=UTC)
T_SEP29 = datetime(2026, 9, 29, 12, tzinfo=UTC)
T_OCT1 = datetime(2026, 10, 1, tzinfo=UTC)
T_OCT2 = datetime(2026, 10, 2, 12, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S+00:00")


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("NETWORK_DATABASE_PATH", str(tmp_path / "policy.db"))
    from app.database import close_connection
    close_connection()
    connection = get_connection()

    def build(clock: datetime | FrozenClock) -> NetworkAccelerationService:
        if isinstance(clock, datetime):
            clock = FrozenClock(clock)
        return NetworkAccelerationService(connection, clock)

    build(T_SEP20).create_scenario(SCENARIO)
    build(T_SEP20).add_segment("gdh-rail", SEGMENT)
    build(T_SEP20).create_application(APP)
    return build


def _publish_two_versions(build):
    v1 = build(T_SEP20).create_policy("gdh-rail", DEFAULT_RULES, "ops-alice")
    build(T_SEP20).publish_policy(v1["id"], "ops-alice", "2026-09-26T00:00:00+00:00")
    oct_rules = {**DEFAULT_RULES, "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 240}}
    v2 = build(T_SEP29).create_policy("gdh-rail", oct_rules, "ops-bob")
    build(T_SEP29).publish_policy(v2["id"], "ops-bob", "2026-10-01T00:00:00+00:00")
    return v1, v2


def _row(connection, policy_id):
    return dict(connection.execute("SELECT * FROM policy_versions WHERE id=?", (policy_id,)).fetchone())


def test_future_version_stays_published_pending(service):
    build = service
    v1, v2 = _publish_two_versions(build)
    connection = get_connection()
    current, future = _row(connection, v1["id"]), _row(connection, v2["id"])
    assert future["state"] == "published"
    assert future["is_active"] == 0
    assert future["effective_from"] == "2026-10-01T00:00:00+00:00"
    assert future["published_by"] == "ops-bob"
    assert future["published_at"] == _iso(T_SEP29)
    assert current["state"] == "published"
    assert current["is_active"] == 1
    assert current["retired_at"] is None
    assert connection.execute("SELECT COUNT(*) FROM policy_versions WHERE scenario_id=1 AND is_active=1").fetchone()[0] == 1


def test_effective_policy_is_selected_point_in_time(service):
    build = service
    v1, v2 = _publish_two_versions(build)
    repository = build(T_SEP20).repository
    assert repository.effective_policy(1, "2026-09-25T23:59:59+00:00") is None
    assert repository.effective_policy(1, "2026-09-26T00:00:00+00:00")["id"] == v1["id"]
    assert repository.effective_policy(1, "2026-09-30T23:59:59+00:00")["id"] == v1["id"]
    # 到达切换时刻前新版本不可被选中
    assert repository.effective_policy(1, "2026-09-30T23:59:59+00:00")["id"] != v2["id"]
    # 调度切换后历史时刻仍能查到当时生效的版本
    build(T_OCT1).activate_due_policies()
    assert repository.effective_policy(1, "2026-09-27T00:00:00+00:00")["id"] == v1["id"]
    assert repository.effective_policy(1, "2026-10-01T00:00:00+00:00")["id"] == v2["id"]
    assert repository.effective_policy(1, "2026-10-02T00:00:00+00:00")["id"] == v2["id"]


def test_scheduler_takes_over_at_boundary_with_full_history(service):
    build = service
    v1, v2 = _publish_two_versions(build)
    assert build(T_SEP29).activate_due_policies() == {"activated": [], "retired": []}
    result = build(T_OCT1).activate_due_policies()
    assert result["activated"] == [{"id": v2["id"], "version_no": 2, "effective_from": "2026-10-01T00:00:00+00:00"}]
    assert result["retired"] == [{"id": v1["id"], "version_no": 1, "retired_at": "2026-10-01T00:00:00+00:00", "retired_by": "ops-bob"}]
    old, new = _row(get_connection(), v1["id"]), _row(get_connection(), v2["id"])
    assert old["state"] == "retired"
    assert old["is_active"] == 0
    assert old["retired_at"] == "2026-10-01T00:00:00+00:00"
    assert old["retired_by"] == "ops-bob"
    assert old["published_by"] == "ops-alice"
    assert new["is_active"] == 1
    # 重复执行调度不产生重复切换
    assert build(T_OCT2).activate_due_policies() == {"activated": [], "retired": []}


def test_repeated_publication_is_idempotent_and_conflicting_change_rejected(service):
    build = service
    v1, v2 = _publish_two_versions(build)
    repeat = build(T_SEP29).publish_policy(v2["id"], "ops-bob", "2026-10-01T00:00:00+00:00")
    assert repeat["id"] == v2["id"]
    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM operation_events WHERE resource_id=? AND event_type='published'", (v2["id"],)).fetchone()[0] == 1
    with pytest.raises(ConflictError):
        build(T_SEP29).publish_policy(v2["id"], "ops-bob", "2026-10-02T00:00:00+00:00")
    with pytest.raises(ConflictError):
        build(T_SEP29).publish_policy(v2["id"], "ops-carol", "2026-10-01T00:00:00+00:00")
    rules3 = {**DEFAULT_RULES, "score": {**DEFAULT_RULES["score"], "major_threshold": 1.6}}
    v3 = build(T_SEP29).create_policy("gdh-rail", rules3, "ops-carol")
    with pytest.raises(ConflictError):
        build(T_SEP29).publish_policy(v3["id"], "ops-carol", "2026-09-30T00:00:00+00:00")


def test_continuous_acceleration_service_across_switchover(service):
    build = service
    v1, v2 = _publish_two_versions(build)

    def entitlement(at, subscriber, order_id):
        build(at).add_entitlement({
            "subscriber_hash": subscriber, "scenario_code": "gdh-rail", "product_code": "rail-day",
            "valid_from": "2026-09-01T00:00:00+00:00", "valid_until": "2026-11-01T00:00:00+00:00", "source_order_id": order_id,
        })

    def degraded_sample(at, key, subscriber):
        return build(at).ingest_sample({
            "sample_key": key, "scenario_code": "gdh-rail", "segment_code": "gz-sz-01", "app_code": "video-call",
            "subscriber_hash": subscriber, "device_class": "phone", "train_speed_kmh": 300,
            "latency_ms": 500, "packet_loss": 0.2, "downlink_mbps": 1, "uplink_mbps": 0.2, "observed_at": _iso(at),
        })

    entitlement(T_SEP29, "subscriber-sep-0000000001", "order-sep")
    sample_before = degraded_sample(T_SEP29, "sample-sep", "subscriber-sep-0000000001")
    session_before = build(T_SEP29).start_acceleration(sample_before["incident_id"], "ops")
    connection = get_connection()
    assert connection.execute("SELECT policy_version_id FROM acceleration_sessions WHERE id=?", (session_before["id"],)).fetchone()[0] == v1["id"]

    # 跨过切换时刻，即使调度器未运行，读路径也会按注入时钟完成交接，服务不中断
    entitlement(T_OCT2, "subscriber-oct-0000000002", "order-oct")
    sample_after = degraded_sample(T_OCT2, "sample-oct", "subscriber-oct-0000000002")
    session_after = build(T_OCT2).start_acceleration(sample_after["incident_id"], "ops")
    assert connection.execute("SELECT policy_version_id FROM acceleration_sessions WHERE id=?", (session_after["id"],)).fetchone()[0] == v2["id"]
    assert build(T_OCT2).get_session(session_before["id"])["status"] == "active"


def test_database_forbids_two_simultaneously_active_versions(service):
    build = service
    v1, v2 = _publish_two_versions(build)
    build(T_OCT1).activate_due_policies()
    connection = get_connection()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE policy_versions SET is_active=1,state='published',retired_at=NULL WHERE id=?", (v1["id"],))
    connection.rollback()


def test_concurrent_publication_cannot_create_two_active_versions(service):
    build = service
    v1 = build(T_SEP20).create_policy("gdh-rail", DEFAULT_RULES, "ops-alice")
    build(T_SEP20).publish_policy(v1["id"], "ops-alice", "2026-09-26T00:00:00+00:00")
    rules_a = {**DEFAULT_RULES, "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 200}}
    rules_b = {**DEFAULT_RULES, "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 300}}
    va = build(T_SEP29).create_policy("gdh-rail", rules_a, "ops-a")
    vb = build(T_SEP29).create_policy("gdh-rail", rules_b, "ops-b")
    build(T_SEP29).publish_policy(va["id"], "ops-a", "2026-09-27T00:00:00+00:00")
    # 并发场景下第二个事务尝试相同生效时间会被拒绝，而不是产生两个同时生效版本
    with pytest.raises(ConflictError):
        build(T_SEP29).publish_policy(vb["id"], "ops-b", "2026-09-27T00:00:00+00:00")
    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM policy_versions WHERE scenario_id=1 AND is_active=1").fetchone()[0] == 1
    assert connection.execute("SELECT id FROM policy_versions WHERE scenario_id=1 AND is_active=1").fetchone()[0] == va["id"]
