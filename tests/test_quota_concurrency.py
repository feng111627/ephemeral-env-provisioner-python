"""业务域：额度规则与并发申请。

覆盖：单用户上限、全局上限、并发不超发不少算、非法参数、
幂等重复提交不重复占额度、销毁后额度回收。
"""
import logging
import threading

import pytest

from app.errors import ErrorCode, Rejection
from app.quota import QuotaManager
from app.service import EnvService

log = logging.getLogger("test.quota")


def test_per_user_limit_enforced(service):
    log.info("验证 单用户额度上限=2，第 3 个申请被拒绝且原因可区分")
    service.request_env("alice", "k1")
    service.request_env("alice", "k2")
    with pytest.raises(Rejection) as exc:
        service.request_env("alice", "k3")
    assert exc.value.code is ErrorCode.QUOTA_EXCEEDED_USER
    assert service.usage("alice")["user_used"] == 2  # 拒绝不产生计数变化


def test_global_limit_enforced():
    svc = EnvService(QuotaManager(per_user_limit=3, global_limit=3))
    log.info("验证 全局额度上限=3，多用户合计超限被拒绝")
    svc.request_env("u1", "k1")
    svc.request_env("u2", "k1")
    svc.request_env("u3", "k1")
    with pytest.raises(Rejection) as exc:
        svc.request_env("u4", "k1")
    assert exc.value.code is ErrorCode.QUOTA_EXCEEDED_GLOBAL
    assert svc.usage("u4")["global_used"] == 3


def test_invalid_params_rejected(service):
    log.info("验证 空申请人/空幂等键返回 INVALID_PARAM")
    with pytest.raises(Rejection) as exc:
        service.request_env("", "k1")
    assert exc.value.code is ErrorCode.INVALID_PARAM
    with pytest.raises(Rejection) as exc:
        service.request_env("alice", "  ")
    assert exc.value.code is ErrorCode.INVALID_PARAM


def test_idempotent_resubmit_no_double_quota(service):
    log.info("验证 相同 (owner, 幂等键) 重复提交返回同一环境且只占 1 份额度")
    e1 = service.request_env("alice", "same-key")
    for _ in range(5):
        e = service.request_env("alice", "same-key")
        assert e.env_id == e1.env_id
    assert service.usage("alice")["user_used"] == 1


def test_destroy_reclaims_quota(service):
    log.info("验证 销毁后额度回收，可再次申请")
    e1 = service.request_env("alice", "k1")
    service.request_env("alice", "k2")
    service.destroy(e1.env_id, "alice")
    assert service.usage("alice")["user_used"] == 1
    service.request_env("alice", "k3")  # 回收后可再申请
    assert service.usage("alice")["user_used"] == 2


def test_concurrent_requests_never_exceed_quota():
    log.info("验证 20 线程并发申请，全局=5 单用户=2，成功数与额度计数精确一致")
    svc = EnvService(QuotaManager(per_user_limit=2, global_limit=5))
    barrier = threading.Barrier(20)
    ok, rejected = [], []

    def worker(i):
        barrier.wait()
        try:
            svc.request_env(f"user-{i}", "k1")
            ok.append(i)
        except Rejection as r:
            rejected.append(r.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    usage = svc.usage("anyone")
    log.info("并发结果: 成功=%d 拒绝=%d 全局计数=%d", len(ok), len(rejected), usage["global_used"])
    assert len(ok) == 5                       # 不超发
    assert usage["global_used"] == 5          # 不少算
    assert len(ok) + len(rejected) == 20      # 不多算
    assert all(c is ErrorCode.QUOTA_EXCEEDED_GLOBAL for c in rejected)


def test_concurrent_same_user_never_exceed_per_user_limit():
    log.info("验证 同一用户 10 线程并发申请，最多成功 2 个（单用户上限）")
    svc = EnvService(QuotaManager(per_user_limit=2, global_limit=100))
    barrier = threading.Barrier(10)
    ok = []

    def worker(i):
        barrier.wait()
        try:
            svc.request_env("alice", f"key-{i}")
            ok.append(i)
        except Rejection:
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 2
    assert svc.usage("alice")["user_used"] == 2
