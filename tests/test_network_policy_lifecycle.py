from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError
from app.database import get_connection
from app.network.rules import DEFAULT_RULES
from app.network.service import NetworkAccelerationService

T0 = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
SWITCH = "2026-10-01T00:00:00+00:00"
LATER = "2026-11-01T00:00:00+00:00"

V2_RULES = {
    "score": {**DEFAULT_RULES["score"], "critical_threshold": 6.0},
    "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 240},
}
V3_RULES = {
    "score": {**DEFAULT_RULES["score"], "major_threshold": 2.0},
    "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 300},
}
V4_RULES = {
    "score": {**DEFAULT_RULES["score"], "major_threshold": 1.2},
    "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 360},
}


def scenario_payload():
    return {
        "code": "gdh-rail",
        "name": "广深高铁",
        "scene_type": "railway",
        "timezone": "Asia/Shanghai",
        "max_concurrent_sessions": 10,
        "capacity_mbps": 3000,
    }


def app_payload():
    return {
        "app_code": "video-call",
        "name": "视频通话",
        "category": "video_call",
        "latency_target_ms": 100,
        "packet_loss_target": 0.01,
        "min_downlink_mbps": 8,
        "min_uplink_mbps": 4,
        "default_priority": 70,
    }


def sample_payload(key="sample-000001", observed="2026-09-30T08:05:00+00:00"):
    return {
        "sample_key": key,
        "scenario_code": "gdh-rail",
        "app_code": "video-call",
        "subscriber_hash": "subscriber-000000000001",
        "device_class": "phone",
        "train_speed_kmh": 300,
        "latency_ms": 350,
        "packet_loss": 0.08,
        "downlink_mbps": 1.5,
        "uplink_mbps": 0.5,
        "observed_at": observed,
    }


def entitlement_payload(order="order-000001"):
    return {
        "subscriber_hash": "subscriber-000000000001",
        "scenario_code": "gdh-rail",
        "product_code": "rail-boost-day",
        "valid_from": "2026-09-01T00:00:00+00:00",
        "valid_until": "2026-10-31T23:59:59+00:00",
        "source_order_id": order,
    }


def make_service(clock) -> NetworkAccelerationService:
    return NetworkAccelerationService(get_connection(), clock)


def setup_service(service: NetworkAccelerationService) -> None:
    service.create_scenario(scenario_payload())
    service.create_application(app_payload())
    service.add_entitlement(entitlement_payload())


def setup_api(client) -> None:
    assert client.post("/api/network/scenarios", json=scenario_payload()).status_code == 201
    assert client.post("/api/network/applications", json=app_payload()).status_code == 201
    assert client.post("/api/network/entitlements", json=entitlement_payload()).status_code == 201


def test_future_scheduled_policy_does_not_interrupt_current_service(client):
    """国庆事故回归：预发布未来版本不能把当前版本立刻退役。"""
    setup_api(client)
    v1 = client.post("/api/network/scenarios/gdh-rail/policies", json={"rules": DEFAULT_RULES, "actor": "planner"}).json()
    published = client.post(f"/api/network/policies/{v1['id']}/publish", json={"actor": "planner", "effective_from": "2026-01-01T00:00:00Z"})
    assert published.status_code == 200
    v2 = client.post("/api/network/scenarios/gdh-rail/policies", json={"rules": V2_RULES, "actor": "planner"}).json()
    scheduled = client.post(f"/api/network/policies/{v2['id']}/publish", json={"actor": "national-day-ops", "effective_from": "2099-10-01T00:00:00Z"})
    assert scheduled.status_code == 200

    effective = client.get("/api/network/scenarios/gdh-rail/policies/effective")
    assert effective.status_code == 200
    assert effective.json()["id"] == v1["id"]
    items = client.get("/api/network/scenarios/gdh-rail/policies").json()["items"]
    lifecycle = {item["id"]: item["lifecycle"] for item in items}
    assert lifecycle[v1["id"]] == "active"
    assert lifecycle[v2["id"]] == "scheduled"

    sample = client.post("/api/network/samples", json=sample_payload())
    assert sample.status_code == 202, sample.text
    assert sample.json()["incident_id"] is not None
    started = client.post(f"/api/network/incidents/{sample.json()['incident_id']}/accelerate", json={"actor": "duty"})
    assert started.status_code == 200, started.text
    assert started.json()["policy_version_id"] == v1["id"]

    missing = client.get("/api/network/scenarios/gdh-rail/policies/effective", params={"at": "2020-01-01T00:00:00Z"})
    assert missing.status_code == 404


def test_versions_hand_over_at_switch_time(client):
    """可注入时钟跨过切换边界：样本判定、加速与版本历史连续且准确。"""
    clock = FrozenClock(T0)
    service = make_service(clock)
    setup_service(service)
    v1 = service.create_policy("gdh-rail", DEFAULT_RULES, "planner")
    v1 = service.publish_policy(v1["id"], "planner", to_storage(T0))
    v2 = service.create_policy("gdh-rail", V2_RULES, "planner")
    v2 = service.publish_policy(v2["id"], "national-day-ops", SWITCH)

    # 已发布待生效：当前版本继续服役，交棒信息已记录
    assert service.effective_policy_at("gdh-rail")["id"] == v1["id"]
    history = {item["id"]: item for item in service.list_policies("gdh-rail")}
    assert history[v1["id"]]["lifecycle"] == "active"
    assert history[v1["id"]]["retired_at"] == SWITCH
    assert history[v1["id"]]["retired_by"] == "national-day-ops"
    assert history[v2["id"]]["lifecycle"] == "scheduled"

    # 切换前：样本按旧版本判定，加速使用旧版本资源参数
    before = service.ingest_sample(sample_payload())
    assert before["quality"]["severity"] == "critical"
    session1 = service.start_acceleration(before["incident_id"], "duty")
    assert session1["policy_version_id"] == v1["id"]
    assert from_storage(session1["expires_at"]) - from_storage(session1["started_at"]) == timedelta(seconds=180)

    # 跨过切换边界：新版本接管，服务不中断
    clock.current = datetime(2026, 10, 1, 0, 0, 1, tzinfo=UTC)
    after = service.ingest_sample(sample_payload("sample-000002", "2026-10-01T00:00:01+00:00"))
    assert after["quality"]["severity"] == "major"
    session2 = service.start_acceleration(after["incident_id"], "duty")
    assert session2["policy_version_id"] == v2["id"]
    assert from_storage(session2["expires_at"]) - from_storage(session2["started_at"]) == timedelta(seconds=240)

    # 版本历史：旧版本留下完整退役时间与发布人
    history = {item["id"]: item for item in service.list_policies("gdh-rail")}
    old, new = history[v1["id"]], history[v2["id"]]
    assert old["state"] == "retired"
    assert old["lifecycle"] == "retired"
    assert old["retired_at"] == SWITCH
    assert old["retired_by"] == "national-day-ops"
    assert old["published_by"] == "planner"
    assert new["lifecycle"] == "active"
    assert new["published_by"] == "national-day-ops"

    # 任意时刻查询只选择当时有效的版本
    assert service.effective_policy_at("gdh-rail")["id"] == v2["id"]
    assert service.effective_policy_at("gdh-rail", "2026-09-30T23:59:59+00:00")["id"] == v1["id"]
    assert service.effective_policy_at("gdh-rail", SWITCH)["id"] == v2["id"]
    with pytest.raises(NotFoundError):
        service.effective_policy_at("gdh-rail", "2026-09-30T07:59:59+00:00")


def test_publish_idempotency_chaining_and_preemption(client):
    clock = FrozenClock(T0)
    service = make_service(clock)
    setup_service(service)
    v1 = service.create_policy("gdh-rail", DEFAULT_RULES, "planner")
    v1 = service.publish_policy(v1["id"], "planner", to_storage(T0))

    # 重复发布幂等返回，不制造额外版本
    again = service.publish_policy(v1["id"], "planner", "2026-09-30T07:00:00+00:00")
    assert again["id"] == v1["id"]
    assert again["state"] == "published"

    v2 = service.create_policy("gdh-rail", V2_RULES, "planner")
    service.publish_policy(v2["id"], "ops", SWITCH)
    v3 = service.create_policy("gdh-rail", V3_RULES, "planner")
    # 相同切换时刻冲突
    with pytest.raises(ConflictError):
        service.publish_policy(v3["id"], "ops", SWITCH)
    # 更晚的切换时刻：v2 先接管，到点再交棒给 v3
    service.publish_policy(v3["id"], "ops", LATER)
    history = {item["id"]: item for item in service.list_policies("gdh-rail")}
    assert history[v2["id"]]["state"] == "published"
    assert history[v2["id"]]["retired_at"] == LATER
    assert history[v2["id"]]["retired_by"] == "ops"
    # 已发布版本不能改期
    with pytest.raises(ConflictError):
        service.publish_policy(v2["id"], "ops", LATER)

    # 立即发布 v4：抢占全部待生效版本，当前版本即刻退役
    v4 = service.create_policy("gdh-rail", V4_RULES, "planner")
    service.publish_policy(v4["id"], "duty", to_storage(clock.now()))
    history = {item["id"]: item for item in service.list_policies("gdh-rail")}
    assert history[v1["id"]]["state"] == "retired"
    assert history[v1["id"]]["retired_at"] == to_storage(T0)
    assert history[v1["id"]]["retired_by"] == "duty"
    assert history[v2["id"]]["state"] == "retired"
    assert history[v3["id"]]["state"] == "retired"
    assert history[v4["id"]]["lifecycle"] == "active"
    assert service.effective_policy_at("gdh-rail")["id"] == v4["id"]

    # 已退役策略不能发布
    with pytest.raises(ConflictError):
        service.publish_policy(v1["id"], "ops", SWITCH)


def test_no_two_versions_effective_at_any_moment(client):
    clock = FrozenClock(T0)
    service = make_service(clock)
    setup_service(service)
    v1 = service.create_policy("gdh-rail", DEFAULT_RULES, "planner")
    v1 = service.publish_policy(v1["id"], "planner", to_storage(T0))
    v2 = service.create_policy("gdh-rail", V2_RULES, "planner")
    service.publish_policy(v2["id"], "ops", SWITCH)
    v3 = service.create_policy("gdh-rail", V3_RULES, "planner")
    service.publish_policy(v3["id"], "ops", LATER)

    # 逐小时扫描：覆盖区间内任意时刻恰好一个生效版本
    expected = [(T0, v1["id"]), (datetime(2026, 10, 1, tzinfo=UTC), v2["id"]), (datetime(2026, 11, 1, tzinfo=UTC), v3["id"])]
    moment = T0
    while moment < datetime(2026, 11, 2, tzinfo=UTC):
        policy = service.effective_policy_at("gdh-rail", to_storage(moment))
        want = max(start for start, _ in expected if start <= moment)
        assert policy["id"] == dict(expected)[want], moment
        moment += timedelta(hours=1)

    # SQL 层面不存在重叠窗口
    overlap = get_connection().execute(
        "SELECT COUNT(*) FROM policy_versions a JOIN policy_versions b "
        "ON a.scenario_id=b.scenario_id AND a.id<b.id "
        "WHERE a.effective_from IS NOT NULL AND b.effective_from IS NOT NULL "
        "AND a.effective_from < COALESCE(b.retired_at,'9999-12-31') "
        "AND b.effective_from < COALESCE(a.retired_at,'9999-12-31')"
    ).fetchone()[0]
    assert overlap == 0


def test_concurrent_immediate_publish_keeps_single_active_version(client):
    clock = FrozenClock(T0)
    service = make_service(clock)
    setup_service(service)
    drafts = [
        service.create_policy("gdh-rail", rules, "planner")["id"]
        for rules in (DEFAULT_RULES, V2_RULES, V3_RULES, V4_RULES)
    ]
    barrier = threading.Barrier(len(drafts))
    errors: list[Exception] = []

    def worker(policy_id: int) -> None:
        threaded = NetworkAccelerationService(clock=clock)
        barrier.wait()
        try:
            threaded.publish_policy(policy_id, "concurrent", to_storage(T0))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(policy_id,)) for policy_id in drafts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    active = [item for item in service.list_policies("gdh-rail") if item["lifecycle"] == "active"]
    assert len(active) == 1
    assert service.effective_policy_at("gdh-rail")["id"] == active[0]["id"]


def test_concurrent_same_instant_schedule_conflicts(client):
    clock = FrozenClock(T0)
    service = make_service(clock)
    setup_service(service)
    first = service.create_policy("gdh-rail", DEFAULT_RULES, "planner")["id"]
    second = service.create_policy("gdh-rail", V2_RULES, "planner")["id"]
    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {"published": 0, "conflict": 0}

    def worker(policy_id: int) -> None:
        threaded = NetworkAccelerationService(clock=clock)
        barrier.wait()
        try:
            threaded.publish_policy(policy_id, "concurrent", SWITCH)
            outcomes["published"] += 1
        except ConflictError:
            outcomes["conflict"] += 1

    threads = [threading.Thread(target=worker, args=(policy_id,)) for policy_id in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes == {"published": 1, "conflict": 1}
    assert service.effective_policy_at("gdh-rail", SWITCH)["id"] in {first, second}
