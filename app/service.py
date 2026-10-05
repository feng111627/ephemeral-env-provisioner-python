"""核心服务：申请、查询、销毁，含额度控制、幂等与失败重试。"""
from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from app.domain import Environment, EnvStatus, can_transition
from app.errors import EnvError, RejectionReason

logger = logging.getLogger("env_provisioner.service")

# 占用资源（计入额度）的状态：分配中 / 就绪 / 销毁中。
# REQUESTED 表示尚未分配或分配失败已回滚，不占用资源；
# DESTROYED 已释放，不占用资源。
_ACTIVE_STATUSES = frozenset(
    {EnvStatus.PROVISIONING, EnvStatus.READY, EnvStatus.DESTROYING}
)


class EnvService:
    """临时环境生命周期管理服务。

    - 线程安全：所有状态变更与额度计数在同一把锁内完成；
    - 幂等：同一 (owner, request_id) 重复申请返回同一环境，不重复占额度；
    - 失败回滚：分配失败时状态回滚为 REQUESTED 并释放已占额度，可重试；
    - 内部异常：记录日志后以 INTERNAL 原因返回，不暴露内部细节。
    """

    def __init__(
        self,
        per_user_quota: int = 2,
        global_quota: int = 8,
        provisioner: Callable[[Environment], None] | None = None,
    ) -> None:
        if per_user_quota <= 0 or global_quota <= 0:
            raise EnvError(RejectionReason.INVALID_ARGUMENT, "额度必须为正整数")
        if per_user_quota > global_quota:
            raise EnvError(
                RejectionReason.INVALID_ARGUMENT, "个人额度不能大于整体额度"
            )
        self._per_user_quota = per_user_quota
        self._global_quota = global_quota
        self._provisioner = provisioner or (lambda env: None)
        self._lock = threading.Lock()
        self._envs: dict[str, Environment] = {}
        self._by_request: dict[tuple[str, str], str] = {}

    # ---------- 内部辅助 ----------

    def _transition(self, env: Environment, target: EnvStatus) -> None:
        """执行状态流转，非法流转抛出 ILLEGAL_TRANSITION。"""
        if not can_transition(env.status, target):
            raise EnvError(
                RejectionReason.ILLEGAL_TRANSITION,
                f"不允许从 {env.status.value} 流转到 {target.value}",
            )
        env.history.append((env.status, target))
        logger.info("env %s: %s -> %s", env.env_id, env.status.value, target.value)
        env.status = target

    def _active_count(self, owner: str | None = None) -> int:
        return sum(
            1
            for e in self._envs.values()
            if e.status in _ACTIVE_STATUSES and (owner is None or e.owner == owner)
        )

    def _check_quota(self, owner: str) -> None:
        if self._active_count(owner) >= self._per_user_quota:
            raise EnvError(
                RejectionReason.QUOTA_EXCEEDED_USER,
                f"用户 {owner} 额度已达上限 {self._per_user_quota}",
            )
        if self._active_count() >= self._global_quota:
            raise EnvError(
                RejectionReason.QUOTA_EXCEEDED_GLOBAL,
                f"整体额度已达上限 {self._global_quota}",
            )

    def _provision(self, env: Environment) -> None:
        """执行分配；失败时回滚状态、释放额度并记录日志。"""
        self._transition(env, EnvStatus.PROVISIONING)
        try:
            self._provisioner(env)
        except EnvError:
            self._transition(env, EnvStatus.REQUESTED)
            raise
        except Exception:
            logger.exception("env %s 分配失败（内部异常）", env.env_id)
            self._transition(env, EnvStatus.REQUESTED)
            raise EnvError(
                RejectionReason.INTERNAL, "分配失败：内部错误，可稍后重试"
            ) from None
        self._transition(env, EnvStatus.READY)

    # ---------- 对外接口 ----------

    def apply(self, owner: str, request_id: str) -> Environment:
        """申请环境。同一 (owner, request_id) 重复提交返回同一结果。"""
        if not owner or not owner.strip():
            raise EnvError(RejectionReason.INVALID_ARGUMENT, "owner 不能为空")
        if not request_id or not request_id.strip():
            raise EnvError(RejectionReason.INVALID_ARGUMENT, "request_id 不能为空")
        key = (owner, request_id)
        with self._lock:
            existing = self._by_request.get(key)
            if existing is not None:
                logger.info("重复申请 %s，返回已有环境 %s", key, existing)
                return self._envs[existing]
            self._check_quota(owner)
            env = Environment(
                env_id=uuid.uuid4().hex, owner=owner, request_id=request_id
            )
            self._envs[env.env_id] = env
            try:
                self._provision(env)
            except Exception:
                # 分配失败：释放记录与额度，允许用同一 request_id 重试
                del self._envs[env.env_id]
                raise
            self._by_request[key] = env.env_id
            return env

    def retry_provision(self, owner: str, request_id: str) -> Environment:
        """对同一申请重试分配：重新检查额度后再次执行分配。"""
        if not owner or not request_id:
            raise EnvError(RejectionReason.INVALID_ARGUMENT, "owner/request_id 不能为空")
        key = (owner, request_id)
        with self._lock:
            existing = self._by_request.get(key)
            if existing is not None:
                # 已成功过，幂等返回同一环境
                return self._envs[existing]
            self._check_quota(owner)
            env = Environment(
                env_id=uuid.uuid4().hex, owner=owner, request_id=request_id
            )
            self._envs[env.env_id] = env
            try:
                self._provision(env)
            except Exception:
                del self._envs[env.env_id]
                raise
            self._by_request[key] = env.env_id
            return env

    def get(self, env_id: str) -> Environment:
        """查询环境，不存在返回 NOT_FOUND。"""
        with self._lock:
            env = self._envs.get(env_id)
            if env is None:
                raise EnvError(RejectionReason.NOT_FOUND, f"环境 {env_id} 不存在")
            return env

    def destroy(self, env_id: str, operator: str) -> Environment:
        """销毁环境。仅 READY 可销毁；已销毁的重复销毁幂等返回同一终态。"""
        if not operator or not operator.strip():
            raise EnvError(RejectionReason.INVALID_ARGUMENT, "operator 不能为空")
        with self._lock:
            env = self._envs.get(env_id)
            if env is None:
                raise EnvError(RejectionReason.NOT_FOUND, f"环境 {env_id} 不存在")
            if env.owner != operator:
                raise EnvError(RejectionReason.FORBIDDEN, "只有属主可以销毁环境")
            if env.status is EnvStatus.DESTROYED:
                logger.info("环境 %s 已销毁，重复销毁幂等返回", env_id)
                return env
            self._transition(env, EnvStatus.DESTROYING)
            self._transition(env, EnvStatus.DESTROYED)
            return env

    def quota_usage(self, owner: str) -> dict[str, int]:
        """返回额度使用情况（用于调用方观测，不做校验）。"""
        with self._lock:
            return {
                "user_active": self._active_count(owner),
                "user_quota": self._per_user_quota,
                "global_active": self._active_count(),
                "global_quota": self._global_quota,
            }
