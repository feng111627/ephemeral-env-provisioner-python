"""业务：并发额度控制。

覆盖：个人额度上限、整体额度上限、并发申请不超发、
额度计数不多算不少算、重复申请不重复占额度、拒绝原因可区分。
"""
from __future__ import annotations

import threading

import pytest

from app.errors import EnvError, RejectionReason
from app.service import EnvService


def test_per_user_quota_enforced() -> None:
    svc = EnvService(per_user_quota=2, global_quota=10)
    svc.apply("alice", "r1")
    svc.apply("alice", "r2")
    with pytest.raises(EnvError) as exc:
        svc.apply("alice", "r3")
    assert exc.value.reason is RejectionReason.QUOTA_EXCEEDED_USER
    # 额度计数精确：2 个活跃，未多算未少算
    usage = svc.quota_usage("alice")
    assert usage["user_active"] == 2
    assert usage["global_active"] == 2


def test_global_quota_enforced() -> None:
    svc = EnvService(per_user_quota=3, global_quota=3)
    svc.apply("u1", "r1")
    svc.apply("u2", "r1")
    svc.apply("u3", "r1")
    with pytest.raises(EnvError) as exc:
        svc.apply("u4", "r1")
    assert exc.value.reason is RejectionReason.QUOTA_EXCEEDED_GLOBAL


def test_quota_reasons_distinguishable() -> None:
    svc = EnvService(per_user_quota=1, global_quota=2)
    svc.apply("alice", "r1")
    svc.apply("bob", "r1")
    # alice 撞到的是个人额度而非整体额度
    with pytest.raises(EnvError) as user_exc:
        svc.apply("alice", "r2")
    assert user_exc.value.reason is RejectionReason.QUOTA_EXCEEDED_USER
    # carol 撞到的是整体额度
    with pytest.raises(EnvError) as global_exc:
        svc.apply("carol", "r1")
    assert global_exc.value.reason is RejectionReason.QUOTA_EXCEEDED_GLOBAL


def test_duplicate_apply_returns_same_env_without_extra_quota() -> None:
    svc = EnvService(per_user_quota=2, global_quota=10)
    first = svc.apply("alice", "same-req")
    for _ in range(5):
        again = svc.apply("alice", "same-req")
        assert again.env_id == first.env_id
    usage = svc.quota_usage("alice")
    assert usage["user_active"] == 1  # 重复提交不重复占额度


def test_destroy_releases_quota() -> None:
    svc = EnvService(per_user_quota=1, global_quota=10)
    env = svc.apply("alice", "r1")
    svc.destroy(env.env_id, "alice")
    assert svc.quota_usage("alice")["user_active"] == 0
    # 释放后可再次申请
    svc.apply("alice", "r2")


def test_rejected_apply_does_not_consume_quota() -> None:
    svc = EnvService(per_user_quota=1, global_quota=10)
    svc.apply("alice", "r1")
    for i in range(5):
        with pytest.raises(EnvError):
            svc.apply("alice", f"overflow-{i}")
    assert svc.quota_usage("alice")["user_active"] == 1


def test_concurrent_apply_never_exceeds_quota() -> None:
    per_user, global_q = 3, 10
    svc = EnvService(per_user_quota=per_user, global_quota=global_q)
    users = [f"user-{i}" for i in range(8)]
    barrier = threading.Barrier(len(users) * 6)
    successes: list[str] = []
    failures: list[RejectionReason] = []
    lock = threading.Lock()

    def worker(owner: str, n: int) -> None:
        barrier.wait()
        try:
            svc.apply(owner, f"req-{n}")
            with lock:
                successes.append(owner)
        except EnvError as e:
            with lock:
                failures.append(e.reason)

    threads = [
        threading.Thread(target=worker, args=(u, n))
        for u in users
        for n in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 总成功数不超过整体额度
    assert len(successes) <= global_q
    # 每个用户成功数不超过个人额度
    for u in users:
        assert successes.count(u) <= per_user
    # 所有失败都是额度原因，不是内部错误
    assert set(failures) <= {
        RejectionReason.QUOTA_EXCEEDED_USER,
        RejectionReason.QUOTA_EXCEEDED_GLOBAL,
    }
    # 额度计数与实际一致：不少算也不多算
    usage = svc.quota_usage("user-0")
    assert usage["global_active"] == len(successes)


def test_invalid_arguments_rejected() -> None:
    svc = EnvService(per_user_quota=2, global_quota=4)
    for owner, req in [("", "r1"), ("  ", "r1"), ("alice", ""), ("alice", "  ")]:
        with pytest.raises(EnvError) as exc:
            svc.apply(owner, req)
        assert exc.value.reason is RejectionReason.INVALID_ARGUMENT
    assert svc.quota_usage("alice")["global_active"] == 0


def test_invalid_quota_config_rejected() -> None:
    with pytest.raises(EnvError) as exc:
        EnvService(per_user_quota=0, global_quota=4)
    assert exc.value.reason is RejectionReason.INVALID_ARGUMENT
    with pytest.raises(EnvError) as exc:
        EnvService(per_user_quota=5, global_quota=2)
    assert exc.value.reason is RejectionReason.INVALID_ARGUMENT
