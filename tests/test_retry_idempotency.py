"""业务：失败重试与幂等。

覆盖：分配中途失败释放已占资源、失败原因可区分（参数/额度/内部）、
内部异常不暴露细节、失败后允许重试且重试成功、重试幂等。
"""
from __future__ import annotations

import pytest

from app.domain import Environment, EnvStatus
from app.errors import EnvError, RejectionReason
from app.service import EnvService


class FlakyProvisioner:
    """可注入的分配器：前 fail_times 次抛内部异常，之后成功。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, env: Environment) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("simulated backend outage: db conn refused")


def test_provision_failure_releases_resources_and_allows_retry() -> None:
    prov = FlakyProvisioner(fail_times=1)
    svc = EnvService(per_user_quota=1, global_quota=1, provisioner=prov)

    with pytest.raises(EnvError) as exc:
        svc.apply("alice", "req-1")
    assert exc.value.reason is RejectionReason.INTERNAL

    # 失败后已占资源被释放：额度归零
    usage = svc.quota_usage("alice")
    assert usage["user_active"] == 0
    assert usage["global_active"] == 0

    # 同一 request_id 允许重试，且第二次成功
    env = svc.retry_provision("alice", "req-1")
    assert env.status is EnvStatus.READY
    assert svc.quota_usage("alice")["user_active"] == 1


def test_retry_after_success_is_idempotent() -> None:
    svc = EnvService(per_user_quota=2, global_quota=4)
    env = svc.apply("alice", "req-1")
    again = svc.retry_provision("alice", "req-1")
    assert again.env_id == env.env_id
    assert svc.quota_usage("alice")["user_active"] == 1


def test_internal_error_does_not_leak_details() -> None:
    prov = FlakyProvisioner(fail_times=1)
    svc = EnvService(provisioner=prov)
    with pytest.raises(EnvError) as exc:
        svc.apply("alice", "req-1")
    # 不暴露内部异常内容（如 "db conn refused"）
    assert "db conn refused" not in exc.value.message
    assert "RuntimeError" not in exc.value.message
    assert exc.value.reason is RejectionReason.INTERNAL


def test_failure_reasons_distinguishable() -> None:
    prov = FlakyProvisioner(fail_times=10)
    svc = EnvService(per_user_quota=1, global_quota=1, provisioner=prov)
    reasons: set[RejectionReason] = set()

    # 参数错误
    with pytest.raises(EnvError) as e1:
        svc.apply("", "req")
    reasons.add(e1.value.reason)

    # 内部异常（分配失败，不占额度）
    with pytest.raises(EnvError) as e2:
        svc.apply("alice", "req-1")
    reasons.add(e2.value.reason)

    # 换一个永远成功的服务占满额度 -> 额度不足
    svc2 = EnvService(per_user_quota=1, global_quota=1)
    svc2.apply("alice", "req-1")
    with pytest.raises(EnvError) as e3:
        svc2.apply("alice", "req-2")
    reasons.add(e3.value.reason)

    assert reasons == {
        RejectionReason.INVALID_ARGUMENT,
        RejectionReason.INTERNAL,
        RejectionReason.QUOTA_EXCEEDED_USER,
    }


def test_failed_apply_does_not_poison_request_id() -> None:
    """同一 request_id 失败后重试成功，再重复提交返回同一环境。"""
    prov = FlakyProvisioner(fail_times=2)
    svc = EnvService(per_user_quota=2, global_quota=4, provisioner=prov)

    for _ in range(2):
        with pytest.raises(EnvError) as exc:
            svc.apply("alice", "req-x")
        assert exc.value.reason is RejectionReason.INTERNAL

    env = svc.apply("alice", "req-x")  # 第三次成功
    assert env.status is EnvStatus.READY
    again = svc.apply("alice", "req-x")
    assert again.env_id == env.env_id
    assert svc.quota_usage("alice")["user_active"] == 1


def test_env_error_business_reason_passthrough() -> None:
    """分配器主动抛 EnvError（如参数校验）时原因原样传递，不回滚为 INTERNAL。"""
    def bad_provisioner(env: Environment) -> None:
        raise EnvError(RejectionReason.INVALID_ARGUMENT, "镜像不存在")

    svc = EnvService(provisioner=bad_provisioner)
    with pytest.raises(EnvError) as exc:
        svc.apply("alice", "req-1")
    assert exc.value.reason is RejectionReason.INVALID_ARGUMENT
    assert "镜像不存在" in exc.value.message
    assert svc.quota_usage("alice")["user_active"] == 0
