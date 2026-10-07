"""业务域：分配失败回滚与重试、内部异常隔离。

覆盖：分配失败释放额度并回滚状态、重试成功、重试时额度不足、
非失败状态不允许重试、内部异常不泄露给调用方、并发重复分配防护。
"""
import logging
import threading

import pytest

from app.errors import ErrorCode, Rejection
from app.models import EnvStatus
from app.quota import QuotaManager
from app.service import EnvService

log = logging.getLogger("test.retry")


def _flaky_service(fail_times: int) -> tuple[EnvService, dict]:
    calls = {"n": 0}

    def allocator(env):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise RuntimeError("simulated-internal-detail: db conn 10.0.0.1 refused")
        return f"res-{env.env_id}"

    return EnvService(QuotaManager(per_user_limit=2, global_limit=4), allocator=allocator), calls


def test_allocate_failure_releases_quota_and_allows_retry():
    log.info("验证 首次分配失败 -> 回滚 REQUESTED + 释放额度 -> 重试成功")
    svc, calls = _flaky_service(fail_times=1)
    env = svc.request_env("alice", "k1")
    assert env.status is EnvStatus.REQUESTED          # 回滚到可申请状态
    assert env.failure_reason == "ALLOCATE_FAILED"    # 失败原因可区分
    assert svc.usage("alice")["user_used"] == 0       # 已占资源被释放
    env = svc.retry(env.env_id, "alice")
    assert env.status is EnvStatus.READY
    assert svc.usage("alice")["user_used"] == 1
    assert calls["n"] == 2


def test_persistent_failure_keeps_quota_clean():
    log.info("验证 持续失败不累积额度泄漏，每次失败都回滚")
    svc, calls = _flaky_service(fail_times=99)
    env = svc.request_env("alice", "k1")
    for _ in range(3):
        svc.retry(env.env_id, "alice")  # retry 不抛异常，而是回滚状态
        assert svc.usage("alice")["user_used"] == 0
        assert env.status is EnvStatus.REQUESTED
    assert calls["n"] == 4


def test_retry_rejected_when_not_in_requested(service):
    log.info("验证 就绪状态不允许重试，返回 ILLEGAL_TRANSITION")
    env = service.request_env("alice", "k1")
    assert env.status is EnvStatus.READY
    with pytest.raises(Rejection) as exc:
        service.retry(env.env_id, "alice")
    assert exc.value.code is ErrorCode.ILLEGAL_TRANSITION


def test_retry_respects_quota():
    log.info("验证 重试时额度不足返回 QUOTA_EXCEEDED，状态保持 REQUESTED 可再次重试")
    svc, _ = _flaky_service(fail_times=1)
    env = svc.request_env("alice", "k1")   # 失败，额度已释放
    assert svc.usage("alice")["user_used"] == 0
    svc.request_env("alice", "k2")         # 占满 alice 的 2 份额度
    svc.request_env("alice", "k3")
    with pytest.raises(Rejection) as exc:
        svc.retry(env.env_id, "alice")
    assert exc.value.code is ErrorCode.QUOTA_EXCEEDED_USER
    assert env.status is EnvStatus.REQUESTED


def test_internal_exception_not_exposed():
    log.info("验证 分配器内部异常细节不出现在对外的失败原因中")
    svc, _ = _flaky_service(fail_times=1)
    env = svc.request_env("alice", "k1")
    assert env.failure_reason == "ALLOCATE_FAILED"
    assert "10.0.0.1" not in (env.failure_reason or "")
    assert "db conn" not in (env.failure_reason or "")


def test_destroy_after_failed_allocation_does_not_double_release():
    log.info("回归：分配失败(额度已释放)后再销毁，不能重复 release 导致计数错乱")
    svc, _ = _flaky_service(fail_times=1)
    env = svc.request_env("alice", "k1")
    other = svc.request_env("alice", "k2")  # 此时 alice 占 1 份额度
    assert svc.usage("alice")["user_used"] == 1
    svc.destroy(env.env_id, "alice")  # 失败停留在 REQUESTED 的环境已不占额度
    assert svc.usage("alice")["user_used"] == 1  # 另一个环境的额度不被连带多放
    assert other.status is EnvStatus.READY


def test_concurrent_allocate_same_env_only_once():
    log.info("验证 并发对同一环境触发分配，分配器只被调用一次")
    calls = {"n": 0}
    lock = threading.Lock()

    def slow_allocator(env):
        with lock:
            calls["n"] += 1
        return f"res-{env.env_id}"

    svc = EnvService(QuotaManager(per_user_limit=2, global_limit=4), allocator=slow_allocator)
    env = svc.request_env("alice", "k1")
    baseline = calls["n"]  # request_env 已完成首次分配
    # 人为把环境拨回 REQUESTED 后并发触发 _allocate，模拟并发重复分配
    env.status = EnvStatus.REQUESTED
    barrier = threading.Barrier(8)

    def do_alloc():
        barrier.wait()
        svc._allocate(env)

    threads = [threading.Thread(target=do_alloc) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == baseline + 1  # 8 个并发线程只触发 1 次真实分配
    assert env.status is EnvStatus.READY
