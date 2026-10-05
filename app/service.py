"""环境生命周期服务。

职责：
- 申请幂等：同一 (owner, idempotency_key) 重复提交返回同一环境，不重复占额度。
- 分配：REQUESTED -> ALLOCATING -> READY；失败回滚到 REQUESTED 并释放额度，可重试。
- 销毁：READY/REQUESTED -> DESTROYING -> DESTROYED，销毁完成即释放额度。
- 并发：状态迁移在锁内完成，同一环境不会被并发重复分配或重复销毁。
- 内部异常只记日志，对外统一为 ALLOCATE_FAILED / INTERNAL，不泄露细节。
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from .errors import ErrorCode, Rejection
from .models import Environment, EnvStatus
from .quota import QuotaManager

logger = logging.getLogger("env_service")

# 分配器：接收 Environment，返回资源标识；失败抛异常
Allocator = Callable[[Environment], str]


def _default_allocator(env: Environment) -> str:
    return f"res-{env.env_id}"


class EnvService:
    def __init__(self, quota: QuotaManager, allocator: Allocator | None = None) -> None:
        self._quota = quota
        self._allocator = allocator or _default_allocator
        self._lock = threading.RLock()
        self._envs: dict[str, Environment] = {}
        self._by_key: dict[tuple[str, str], str] = {}  # (owner, idem_key) -> env_id

    # ---------- 查询 ----------

    def get(self, env_id: str, owner: str) -> Environment:
        with self._lock:
            env = self._envs.get(env_id)
            if env is None or env.owner != owner:
                raise Rejection(ErrorCode.ENV_NOT_FOUND, f"环境 {env_id} 不存在")
            return env

    def list_by_owner(self, owner: str) -> list[Environment]:
        with self._lock:
            return [e for e in self._envs.values() if e.owner == owner]

    def usage(self, owner: str) -> dict[str, int]:
        return self._quota.usage(owner)

    # ---------- 申请（幂等） ----------

    def request_env(self, owner: str, idempotency_key: str) -> Environment:
        if not owner or not owner.strip():
            raise Rejection(ErrorCode.INVALID_PARAM, "申请人不能为空")
        if not idempotency_key or not idempotency_key.strip():
            raise Rejection(ErrorCode.INVALID_PARAM, "幂等键不能为空")
        with self._lock:
            existing_id = self._by_key.get((owner, idempotency_key))
            if existing_id is not None:
                env = self._envs[existing_id]
                logger.info("幂等命中 owner=%s key=%s -> env=%s", owner, idempotency_key, env.env_id)
                return env
            # 占额度（超限抛 Rejection，不产生任何计数变化）
            self._quota.acquire(owner)
            try:
                env = Environment(env_id=uuid.uuid4().hex[:12], owner=owner,
                                  idempotency_key=idempotency_key)
                self._envs[env.env_id] = env
                self._by_key[(owner, idempotency_key)] = env.env_id
            except Exception:
                self._quota.release(owner)
                raise
            logger.info("申请受理 env=%s owner=%s", env.env_id, owner)
        # 锁外执行分配，失败不影响其他请求
        self._allocate(env)
        return env

    # ---------- 分配与重试 ----------

    def _allocate(self, env: Environment) -> None:
        with self._lock:
            if env.status is not EnvStatus.REQUESTED:
                # 并发下已有其他线程在分配/已就绪，直接返回，保证不重复分配
                return
            env.transition(EnvStatus.ALLOCATING)
        try:
            resource = self._allocator(env)
        except Exception:
            logger.exception("分配失败 env=%s owner=%s", env.env_id, env.owner)
            with self._lock:
                env.transition(EnvStatus.REQUESTED, reason="ALLOCATE_FAILED")
                self._quota.release(env.owner)  # 释放已占用资源，允许重试
            return
        with self._lock:
            env.transition(EnvStatus.READY)
            logger.info("分配成功 env=%s resource=%s", env.env_id, resource)

    def retry(self, env_id: str, owner: str) -> Environment:
        """分配失败后的重试：仅允许从 REQUESTED 状态发起。"""
        with self._lock:
            env = self.get(env_id, owner)
            if env.status is not EnvStatus.REQUESTED:
                raise Rejection(
                    ErrorCode.ILLEGAL_TRANSITION,
                    f"环境 {env_id} 当前状态 {env.status.value}，仅分配失败(REQUESTED)后可重试",
                )
            self._quota.acquire(owner)  # 重新占额度，超限则拒绝
        self._allocate(env)
        return env

    # ---------- 销毁 ----------

    def destroy(self, env_id: str, owner: str) -> Environment:
        with self._lock:
            env = self.get(env_id, owner)
            # 非法流转（含并发重复销毁）在此被状态机拒绝
            env.transition(EnvStatus.DESTROYING)
            env.transition(EnvStatus.DESTROYED)
            self._quota.release(owner)
            logger.info("销毁完成 env=%s owner=%s", env.env_id, owner)
            return env
