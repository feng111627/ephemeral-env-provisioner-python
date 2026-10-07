"""业务域：租约（定时回收与续租）。

覆盖：
- 带租约申请与到期时间计算；未指定时长默认永不过期（旧用法兼容）。
- 到期自动回收（批量回收 + 查询惰性回收）、回收幂等只放一次额度。
- 续租成功、多次续租不重复占额度、到期时间顺延。
- 不该续租的环境被拒绝且原因可区分：已回收 / 已销毁 / 无租约 / 未就绪 / 参数非法。
- 并发：续租撞上到期回收，要么续租成功要么被回收，二者不并存；
  额度计数与实际占用始终一致；重复批量回收只有一次生效。
- 终态保护：已回收环境不能被续租/重试/销毁拉回。
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
    """可手动推进的假时钟，到期验证无需真实等待。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fx():
    clock = FakeClock()
    svc = EnvService(QuotaManager(per_user_limit=3, global_limit=6), clock=clock)
    return svc, clock


def _held_count(svc: EnvService) -> int:
    return sum(1 for e in svc._envs.values() if e.quota_held)


# ---------- 申请与默认行为 ----------

def test_request_with_ttl_sets_expiry(fx):
    log.info("验证 带 ttl 申请：到期时间=申请时刻+ttl，视图字段完整")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    assert env.status is EnvStatus.READY
    assert env.ttl_seconds == 30
    assert env.expires_at == clock.now + 30


def test_request_without_ttl_never_expires(fx):
    log.info("验证 不指定时长：ttl/到期时间均为空，时间推进后也不被回收（旧用法不变）")
    svc, clock = fx
    env = svc.request_env("alice", "k1")
    assert env.ttl_seconds is None and env.expires_at is None
    clock.advance(3600 * 24 * 365)
    assert svc.reap_expired() == []
    assert env.status is EnvStatus.READY
    assert svc.usage("alice")["user_used"] == 1


@pytest.mark.parametrize("bad_ttl", [0, -1, 1.5, True])
def test_request_with_invalid_ttl_rejected(fx, bad_ttl):
    log.info("验证 申请时非法 ttl(%r) 返回 INVALID_PARAM 且不占额度", bad_ttl)
    svc, _ = fx
    with pytest.raises(Rejection) as exc:
        svc.request_env("alice", "k1", ttl_seconds=bad_ttl)
    assert exc.value.code is ErrorCode.INVALID_PARAM
    assert svc.usage("alice")["user_used"] == 0


# ---------- 到期回收 ----------

def test_reap_expired_reclaims_quota_once(fx):
    log.info("验证 到期回收：READY->EXPIRED、额度释放、重复触发只回收一次")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    clock.advance(30)  # 恰好到期（>= 判定）
    reaped = svc.reap_expired()
    assert reaped == [env.env_id]
    assert env.status is EnvStatus.EXPIRED
    assert env.failure_reason == "LEASE_EXPIRED"
    assert env.quota_held is False
    assert svc.usage("alice")["user_used"] == 0
    assert svc.usage("alice")["global_used"] == 0
    # 重复回收：幂等，不多放额度
    assert svc.reap_expired() == []
    assert svc.usage("alice")["global_used"] == 0


def test_reap_only_takes_due_envs(fx):
    log.info("验证 批量回收只收到期的，未到期与无租约环境不受影响")
    svc, clock = fx
    short = svc.request_env("alice", "k1", ttl_seconds=10)
    long_ = svc.request_env("alice", "k2", ttl_seconds=60)
    forever = svc.request_env("bob", "k1")
    clock.advance(20)
    reaped = svc.reap_expired()
    assert reaped == [short.env_id]
    assert long_.status is EnvStatus.READY and forever.status is EnvStatus.READY
    assert svc.usage("anyone")["global_used"] == 2


def test_lazy_expiry_on_get_and_list(fx):
    log.info("验证 查询/列表时惰性回收到期环境并释放额度，无需真实等待")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    clock.advance(11)
    got = svc.get(env.env_id, "alice")
    assert got.status is EnvStatus.EXPIRED
    assert svc.usage("alice")["user_used"] == 0
    # 列表同样触发
    env2 = svc.request_env("alice", "k2", ttl_seconds=5)
    clock.advance(6)  # 推进到 env2 到期之后
    assert {e.env_id for e in svc.list_by_owner("alice") if e.status is EnvStatus.EXPIRED} == {
        env.env_id, env2.env_id
    }
    assert svc.usage("anyone")["global_used"] == 0


def test_expired_is_terminal_for_all_actions(fx):
    log.info("验证 已回收是终态：续租/重试/销毁都带可区分原因被拒绝，拉不回来")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    clock.advance(10)
    svc.reap_expired()
    for action, expected in (
        (lambda: svc.renew_lease(env.env_id, "alice", 30), ErrorCode.LEASE_EXPIRED),
        (lambda: svc.retry(env.env_id, "alice"), ErrorCode.LEASE_EXPIRED),
        (lambda: svc.destroy(env.env_id, "alice"), ErrorCode.LEASE_EXPIRED),
    ):
        with pytest.raises(Rejection) as exc:
            action()
        assert exc.value.code is expected
    assert env.status is EnvStatus.EXPIRED


def test_quota_freed_after_expiry_allows_new_request(fx):
    log.info("验证 回收放出的额度可立即用于新申请，计数与占用一致")
    svc, clock = fx
    svc.request_env("alice", "k1", ttl_seconds=10)
    svc.request_env("alice", "k2", ttl_seconds=10)
    svc.request_env("alice", "k3", ttl_seconds=10)
    clock.advance(10)
    svc.reap_expired()
    assert svc.usage("alice")["user_used"] == 0
    fresh = svc.request_env("alice", "k4", ttl_seconds=10)
    assert fresh.status is EnvStatus.READY
    assert svc.usage("alice")["user_used"] == 1


# ---------- 续租 ----------

def test_renew_extends_expiry_without_extra_quota(fx):
    log.info("验证 续租顺延到期时间，可多次续租，额度始终只占一份")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    first_expiry = env.expires_at
    svc.renew_lease(env.env_id, "alice", 30)
    assert env.expires_at == first_expiry  # 时钟未动，从“当前时刻”重新计时
    clock.advance(20)
    svc.renew_lease(env.env_id, "alice", 60)
    assert env.expires_at == clock.now + 60
    assert env.status is EnvStatus.READY
    assert svc.usage("alice")["user_used"] == 1
    renew_entries = [h for h in env.history if h.startswith("RENEWED")]
    assert len(renew_entries) == 2


def test_renew_survives_beyond_original_expiry(fx):
    log.info("验证 续租后按新到期时间判定，原到期时间不再回收")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    clock.advance(25)
    svc.renew_lease(env.env_id, "alice", 30)
    clock.advance(20)  # 已过原到期(+30)，但续租后仍有效
    assert svc.reap_expired() == []
    assert env.status is EnvStatus.READY
    clock.advance(11)  # 超过新到期
    assert svc.reap_expired() == [env.env_id]
    assert env.status is EnvStatus.EXPIRED


@pytest.mark.parametrize("bad_ttl", [0, -5, 2.0, False])
def test_renew_invalid_ttl_rejected(fx, bad_ttl):
    log.info("验证 续租非法时长(%r) 返回 INVALID_PARAM 且不改到期时间", bad_ttl)
    svc, _ = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    before = env.expires_at
    with pytest.raises(Rejection) as exc:
        svc.renew_lease(env.env_id, "alice", bad_ttl)
    assert exc.value.code is ErrorCode.INVALID_PARAM
    assert env.expires_at == before


def test_renew_expired_env_distinct_reason(fx):
    log.info("验证 对已回收环境续租返回 LEASE_EXPIRED")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    clock.advance(10)
    svc.reap_expired()
    with pytest.raises(Rejection) as exc:
        svc.renew_lease(env.env_id, "alice", 30)
    assert exc.value.code is ErrorCode.LEASE_EXPIRED


def test_renew_destroyed_env_distinct_reason(fx):
    log.info("验证 对已销毁环境续租返回 ENV_DESTROYED，与到期回收可区分")
    svc, _ = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    svc.destroy(env.env_id, "alice")
    with pytest.raises(Rejection) as exc:
        svc.renew_lease(env.env_id, "alice", 30)
    assert exc.value.code is ErrorCode.ENV_DESTROYED


def test_renew_lease_without_ttl_distinct_reason(fx):
    log.info("验证 对未指定租约（永不过期）的环境续租返回 LEASE_NOT_ACTIVE")
    svc, _ = fx
    env = svc.request_env("alice", "k1")
    with pytest.raises(Rejection) as exc:
        svc.renew_lease(env.env_id, "alice", 30)
    assert exc.value.code is ErrorCode.LEASE_NOT_ACTIVE


def test_renew_unready_env_distinct_reason():
    log.info("验证 分配失败处于 REQUESTED 的环境不可续租，返回 LEASE_NOT_ACTIVE")

    def boom(_env):
        raise RuntimeError("internal-detail-x")

    svc = EnvService(QuotaManager(3, 6), allocator=boom)
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    assert env.status is EnvStatus.REQUESTED
    with pytest.raises(Rejection) as exc:
        svc.renew_lease(env.env_id, "alice", 30)
    assert exc.value.code is ErrorCode.LEASE_NOT_ACTIVE


# ---------- 并发：续租 vs 到期回收 ----------

def test_concurrent_renew_and_reap_race_has_single_outcome(fx):
    log.info("验证 到期临界点并发：续租与回收不会同时发生，结果唯一且额度一致")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    clock.advance(30)  # 恰好到期：谁先拿锁谁赢
    barrier = threading.Barrier(16)
    renewed, expired, other = [], [], []

    def do_renew():
        barrier.wait()
        try:
            svc.renew_lease(env.env_id, "alice", 60)
            renewed.append(1)
        except Rejection as r:
            if r.code is ErrorCode.LEASE_EXPIRED:
                expired.append(r.code)
            else:
                other.append(r.code)

    def do_reap():
        barrier.wait()
        svc.reap_expired()

    threads = [threading.Thread(target=do_renew) for _ in range(8)]
    threads += [threading.Thread(target=do_reap) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert other == []
    assert _held_count(svc) == svc.usage("anyone")["global_used"]
    if env.status is EnvStatus.EXPIRED:
        # 回收先赢：所有续租都被拒，额度归零且只有一次释放
        assert renewed == []
        assert len(expired) == 8
        assert svc.usage("anyone")["global_used"] == 0
        assert env.quota_held is False
        log.info("临界点结果：回收胜出，续租全部被拒")
    else:
        # 续租先赢：至少一次续租成功，之后回收在旧时刻已无东西可收
        assert env.status is EnvStatus.READY
        assert env.expires_at > clock.now
        assert len(renewed) >= 1
        assert svc.usage("anyone")["global_used"] == 1
        log.info("临界点结果：续租胜出 %d 次，环境保留", len(renewed))


def test_concurrent_renew_wins_before_expiry(fx):
    log.info("验证 未到期时并发续租/回收：续租成功、环境保留、额度仍为 1")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    clock.advance(29)
    barrier = threading.Barrier(12)

    def race(is_renew):
        barrier.wait()
        if is_renew:
            svc.renew_lease(env.env_id, "alice", 60)
        else:
            svc.reap_expired()

    threads = [threading.Thread(target=race, args=(i % 2 == 0,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert env.status is EnvStatus.READY
    assert env.expires_at == clock.now + 60
    assert svc.usage("anyone")["global_used"] == 1
    assert _held_count(svc) == 1


def test_concurrent_reap_wins_after_expiry(fx):
    log.info("验证 已过期时并发续租/回收：回收胜出，续租全部 LEASE_EXPIRED，额度归零")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=30)
    clock.advance(31)
    barrier = threading.Barrier(12)
    rejected = []

    def race(is_renew):
        barrier.wait()
        try:
            if is_renew:
                svc.renew_lease(env.env_id, "alice", 60)
            else:
                svc.reap_expired()
        except Rejection as r:
            rejected.append(r.code)

    threads = [threading.Thread(target=race, args=(i % 2 == 0,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert env.status is EnvStatus.EXPIRED
    assert rejected and all(c is ErrorCode.LEASE_EXPIRED for c in rejected)
    assert svc.usage("anyone")["global_used"] == 0
    assert _held_count(svc) == 0


def test_concurrent_repeated_reap_only_reclaims_once(fx):
    log.info("验证 多线程同时触发批量回收，同一环境只被回收一次、额度只放一次")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    svc.request_env("alice", "k2", ttl_seconds=10)
    clock.advance(10)
    barrier = threading.Barrier(8)
    all_reaped: list[list[str]] = []
    lock = threading.Lock()

    def do_reap():
        barrier.wait()
        ids = svc.reap_expired()
        with lock:
            all_reaped.append(ids)

    threads = [threading.Thread(target=do_reap) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    flat = [i for ids in all_reaped for i in ids]
    assert sorted(flat) == sorted([e for e in svc._envs])
    assert len(flat) == 2  # 每个环境恰好在一个线程的返回里
    assert svc.usage("anyone")["global_used"] == 0
    assert _held_count(svc) == 0


def test_concurrent_destroy_and_reap_single_outcome(fx):
    log.info("验证 手动销毁与到期回收并发：额度恰好释放一次，终态唯一")
    svc, clock = fx
    env = svc.request_env("alice", "k1", ttl_seconds=10)
    clock.advance(10)
    barrier = threading.Barrier(8)
    outcomes = {"destroyed": 0, "expired_reject": 0}

    def race(is_destroy):
        barrier.wait()
        try:
            if is_destroy:
                svc.destroy(env.env_id, "alice")
                outcomes["destroyed"] += 1
            else:
                svc.reap_expired()
        except Rejection as r:
            if r.code is ErrorCode.LEASE_EXPIRED:
                outcomes["expired_reject"] += 1
            else:
                raise

    threads = [threading.Thread(target=race, args=(i % 2 == 0,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert env.is_terminal
    assert env.status in (EnvStatus.DESTROYED, EnvStatus.EXPIRED)
    assert outcomes["destroyed"] == (1 if env.status is EnvStatus.DESTROYED else 0)
    assert svc.usage("anyone")["global_used"] == 0
    assert _held_count(svc) == 0
