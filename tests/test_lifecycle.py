"""业务：生命周期状态机流转。

覆盖：申请->分配中->就绪->销毁中->已销毁 的正常流转、
非法流转拒绝（原因可区分）、并发重复销毁只允许一次。
"""
from __future__ import annotations

import threading

import pytest

from app.domain import EnvStatus
from app.errors import EnvError, RejectionReason
from app.service import EnvService


@pytest.fixture
def service() -> EnvService:
    return EnvService(per_user_quota=4, global_quota=16)


def test_apply_transitions_to_ready_with_history(service: EnvService) -> None:
    env = service.apply("alice", "req-1")
    assert env.status is EnvStatus.READY
    # 完整流转历史：申请 -> 分配中 -> 就绪
    assert env.history == [
        (EnvStatus.REQUESTED, EnvStatus.PROVISIONING),
        (EnvStatus.PROVISIONING, EnvStatus.READY),
    ]


def test_destroy_transitions_to_destroyed(service: EnvService) -> None:
    env = service.apply("alice", "req-1")
    destroyed = service.destroy(env.env_id, "alice")
    assert destroyed.status is EnvStatus.DESTROYED
    assert destroyed.history[-2:] == [
        (EnvStatus.READY, EnvStatus.DESTROYING),
        (EnvStatus.DESTROYING, EnvStatus.DESTROYED),
    ]


def test_double_destroy_is_idempotent(service: EnvService) -> None:
    env = service.apply("alice", "req-1")
    first = service.destroy(env.env_id, "alice")
    again = service.destroy(env.env_id, "alice")
    assert again.status is EnvStatus.DESTROYED
    assert again.env_id == first.env_id
    # 不会重复记录销毁流转
    transitions = [t for t in again.history if t[1] is EnvStatus.DESTROYING]
    assert len(transitions) == 1


def test_destroy_by_non_owner_forbidden(service: EnvService) -> None:
    env = service.apply("alice", "req-1")
    with pytest.raises(EnvError) as exc:
        service.destroy(env.env_id, "bob")
    assert exc.value.reason is RejectionReason.FORBIDDEN


def test_destroy_missing_env_not_found(service: EnvService) -> None:
    with pytest.raises(EnvError) as exc:
        service.destroy("no-such-env", "alice")
    assert exc.value.reason is RejectionReason.NOT_FOUND


def test_get_missing_env_not_found(service: EnvService) -> None:
    with pytest.raises(EnvError) as exc:
        service.get("no-such-env")
    assert exc.value.reason is RejectionReason.NOT_FOUND


def test_illegal_transition_rejected_with_distinct_reason(service: EnvService) -> None:
    env = service.apply("alice", "req-1")
    # 直接构造非法流转：READY 不允许回到 PROVISIONING
    with pytest.raises(EnvError) as exc:
        service._transition(env, EnvStatus.PROVISIONING)
    assert exc.value.reason is RejectionReason.ILLEGAL_TRANSITION
    assert env.status is EnvStatus.READY  # 状态未被破坏


def test_concurrent_destroy_only_one_transition(service: EnvService) -> None:
    env = service.apply("alice", "req-1")
    results: list[EnvStatus] = []
    errors: list[EnvError] = []

    def do_destroy() -> None:
        try:
            results.append(service.destroy(env.env_id, "alice").status)
        except EnvError as e:
            errors.append(e)

    threads = [threading.Thread(target=do_destroy) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert results == [EnvStatus.DESTROYED] * 8
    # 销毁流转只发生一次，其余为幂等返回
    transitions = [t for t in env.history if t[1] is EnvStatus.DESTROYING]
    assert len(transitions) == 1
