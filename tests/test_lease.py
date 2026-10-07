"""业务域：租约（到期回收 / 续租 / 并发竞态）。

覆盖：
- 指定时长申请、不指定时长保持永久（旧用法兼容）；
- 到期确定性回收、释放额度、终态保护；
- 回收重复触发/与销毁并发只生效一次，额度只放开一次；
- 续租延后到期时间且不重复占额度；多次续租；
- 对已回收/已销毁/未就绪/无租约环境续租分别给出可区分原因；
- 续租与到期回收并发：要么续租成功要么被回收，二者不会同时发生；
- 高并发交错下额度计数与实际持有额度的环境严格一致。
"""
import logging
import threading

import pytest

from app.errors import ErrorCode, Rejection
from app.models import EnvStatus
from app.quota import QuotaManager
from app.service import EnvService

log = logging.getLogger("test.lease")


class FakeClock:
    """可手动推进的时钟，保证到期验证确定性、不依赖真实等待。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


@pytest.fixture
def svc():
    return EnvService(QuotaManager(per_user_limit=3, global_limit=10), clock=FakeClock())


def test_request_with_ttl_sets_expiry(svc):
    log.info("验证 指定 ttl=30 申请后记录到期时间，READY 且占用 1 份额度")
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    assert env.status is EnvStatus.READY
    assert env.ttl_seconds == 30
    assert env.lease_expires_at == env.created_at + 30
    assert svc.usage("alice")["user_used"] == 1


def test_request_without_ttl_keeps_legacy_behavior(svc):
    log.info("验证 不指定时长 -> 无到期时间，时间再怎么推进都不会被回收")
    env = svc.request_env("alice", "k1")
    assert env.ttl_seconds is None and env.lease_expires_at is None
    svc._clock.advance(10_000_000)
    assert svc.reap_expired() == 0
    assert env.status is EnvStatus.READY
    assert svc.usage("alice")["user_used"] == 1


def test_invalid_ttl_rejected(svc):
    log.info("验证 ttl<=0 返回 INVALID_PARAM 且不产生环境/额度变化")
    for bad in (0, -10):
        with pytest.raises(Rejection) as exc:
            svc.request_env("alice", f"k{bad}", ttl_seconds=bad)
        assert exc.value.code is ErrorCode.INVALID_PARAM
    assert svc.usage("alice")["user_used"] == 0


def test_expiry_reclaims_env_and_quota(svc):
    log.info("验证 到期 -> reap 收进 EXPIRED 终态并释放额度，回收后可再申请")
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    svc.request_env("alice", "k2", ttl_seconds=60)
    svc._clock.advance(31)
    reaped = svc.reap_expired()
    assert reaped == 1
    assert env.status is EnvStatus.EXPIRED
    assert env.history[-1].startswith("EXPIRED@")
    assert "LEASE_EXPIRED" in env.history[-1]
    assert svc.usage("alice")["user_used"] == 1
    svc.request_env("alice", "k3", ttl_seconds=30)  # 额度已释放
    assert svc.usage("alice")["user_used"] == 2


def test_reap_is_idempotent_and_quota_released_once(svc):
    log.info("验证 重复回收不再生效、额度只放开一次（不会多放成负数）")
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    svc._clock.advance(11)
    assert svc.reap_expired() == 1
    for _ in range(5):
        assert svc.reap_expired() == 0
    assert env.status is EnvStatus.EXPIRED
    assert svc.usage("alice")["user_used"] == 0
    assert svc.usage("alice")["global_used"] == 0


def test_expired_is_terminal(svc):
    log.info("验证 已回收是终态：续租/重试/销毁/再次回收全部拒绝或无效")
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    svc._clock.advance(11)
    svc.reap_expired()
    with pytest.raises(Rejection) as exc:
        svc.renew(env.env_id, "alice", 10)
    assert exc.value.code is ErrorCode.LEASE_EXPIRED
    with pytest.raises(Rejection) as exc:
        svc.retry(env.env_id, "alice")
    assert exc.value.code is ErrorCode.ILLEGAL_TRANSITION
    with pytest.raises(Rejection) as exc:
        svc.destroy(env.env_id, "alice")
    assert exc.value.code is ErrorCode.ILLEGAL_TRANSITION
    assert svc.reap_expired() == 0


def test_renew_extends_without_extra_quota(svc):
    log.info("验证 续租只后移到期时间，可多次续租，额度始终为 1")
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    old_expiry = env.lease_expires_at
    svc._clock.advance(10)
    svc.renew(env.env_id, "alice", 30)
    assert env.lease_expires_at == old_expiry + 30  # 从原到期时间顺延
    assert any(h.startswith("RENEWED@") for h in env.history)
    for _ in range(3):
        svc.renew(env.env_id, "alice", 60)
    assert env.lease_expires_at == old_expiry + 30 + 180
    assert svc.usage("alice")["user_used"] == 1
    # 续租后在原到期时间不应被回收
    svc._clock.advance(25)
    assert svc.reap_expired() == 0
    assert env.status is EnvStatus.READY


def test_renew_invalid_param(svc):
    log.info("验证 续租时长为空/非正数返回 INVALID_PARAM")
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    for bad in (0, -5):
        with pytest.raises(Rejection) as exc:
            svc.renew(env.env_id, "alice", bad)
        assert exc.value.code is ErrorCode.INVALID_PARAM


def test_renew_rejected_with_distinct_reasons(svc):
    log.info("验证 对已销毁/无租约/未就绪环境续租给出可区分原因")
    # 已销毁
    e1 = svc.request_env("alice", "k1", ttl_seconds=30)
    svc.destroy(e1.env_id, "alice")
    with pytest.raises(Rejection) as exc:
        svc.renew(e1.env_id, "alice", 10)
    assert exc.value.code is ErrorCode.ENV_DESTROYED

    # 无租约（永久保留）
    e2 = svc.request_env("alice", "k2")
    with pytest.raises(Rejection) as exc:
        svc.renew(e2.env_id, "alice", 10)
    assert exc.value.code is ErrorCode.LEASE_NOT_RENEWABLE

    # 环境不存在 / 非本人
    with pytest.raises(Rejection) as exc:
        svc.renew("nope", "alice", 10)
    assert exc.value.code is ErrorCode.ENV_NOT_FOUND


def test_renew_rejected_when_not_ready():
    log.info("验证 分配失败停在 REQUESTED 的环境续租返回 ENV_NOT_READY")
    def allocator(env):
        raise RuntimeError("boom-internal")

    svc = EnvService(QuotaManager(2, 4), allocator=allocator, clock=FakeClock())
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    assert env.status is EnvStatus.REQUESTED
    with pytest.raises(Rejection) as exc:
        svc.renew(env.env_id, "alice", 10)
    assert exc.value.code is ErrorCode.ENV_NOT_READY


def test_renew_after_expiry_races_reaper(svc):
    log.info("验证 临界时刻续租：到期优先 -> LEASE_EXPIRED 且环境进 EXPIRED，不会又续又收")
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    svc._clock.advance(30)  # now == 到期时间
    with pytest.raises(Rejection) as exc:
        svc.renew(env.env_id, "alice", 100)
    assert exc.value.code is ErrorCode.LEASE_EXPIRED
    assert env.status is EnvStatus.EXPIRED
    assert svc.usage("alice")["user_used"] == 0


def test_concurrent_renew_vs_reap_exactly_one_outcome(svc):
    log.info("验证 8 线程续租 与 8 线程回收并发：要么存活要么回收，绝不二者同时发生")
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    svc._clock.advance(30)  # 全员都在到期临界上：确定性结果=回收
    renewed, expired_err, other = 0, 0, []
    barrier = threading.Barrier(16)

    def renewer():
        barrier.wait()
        try:
            svc.renew(env.env_id, "alice", 60)
        except Rejection as r:
            other.append(r.code)

    def reaper():
        barrier.wait()
        try:
            svc.reap_expired()
        except Rejection as r:
            other.append(r)

    threads = [threading.Thread(target=renewer) for _ in range(8)]
    threads += [threading.Thread(target=reaper) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 到期时刻续租必败：0 次成功，其余全部 LEASE_EXPIRED
    assert env.status is EnvStatus.EXPIRED
    assert all(c is ErrorCode.LEASE_EXPIRED for c in other), other
    assert svc.usage("alice")["user_used"] == 0


def test_concurrent_renew_wins_when_before_expiry(svc):
    log.info("验证 到期前并发续租：多个续租请求串行生效，到期时间被延后，环境不被回收")
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    # 仅推进 10 秒：续租必然成功；同时混入回收调用但无环境到期
    barrier = threading.Barrier(12)
    errors = []

    def renewer():
        barrier.wait()
        try:
            svc.renew(env.env_id, "alice", 10)
        except Rejection as r:
            errors.append(r.code)

    def reaper():
        barrier.wait()
        svc.reap_expired()

    threads = [threading.Thread(target=renewer) for _ in range(8)]
    threads += [threading.Thread(target=reaper) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert env.status is EnvStatus.READY
    assert env.lease_expires_at == pytest.approx(env.created_at + 30 + 80)
    assert svc.usage("alice")["user_used"] == 1  # 续租不重复占额度


def test_concurrent_destroy_vs_reap_quota_released_once(svc):
    log.info("验证 销毁与到期回收并发：只有一方拿走终态，额度只放开一次")
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    svc._clock.advance(11)
    barrier = threading.Barrier(16)
    outcomes = []

    def destroyer():
        barrier.wait()
        try:
            svc.destroy(env.env_id, "alice")
            outcomes.append("destroy")
        except Rejection:
            outcomes.append("reject")

    def reaper():
        barrier.wait()
        svc.reap_expired()

    threads = [threading.Thread(target=destroyer) for _ in range(8)]
    threads += [threading.Thread(target=reaper) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 恰好一方拿走终态：手动销毁成功数为 0（回收先得）或 1（销毁先得），不会更多
    assert outcomes.count("destroy") in (0, 1)
    assert outcomes.count("reject") == 8 - outcomes.count("destroy")
    assert env.status in (EnvStatus.DESTROYED, EnvStatus.EXPIRED)
    log.info("竞态结果: 销毁先得=%d 回收先得=%d 终态=%s",
             outcomes.count("destroy"), 1 if outcomes.count("destroy") == 0 else 0,
             env.status.value)
    assert svc.usage("alice")["user_used"] == 0
    assert svc.usage("alice")["global_used"] == 0


def test_allocation_overrunning_ttl_expires_immediately():
    log.info("验证 锁外分配耗时跨过到期时间：READY 落库即回收，额度立即释放不多活一轮")
    clock = FakeClock()

    def slow_allocator(env):
        clock.advance(100)  # 分配耗时超过 10s 租约
        return f"res-{env.env_id}"

    svc = EnvService(QuotaManager(2, 4), allocator=slow_allocator, clock=clock)
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    assert env.status is EnvStatus.EXPIRED
    assert not env.quota_held
    assert svc.usage("alice")["user_used"] == 0


def test_lease_quota_invariant_under_mixed_concurrency():
    log.info("验证 申请/续租/回收/销毁 混合并发下，额度计数==实际持有额度的环境数")
    clock = FakeClock()
    svc = EnvService(QuotaManager(per_user_limit=20, global_limit=100), clock=clock)
    envs = [svc.request_env(f"u{i}", "k1", ttl_seconds=30) for i in range(20)]
    assert svc.usage("anyone")["global_used"] == 20

    def churn():
        for _ in range(200):
            svc.reap_expired()
            clock.advance(2)
            for e in envs:
                try:
                    svc.renew(e.env_id, e.owner, 5)
                except Rejection:
                    pass

    t1 = threading.Thread(target=churn)
    t2 = threading.Thread(target=churn)
    t1.start(); t2.start()
    # 主线程同时反复销毁
    for e in envs:
        try:
            svc.destroy(e.env_id, e.owner)
        except Rejection:
            pass
    t1.join(); t2.join()

    # 最终核对：每个环境要么终态且不占额度，要么存活且占额度；计数精确一致
    held = 0
    for e in envs:
        in_quota_states = e.status in (EnvStatus.REQUESTED, EnvStatus.ALLOCATING, EnvStatus.READY)
        assert e.quota_held == in_quota_states, (e.env_id, e.status, e.quota_held)
        if in_quota_states:
            held += 1
    global_used = svc.usage("anyone")["global_used"]
    log.info("混合并发结束: 存活持有额度=%d 额度计数=%d", held, global_used)
    assert global_used == held
