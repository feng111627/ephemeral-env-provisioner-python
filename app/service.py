"""环境生命周期服务。

职责：
- 申请幂等：同一 (owner, idempotency_key) 重复提交返回同一环境，不重复占额度。
- 分配：REQUESTED -> ALLOCATING -> READY；失败回滚到 REQUESTED 并释放额度，可重试。
- 销毁：READY/REQUESTED -> DESTROYING -> DESTROYED，销毁完成即释放额度。
- 租约：申请时可指定 ttl_seconds；到期由服务自动回收（READY -> EXPIRED）并释放额度；
  未指定时长的环境永不过期，保持旧用法。租约未到期可续租，续租只改到期时间、不重复占额度。
- 并发：所有"判断状态 + 状态迁移 + 额度变更"都在同一把锁内完成，续租与到期回收不可能
  同时发生；额度释放以 env.quota_held 为唯一配对依据，重复触发也只放一次。
- 内部异常只记日志，对外统一为 ALLOCATE_FAILED / INTERNAL，不泄露细节。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable

from .errors import ErrorCode, Rejection
from .models import Environment, EnvStatus
from .quota import QuotaManager

logger = logging.getLogger("env_service")

# 分配器：接收 Environment，返回资源标识；失败抛异常
Allocator = Callable[[Environment], str]
# 时钟：返回当前 unix 时间戳（秒）；可注入假时钟做确定性到期测试
Clock = Callable[[], float]


def _default_allocator(env: Environment) -> str:
    return f"res-{env.env_id}"


class EnvService:
    def __init__(
        self,
        quota: QuotaManager,
        allocator: Allocator | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._quota = quota
        self._allocator = allocator or _default_allocator
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._envs: dict[str, Environment] = {}
        self._by_key: dict[tuple[str, str], str] = {}  # (owner, idem_key) -> env_id

    # ---------- 内部工具（调用方必须持锁） ----------

    def _release_quota_once(self, env: Environment) -> bool:
        """释放一次额度；以 quota_held 为唯一依据，重复调用不会多放。"""
        if env.quota_held:
            self._quota.release(env.owner)
            env.quota_held = False
            return True
        return False

    def _expire_if_due_locked(self, env: Environment) -> bool:
        """惰性回收：带租约环境到期则原子回收并释放额度。

        返回 True 表示本次调用完成了回收（重复触发只可能有一次返回 True）。
        """
        if (
            env.status is EnvStatus.READY
            and env.expires_at is not None
            and self._clock() >= env.expires_at
        ):
            env.transition(EnvStatus.EXPIRED, reason="LEASE_EXPIRED")
            self._release_quota_once(env)
            logger.info("租约到期自动回收 env=%s owner=%s", env.env_id, env.owner)
            return True
        return False

    def _guard_renew_state_locked(self, env: Environment) -> None:
        """根据当前状态对续租给出可区分的拒绝原因。"""
        if env.status is EnvStatus.EXPIRED:
            raise Rejection(
                ErrorCode.LEASE_EXPIRED,
                f"环境 {env.env_id} 租约已到期并回收，不能续租",
            )
        if env.status is EnvStatus.DESTROYED:
            raise Rejection(
                ErrorCode.ENV_DESTROYED,
                f"环境 {env.env_id} 已销毁，不能续租",
            )
        if env.status is not EnvStatus.READY:
            raise Rejection(
                ErrorCode.LEASE_NOT_ACTIVE,
                f"环境 {env.env_id} 当前状态 {env.status.value}，仅就绪(READY)环境可续租",
            )
        if not env.has_lease:
            raise Rejection(
                ErrorCode.LEASE_NOT_ACTIVE,
                f"环境 {env.env_id} 未指定租约时长（永不过期），不能续租",
            )

    # ---------- 查询 ----------

    def get(self, env_id: str, owner: str) -> Environment:
        with self._lock:
            env = self._envs.get(env_id)
            if env is None or env.owner != owner:
                raise Rejection(ErrorCode.ENV_NOT_FOUND, f"环境 {env_id} 不存在")
            self._expire_if_due_locked(env)  # 查询时惰性回收，保证看到真实状态
            return env

    def list_by_owner(self, owner: str) -> list[Environment]:
        with self._lock:
            envs = [e for e in self._envs.values() if e.owner == owner]
            for env in envs:
                self._expire_if_due_locked(env)
            return envs

    def usage(self, owner: str) -> dict[str, int]:
        return self._quota.usage(owner)

    # ---------- 申请（幂等） ----------

    def request_env(
        self,
        owner: str,
        idempotency_key: str,
        ttl_seconds: int | None = None,
    ) -> Environment:
        if not owner or not owner.strip():
            raise Rejection(ErrorCode.INVALID_PARAM, "申请人不能为空")
        if not idempotency_key or not idempotency_key.strip():
            raise Rejection(ErrorCode.INVALID_PARAM, "幂等键不能为空")
        if ttl_seconds is not None:
            if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
                raise Rejection(ErrorCode.INVALID_PARAM, "租约时长必须为正整数秒")
            if ttl_seconds <= 0:
                raise Rejection(ErrorCode.INVALID_PARAM, "租约时长必须为正整数秒")
        with self._lock:
            existing_id = self._by_key.get((owner, idempotency_key))
            if existing_id is not None:
                env = self._envs[existing_id]
                logger.info(
                    "幂等命中 owner=%s key=%s -> env=%s",
                    owner, idempotency_key, env.env_id,
                )
                self._expire_if_due_locked(env)
                return env
            # 占额度（超限抛 Rejection，不产生任何计数变化）
            self._quota.acquire(owner)
            try:
                expires_at = self._clock() + ttl_seconds if ttl_seconds else None
                env = Environment(
                    env_id=uuid.uuid4().hex[:12],
                    owner=owner,
                    idempotency_key=idempotency_key,
                    ttl_seconds=ttl_seconds,
                    expires_at=expires_at,
                )
                self._envs[env.env_id] = env
                self._by_key[(owner, idempotency_key)] = env.env_id
            except Exception:
                self._quota.release(owner)
                raise
            logger.info(
                "申请受理 env=%s owner=%s ttl_seconds=%s",
                env.env_id, owner, ttl_seconds,
            )
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
                self._release_quota_once(env)  # 释放已占用资源，允许重试
            return
        with self._lock:
            env.transition(EnvStatus.READY)
            logger.info("分配成功 env=%s resource=%s", env.env_id, resource)

    def retry(self, env_id: str, owner: str) -> Environment:
        """分配失败后的重试：仅允许从 REQUESTED 状态发起；终态环境不可拉回。"""
        with self._lock:
            env = self.get(env_id, owner)
            if env.status is EnvStatus.EXPIRED:
                # 新增的到期回收终态，给出专门原因
                raise Rejection(
                    ErrorCode.LEASE_EXPIRED,
                    f"环境 {env_id} 已到期回收，不能重试",
                )
            if env.status is not EnvStatus.REQUESTED:
                # 含已销毁(DESTROYED)等非 REQUESTED 状态：沿用既有非法流转语义
                raise Rejection(
                    ErrorCode.ILLEGAL_TRANSITION,
                    f"环境 {env_id} 当前状态 {env.status.value}，仅分配失败(REQUESTED)后可重试",
                )
            self._quota.acquire(owner)  # 重新占额度，超限则拒绝
            env.quota_held = True
        self._allocate(env)
        return env

    # ---------- 续租 ----------

    def renew_lease(self, env_id: str, owner: str, ttl_seconds: int) -> Environment:
        """续租：仅对带租约、已就绪且未到期的环境开放。

        续租只把到期时间向后延（从当前时刻起重新计时），不重复占用额度。
        已到期回收 / 已销毁 / 未就绪 / 本就无租约的环境，返回可区分原因。
        """
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or ttl_seconds <= 0
        ):
            raise Rejection(ErrorCode.INVALID_PARAM, "续租时长必须为正整数秒")
        with self._lock:
            env = self.get(env_id, owner)
            # 先惰性回收：若恰好到期，续租在此被明确拒绝
            self._expire_if_due_locked(env)
            self._guard_renew_state_locked(env)
            now = self._clock()
            env.apply_renewal(now + ttl_seconds, now)
            logger.info(
                "续租成功 env=%s owner=%s 新到期时间=%.3f",
                env.env_id, owner, env.expires_at,
            )
            return env

    # ---------- 到期回收 ----------

    def reap_expired(self) -> list[str]:
        """扫描并回收所有已到期环境，返回本次实际回收的 env_id 列表。

        可由后台定时任务或管理接口确定性触发；重复调用幂等：
        已回收的环境不会被再次回收，额度只释放一次。
        """
        reaped: list[str] = []
        with self._lock:
            # 回收不删除 dict 条目，遍历快照安全
            for env in list(self._envs.values()):
                if self._expire_if_due_locked(env):
                    reaped.append(env.env_id)
        if reaped:
            logger.info("批量到期回收完成 count=%d envs=%s", len(reaped), reaped)
        return reaped

    # ---------- 销毁 ----------

    def destroy(self, env_id: str, owner: str) -> Environment:
        with self._lock:
            env = self.get(env_id, owner)
            # 与到期回收撞车：已被回收则明确拒绝，不允许回到销毁路径
            if env.status is EnvStatus.EXPIRED:
                raise Rejection(
                    ErrorCode.LEASE_EXPIRED,
                    f"环境 {env_id} 已到期回收，无需销毁",
                )
            # 非法流转（含并发重复销毁）在此被状态机拒绝
            env.transition(EnvStatus.DESTROYING)
            env.transition(EnvStatus.DESTROYED)
            self._release_quota_once(env)  # 配对保护：重复销毁也不会多放额度
            logger.info("销毁完成 env=%s owner=%s", env.env_id, owner)
            return env
