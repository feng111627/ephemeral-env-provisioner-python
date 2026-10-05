"""业务：HTTP API 层的可区分拒绝结果与内部异常屏蔽。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import create_app
from app.errors import RejectionReason
from app.service import EnvService


def make_client(**kwargs) -> TestClient:
    return TestClient(create_app(EnvService(**kwargs)), raise_server_exceptions=False)


def test_apply_get_destroy_flow() -> None:
    client = make_client(per_user_quota=2, global_quota=4)
    resp = client.post("/envs", json={"owner": "alice", "request_id": "r1"})
    assert resp.status_code == 201
    env = resp.json()
    assert env["status"] == "ready"

    # 幂等：重复提交返回同一环境
    again = client.post("/envs", json={"owner": "alice", "request_id": "r1"})
    assert again.json()["env_id"] == env["env_id"]

    assert client.get(f"/envs/{env['env_id']}").json()["status"] == "ready"

    destroyed = client.post(
        f"/envs/{env['env_id']}/destroy", json={"operator": "alice"}
    )
    assert destroyed.json()["status"] == "destroyed"


def test_distinct_rejection_reasons_over_http() -> None:
    client = make_client(per_user_quota=1, global_quota=1)
    client.post("/envs", json={"owner": "alice", "request_id": "r1"})

    # 额度不足 -> 409 + quota_exceeded_user
    quota = client.post("/envs", json={"owner": "alice", "request_id": "r2"})
    assert quota.status_code == 409
    assert quota.json()["reason"] == RejectionReason.QUOTA_EXCEEDED_USER.value

    # 非法参数 -> 422（pydantic）或 400
    bad = client.post("/envs", json={"owner": "", "request_id": "r3"})
    assert bad.status_code in (400, 422)

    # 不存在 -> 404
    missing = client.get("/envs/nope")
    assert missing.status_code == 404
    assert missing.json()["reason"] == RejectionReason.NOT_FOUND.value

    # 非属主销毁 -> 403
    env_id = client.post(
        "/envs", json={"owner": "alice", "request_id": "r1"}
    ).json()["env_id"]
    forbidden = client.post(
        f"/envs/{env_id}/destroy",
        json={"operator": "mallory"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["reason"] == RejectionReason.FORBIDDEN.value


def test_internal_error_not_exposed_over_http() -> None:
    def boom(env) -> None:
        raise RuntimeError("secret backend detail")

    client = TestClient(
        create_app(EnvService(provisioner=boom)), raise_server_exceptions=False
    )
    resp = client.post("/envs", json={"owner": "alice", "request_id": "r1"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["reason"] == RejectionReason.INTERNAL.value
    assert "secret backend detail" not in str(body)
