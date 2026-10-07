"""业务域：租约 HTTP 接口端到端。

通过 FastAPI TestClient 走完整对外接口，验证：
- 申请携带 ttl、续租、到期回收接口的对外形态；
- 参数不合法统一为 400 INVALID_PARAM（pydantic 422 不外露）；
- 对不该续租的环境续租，错误码可区分：LEASE_EXPIRED / ENV_DESTROYED / LEASE_NOT_ACTIVE；
- 内部出错统一为 500 INTERNAL，异常细节（堆栈/地址/SQL 等）不泄露给调用方。
到期用“推进服务时钟 + 调用回收接口”确定性触发，不真实等待。
"""
import logging

import pytest
from fastapi.testclient import TestClient

import main as main_module
from app.errors import ErrorCode
from app.models import EnvStatus

log = logging.getLogger("test.api_lease")


@pytest.fixture
def client():
    # 每个用例使用全新的服务与可推进时钟，避免模块级单例互相干扰
    clock = {"now": 1000.0}
    main_module.service = main_module.EnvService(
        main_module.QuotaManager(per_user_limit=3, global_limit=6),
        clock=lambda: clock["now"],
    )
    return TestClient(main_module.app, raise_server_exceptions=False), clock


def _request(client, ttl=None):
    c, _ = client
    payload = {"owner": "alice", "idempotency_key": "k1"}
    if ttl is not None:
        payload["ttl_seconds"] = ttl
    resp = c.post("/envs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_request_with_ttl_view_fields(client):
    log.info("验证 对外申请携带 ttl：视图含 ttl_seconds/expires_at，状态 READY")
    c, clock = client
    body = _request(client, ttl=30)
    assert body["ttl_seconds"] == 30
    assert body["expires_at"] == clock["now"] + 30
    assert body["status"] == "READY"


def test_request_without_ttl_is_null(client):
    log.info("验证 不传 ttl：ttl_seconds/expires_at 均为 null（默认行为兼容旧用法）")
    c, _ = client
    body = _request(client)
    assert body["ttl_seconds"] is None and body["expires_at"] is None


def test_renew_endpoint_extends_lease(client):
    log.info("验证 续租接口返回新到期时间，且不新增环境")
    c, clock = client
    env = _request(client, ttl=30)
    clock["now"] += 10
    resp = c.post(
        f"/envs/{env['env_id']}/renew",
        params={"owner": "alice"},
        json={"ttl_seconds": 60},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expires_at"] == clock["now"] + 60
    assert body["status"] == "READY"
    quota = c.get("/quota", params={"owner": "alice"}).json()
    assert quota["user_used"] == 1  # 续租不多占额度


def test_reap_endpoint_reclaims_expired(client):
    log.info("验证 到期回收接口确定性回收并释放额度，重复调用为空")
    c, clock = client
    env = _request(client, ttl=30)
    clock["now"] += 30
    resp = c.post("/envs/reap-expired")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reaped": [env["env_id"]], "count": 1}
    got = c.get(f"/envs/{env['env_id']}", params={"owner": "alice"}).json()
    assert got["status"] == "EXPIRED"
    quota = c.get("/quota", params={"owner": "alice"}).json()
    assert quota["user_used"] == 0
    assert c.post("/envs/reap-expired").json() == {"reaped": [], "count": 0}


@pytest.mark.parametrize(
    "payload",
    [
        {"owner": "alice", "idempotency_key": "k1", "ttl_seconds": 0},
        {"owner": "alice", "idempotency_key": "k1", "ttl_seconds": -10},
        {"owner": "alice", "idempotency_key": "k1", "ttl_seconds": "half-hour"},
        {"owner": "alice", "idempotency_key": "k1", "ttl_seconds": 1.5},
        {"owner": "", "idempotency_key": "k1"},
        {"owner": "alice", "idempotency_key": ""},
    ],
)
def test_invalid_params_return_invalid_param(client, payload):
    log.info("验证 参数非法(%r) 统一为 400 INVALID_PARAM，不出现 422", payload)
    c, _ = client
    resp = c.post("/envs", json=payload)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == ErrorCode.INVALID_PARAM.value


def test_renew_invalid_ttl_returns_400(client):
    log.info("验证 续租接口非法时长统一 400 INVALID_PARAM")
    c, _ = client
    env = _request(client, ttl=30)
    for bad in (0, -1, "30s"):
        resp = c.post(
            f"/envs/{env['env_id']}/renew",
            params={"owner": "alice"},
            json={"ttl_seconds": bad},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == ErrorCode.INVALID_PARAM.value


def test_renew_expired_returns_lease_expired(client):
    log.info("验证 对到期回收环境续租：409 LEASE_EXPIRED")
    c, clock = client
    env = _request(client, ttl=30)
    clock["now"] += 31
    c.post("/envs/reap-expired")
    resp = c.post(
        f"/envs/{env['env_id']}/renew",
        params={"owner": "alice"},
        json={"ttl_seconds": 60},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == ErrorCode.LEASE_EXPIRED.value


def test_renew_destroyed_returns_env_destroyed(client):
    log.info("验证 对已销毁环境续租：409 ENV_DESTROYED，与 LEASE_EXPIRED 可区分")
    c, _ = client
    env = _request(client, ttl=30)
    assert c.delete(f"/envs/{env['env_id']}", params={"owner": "alice"}).status_code == 200
    resp = c.post(
        f"/envs/{env['env_id']}/renew",
        params={"owner": "alice"},
        json={"ttl_seconds": 60},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == ErrorCode.ENV_DESTROYED.value


def test_renew_non_lease_returns_lease_not_active(client):
    log.info("验证 对未指定租约环境续租：409 LEASE_NOT_ACTIVE，三种拒绝原因互不相同")
    c, _ = client
    env = _request(client)  # 不传 ttl
    resp = c.post(
        f"/envs/{env['env_id']}/renew",
        params={"owner": "alice"},
        json={"ttl_seconds": 60},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == ErrorCode.LEASE_NOT_ACTIVE.value


def test_renew_unknown_env_returns_not_found(client):
    log.info("验证 续租不存在/他人环境返回 404 ENV_NOT_FOUND")
    c, _ = client
    resp = c.post(
        "/envs/nope/renew",
        params={"owner": "alice"},
        json={"ttl_seconds": 60},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == ErrorCode.ENV_NOT_FOUND.value


def test_internal_error_hides_details(client, monkeypatch):
    log.info("验证 内部异常时对外只给 500 INTERNAL 通用文案，不泄露内部细节")
    c, _ = client

    def boom(_env_id, _owner, _ttl):
        raise RuntimeError("SecretFailure: disk /dev/sdx9 table corrupt at host 10.255.0.7")

    monkeypatch.setattr(main_module.service, "renew_lease", boom)
    env = _request(client, ttl=30)
    resp = c.post(
        f"/envs/{env['env_id']}/renew",
        params={"owner": "alice"},
        json={"ttl_seconds": 60},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "INTERNAL"
    assert body["message"] == "内部错误"
    leaked = ("SecretFailure", "/dev/sdx9", "10.255.0.7", "Traceback")
    assert not any(token in resp.text for token in leaked)


def test_expired_environment_is_terminal_over_api(client):
    log.info("验证 端到端：已回收环境续租/重试/销毁都拉不回，且额度已释放")
    c, clock = client
    env = _request(client, ttl=30)
    clock["now"] += 30
    c.post("/envs/reap-expired")
    renew = c.post(
        f"/envs/{env['env_id']}/renew",
        params={"owner": "alice"},
        json={"ttl_seconds": 60},
    )
    retry = c.post(f"/envs/{env['env_id']}/retry", params={"owner": "alice"})
    destroy = c.delete(f"/envs/{env['env_id']}", params={"owner": "alice"})
    assert renew.status_code == 409 and renew.json()["error"] == "LEASE_EXPIRED"
    assert retry.status_code == 409 and retry.json()["error"] == "LEASE_EXPIRED"
    assert destroy.status_code == 409 and destroy.json()["error"] == "LEASE_EXPIRED"
    status = c.get(f"/envs/{env['env_id']}", params={"owner": "alice"}).json()["status"]
    assert status == EnvStatus.EXPIRED.value
