"""业务域：租约能力 HTTP 端到端。

通过 FastAPI TestClient 走对外接口，验证：
- 申请带 ttl、查询返回租约字段、续租延后、到期确定性回收释放额度；
- 参数非法（ttl/续租时长）由接口层 400 拒绝；
- 对不该续租的环境续租（已回收/已销毁/无租约/不存在）返回可区分 error；
- 内部异常统一 500 INTERNAL，不泄露堆栈细节；
- 到期触发确定性：TTL 与回收间隔都设为毫秒级，无需真实长等。
"""
import logging
import time

import pytest
from fastapi.testclient import TestClient

from app.errors import Rejection
from app.quota import QuotaManager
from app.service import EnvService
from main import create_app

log = logging.getLogger("test.api_lease")


@pytest.fixture
def client():
    # ttl 最小可设到毫秒级，回收间隔 0.01s：到期验证确定性且不依赖长时间等待
    svc = EnvService(QuotaManager(per_user_limit=3, global_limit=10))
    application = create_app(svc, reap_interval=0.01)
    # raise_server_exceptions=False：让未预期异常走 500 INTERNAL 处理器，
    # 从而能在端到端层验证内部异常不外泄（而不是被测试客户端重新抛出）
    with TestClient(application, raise_server_exceptions=False) as c:
        c.svc = svc
        yield c
    svc.stop_reaper()


def _request(client, ttl=None):
    body = {"owner": "alice", "idempotency_key": f"k-{time.time_ns()}"}
    if ttl is not None:
        body["ttl_seconds"] = ttl
    resp = client.post("/envs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_request_with_ttl_and_query_e2e(client):
    log.info("E2E 验证 带 ttl 申请返回租约字段，GET 可查到一致内容")
    data = _request(client, ttl=30)
    assert data["status"] == "READY"
    assert data["ttl_seconds"] == 30
    assert data["lease_expires_at"] is not None

    resp = client.get(f"/envs/{data['env_id']}", params={"owner": "alice"})
    assert resp.status_code == 200
    assert resp.json()["lease_expires_at"] == data["lease_expires_at"]


def test_renew_and_automatic_reap_e2e(client):
    log.info("E2E 验证 续租成功后延；短租约环境被后台确定性回收并释放额度")
    keep = _request(client, ttl=600)
    short = _request(client, ttl=0.02)

    # 续租长租约：到期时间后移，不新增额度占用
    resp = client.post(
        f"/envs/{keep['env_id']}/renew",
        json={"owner": "alice", "extend_seconds": 600},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["lease_expires_at"] > keep["lease_expires_at"]

    # 额度：续租不重复占，仍是 2
    quota = client.get("/quota", params={"owner": "alice"}).json()
    assert quota["user_used"] == 2

    # 确定性等待后台回收（毫秒级，不长等）
    deadline = time.time() + 2.0
    while time.time() < deadline:
        got = client.get(f"/envs/{short['env_id']}", params={"owner": "alice"}).json()
        if got["status"] == "EXPIRED":
            break
        time.sleep(0.005)
    assert got["status"] == "EXPIRED"
    quota = client.get("/quota", params={"owner": "alice"}).json()
    assert quota["user_used"] == 1  # 回收释放了短租约的额度

    # 手动 /reap 重复触发也是幂等的
    assert client.post("/reap").json()["reaped"] == 0


def test_invalid_params_e2e(client):
    log.info("E2E 验证 ttl<=0、续租时长<=0 返回 400 INVALID_PARAM")
    r = client.post("/envs", json={"owner": "alice", "idempotency_key": "k1",
                                   "ttl_seconds": 0})
    assert r.status_code == 400 and r.json()["error"] == "INVALID_PARAM"

    env = _request(client, ttl=600)
    r = client.post(f"/envs/{env['env_id']}/renew",
                    json={"owner": "alice", "extend_seconds": -1})
    assert r.status_code == 400 and r.json()["error"] == "INVALID_PARAM"


def test_renew_rejections_are_distinguishable_e2e(client):
    log.info("E2E 验证 已回收/已销毁/无租约/不存在 的续租返回彼此可区分的 error")
    expired = _request(client, ttl=0.02)
    destroyed = _request(client, ttl=600)
    no_lease = _request(client)

    # 等短租约回收
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if client.get(f"/envs/{expired['env_id']}",
                      params={"owner": "alice"}).json()["status"] == "EXPIRED":
            break
        time.sleep(0.005)

    client.delete(f"/envs/{destroyed['env_id']}", params={"owner": "alice"})

    cases = [
        (expired["env_id"], "LEASE_EXPIRED", 409),
        (destroyed["env_id"], "ENV_DESTROYED", 409),
        (no_lease["env_id"], "LEASE_NOT_RENEWABLE", 409),
        ("missing-id", "ENV_NOT_FOUND", 404),
    ]
    for env_id, expect_error, expect_status in cases:
        r = client.post(f"/envs/{env_id}/renew",
                        json={"owner": "alice", "extend_seconds": 60})
        assert r.status_code == expect_status, (env_id, r.text)
        assert r.json()["error"] == expect_error, (env_id, r.json())
        log.info("续租拒绝区分: env=%s -> %s", env_id, expect_error)


def test_internal_error_not_leaked_e2e(client):
    log.info("E2E 验证 服务内部异常统一 500 INTERNAL，不泄露异常细节")
    env = _request(client, ttl=600)

    def boom(*_args, **_kwargs):
        raise RuntimeError("secret-internal: traceback line / 10.0.0.9 stack")

    client.svc.renew = boom  # 模拟未预期的内部异常
    r = client.post(f"/envs/{env['env_id']}/renew",
                    json={"owner": "alice", "extend_seconds": 60})
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "INTERNAL"
    assert "10.0.0.9" not in r.text and "traceback" not in r.text
    assert "secret-internal" not in r.text
