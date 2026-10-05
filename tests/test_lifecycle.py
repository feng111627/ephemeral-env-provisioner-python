"""业务域：生命周期状态机流转。

覆盖：合法流转全链路、非法流转拒绝（可区分原因）、并发重复销毁。
"""
import logging
import threading

import pytest

from app.errors import ErrorCode, Rejection
from app.models import Environment, EnvStatus

log = logging.getLogger("test.lifecycle")


def test_full_happy_path(service):
    log.info("验证 申请->分配中->就绪->销毁中->已销毁 全链路")
    env = service.request_env("alice", "k1")
    assert env.status is EnvStatus.READY
    assert [h.split("@")[0] for h in env.history] == ["REQUESTED", "ALLOCATING", "READY"]
    service.destroy(env.env_id, "alice")
    assert env.status is EnvStatus.DESTROYED
    assert [h.split("@")[0] for h in env.history][-2:] == ["DESTROYING", "DESTROYED"]


def test_illegal_transition_rejected_with_distinct_reason():
    log.info("验证 READY->ALLOCATING 等非法流转被拒绝且原因可区分")
    env = Environment("e1", "alice", "k1")
    env.transition(EnvStatus.ALLOCATING)
    env.transition(EnvStatus.READY)
    with pytest.raises(Rejection) as exc:
        env.transition(EnvStatus.ALLOCATING)
    assert exc.value.code is ErrorCode.ILLEGAL_TRANSITION
    assert "READY" in exc.value.message and "ALLOCATING" in exc.value.message


def test_destroyed_is_terminal(service):
    log.info("验证 已销毁 是终态，任何后续流转被拒绝")
    env = service.request_env("alice", "k1")
    service.destroy(env.env_id, "alice")
    with pytest.raises(Rejection) as exc:
        service.destroy(env.env_id, "alice")
    assert exc.value.code is ErrorCode.ILLEGAL_TRANSITION
    with pytest.raises(Rejection) as exc:
        service.retry(env.env_id, "alice")
    assert exc.value.code is ErrorCode.ILLEGAL_TRANSITION


def test_concurrent_destroy_only_one_succeeds(service):
    log.info("验证 并发销毁同一环境只有一个成功，其余收到 ILLEGAL_TRANSITION")
    env = service.request_env("alice", "k1")
    results, errors = [], []
    barrier = threading.Barrier(8)

    def do_destroy():
        barrier.wait()
        try:
            service.destroy(env.env_id, "alice")
            results.append(1)
        except Rejection as r:
            errors.append(r.code)

    threads = [threading.Thread(target=do_destroy) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1
    assert errors == [ErrorCode.ILLEGAL_TRANSITION] * 7
    assert env.status is EnvStatus.DESTROYED
    log.info("并发销毁结果: 成功=%d 拒绝=%d", len(results), len(errors))


def test_get_other_users_env_not_found(service):
    log.info("验证 跨用户访问返回 ENV_NOT_FOUND，不泄露环境存在性")
    env = service.request_env("alice", "k1")
    with pytest.raises(Rejection) as exc:
        service.get(env.env_id, "bob")
    assert exc.value.code is ErrorCode.ENV_NOT_FOUND
