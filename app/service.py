"""环境生命周期服务。

职责：
- 申请幂等：同一 (owner, idempotency_key) 重复提交返回同一环境，不重复占额度。
- 分配：REQUESTED -> ALLOCATING -> READY；失败回滚到 REQUESTED 并释放额度，可重试。
- 销毁：READY/REQUESTED -> DESTROYING -> DESTROYED，销毁完成即释放额度。
- 租约：申请时可指定 ttl_seconds；到期由回收器把环境收进终态 EXPIRED 并释放额度。
  未指定时长（ttl=None）的环境永久保留，保持旧用法。
- 续租：仅 READY 且租约未到期的环境可续租，只后移到期时间，不重复占用额度。
- 并发：所有状态迁移与额度计数的配对都在同一把锁内完成；
  到期回收、续租、销毁、申请、重试任意交错，回收/销毁只发生一次、额度只放开一次。
- 内部异常只记日志，对外统一为 ALLOCATE_FAILED / INTERNAL，不泄露细节。
"""
from __future__ import annotations

import logging
import math
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
Clock = Callable[[], float]


def _default_allocator(env: Environment) -> str:
    return f"res-{env.env_id}"


class EnvService:
    def __init__(
        self,
        quota: QuotaManager,
        allocator: Allocator | None = None,
        clock: Clock = time.time,
        reap_interval: float | None = None,
    ) -> None:
        """reap_interval 非 None 时启动后台回收线程（秒）。"""
        self._quota = quota
        self._allocator = allocator or _default_allocator
        self._clock = clock
        self._lock = threading.RLock()
        self._envs: dict[str, Environment] = {}
        self._by_key: dict[tuple[str, str], str] = {}  # (owner, idem_key) -> env_id
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()
        if reap_interval is not None:
            self.start_reaper(reap_interval)

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

    # ---------- 申请（幂等，可带租约时长） ----------

    def request_env(
        self,
        owner: str,
        idempotency_key: str,
        ttl_seconds: float | None = None,
    ) -> Environment:
        if not owner or not owner.strip():
            raise Rejection(ErrorCode.INVALID_PARAM, "申请人不能为空")
        if not idempotency_key or not idempotency_key.strip():
            raise Rejection(ErrorCode.INVALID_PARAM, "幂等键不能为空")
        if ttl_seconds is not None and (
            not math.isfinite(ttl_seconds) or ttl_seconds <= 0
        ):
            raise Rejection(ErrorCode.INVALID_PARAM, "租约时长必须为正数（秒）")
        with self._lock:
            existing_id = self._by_key.get((owner, idempotency_key))
            if existing_id is not None:
                env = self._envs[existing_id]
                logger.info("幂等命中 owner=%s key=%s -> env=%s", owner, idempotency_key, env.env_id)
                return env
            # 占额度（超限抛 Rejection，不产生任何计数变化）
            self._quota.acquire(owner)
            try:
                now = self._clock()
                env = Environment(
                    env_id=uuid.uuid4().hex[:12],
                    owner=owner,
                    idempotency_key=idempotency_key,
                    created_at=now,
                    updated_at=now,
                    ttl_seconds=ttl_seconds,
                    lease_expires_at=(now + ttl_seconds) if ttl_seconds else None,
                    quota_held=True,
                )
                self._envs[env.env_id] = env
                self._by_key[(owner, idempotency_key)] = env.env_id
            except Exception:
                self._quota.release(owner)
                raise
            logger.info(
                "申请受理 env=%s owner=%s ttl=%s", env.env_id, owner, ttl_seconds
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
            env.transition(EnvStatus.ALLOCATING, clock=self._clock)
        try:
            resource = self._allocator(env)
        except Exception:
            logger.exception("分配失败 env=%s owner=%s", env.env_id, env.owner)
            with self._lock:
                if env.status is not EnvStatus.ALLOCATING:
                    return
                env.transition(EnvStatus.REQUESTED, reason="ALLOCATE_FAILED",
                               clock=self._clock)
                # 释放已占用资源，允许重试；quota_held 保证只释放一次
                if env.quota_held:
                    env.quota_held = False
                    self._quota.release(env.owner)
            return
        with self._lock:
            if env.status is not EnvStatus.ALLOCATING:
                # 锁外分配期间环境已被回收/销毁，分配结果作废，额度由对端负责
                return
            env.transition(EnvStatus.READY, clock=self._clock)
            logger.info("分配成功 env=%s resource=%s", env.env_id, resource)
            # 分配耗时跨过了到期时间：READY 落库即到期回收，不多活一个回收周期
            if env.is_expired(self._clock()):
                self._expire_locked(env, reason="LEASE_EXPIRED")

    def retry(self, env_id: str, owner: str) -> Environment:
        """分配失败后的重试：仅允许从 REQUESTED 状态发起。"""
        with self._lock:
            env = self.get(env_id, owner)
            if env.status is EnvStatus.EXPIRED or env.status is EnvStatus.DESTROYED:
                raise Rejection(
                    ErrorCode.ILLEGAL_TRANSITION,
                    f"环境 {env_id} 已处于终态 {env.status.value}，不可重试",
                )
            if env.status is not EnvStatus.REQUESTED:
                raise Rejection(
                    ErrorCode.ILLEGAL_TRANSITION,
                    f"环境 {env_id} 当前状态 {env.status.value}，仅分配失败(REQUESTED)后可重试",
                )
            # 已到期但尚未被后台回收：先按到期回收处理，拒绝重试
            if env.is_expired(self._clock()):
                self._expire_locked(env, reason="LEASE_EXPIRED")
                raise Rejection(
                    ErrorCode.LEASE_EXPIRED,
                    f"环境 {env_id} 租约已到期，不可重试",
                )
            self._quota.acquire(owner)  # 重新占额度，超限则拒绝
            env.quota_held = True
        self._allocate(env)
        return env

    # ---------- 销毁 ----------

    def destroy(self, env_id: str, owner: str) -> Environment:
        with self._lock:
            env = self.get(env_id, owner)
            # 非法流转（含并发重复销毁/销毁已回收环境）在此被状态机拒绝
            env.transition(EnvStatus.DESTROYING, clock=self._clock)
            env.transition(EnvStatus.DESTROYED, clock=self._clock)
            if env.quota_held:
                env.quota_held = False
                self._quota.release(owner)
            logger.info("销毁完成 env=%s owner=%s", env.env_id, owner)
            return env

    # ---------- 续租 ----------

    def renew(self, env_id: str, owner: str, extend_seconds: float) -> Environment:
        """续租：把到期时间向后延。续租不重复占用额度。

        拒绝原因可区分：
        - INVALID_PARAM：extend_seconds 非正数；
        - ENV_NOT_FOUND：环境不存在/非本人；
        - ENV_DESTROYED：环境已销毁；
        - LEASE_EXPIRED：租约已到期（可能已被回收）；
        - ENV_NOT_READY：环境未处于 READY（申请中/分配中/分配失败）；
        - LEASE_NOT_RENEWABLE：环境未设置租约，本就永久保留。
        """
        if extend_seconds is None or not math.isfinite(extend_seconds) or extend_seconds <= 0:
            raise Rejection(ErrorCode.INVALID_PARAM, "续租时长必须为正数（秒）")
        with self._lock:
            env = self.get(env_id, owner)
            if env.status is EnvStatus.DESTROYED:
                raise Rejection(
                    ErrorCode.ENV_DESTROYED, f"环境 {env_id} 已销毁，不可续租"
                )
            if env.status is EnvStatus.EXPIRED:
                raise Rejection(
                    ErrorCode.LEASE_EXPIRED, f"环境 {env_id} 已到期回收，不可续租"
                )
            if env.lease_expires_at is None:
                raise Rejection(
                    ErrorCode.LEASE_NOT_RENEWABLE,
                    f"环境 {env_id} 未设置租约，永久保留，无需续租",
                )
            if env.status is not EnvStatus.READY:
                raise Rejection(
                    ErrorCode.ENV_NOT_READY,
                    f"环境 {env_id} 当前状态 {env.status.value}，仅就绪(READY)后可续租",
                )
            now = self._clock()
            if env.is_expired(now):
                # 到期与续租临界：到期优先，直接收进终态
                self._expire_locked(env, reason="LEASE_EXPIRED")
                raise Rejection(
                    ErrorCode.LEASE_EXPIRED, f"环境 {env_id} 租约已到期，不可续租"
                )
            env.extend_lease(extend_seconds, now)
            logger.info(
                "续租成功 env=%s owner=%s +%ss until=%.3f",
                env.env_id, owner, extend_seconds, env.lease_expires_at,
            )
            return env

    # ---------- 到期回收 ----------

    def reap_expired(self) -> int:
        """扫描并回收所有已到期环境，返回本次回收数量。

        可被后台线程、外部定时器或测试确定性地重复调用：
        已回收/已销毁的环境不会被再次处理，额度只放开一次。
        """
        reaped = 0
        with self._lock:
            now = self._clock()
            # 快照：分配中的环境留给分配结束后的下一轮回收，避免与分配结果竞争
            for env in list(self._envs.values()):
                if env.status not in (EnvStatus.READY, EnvStatus.REQUESTED):
                    continue
                if not env.is_expired(now):
                    continue
                self._expire_locked(env, reason="LEASE_EXPIRED")
                reaped += 1
        if reaped:
            logger.info("到期回收完成 count=%d", reaped)
        return reaped

    def _expire_locked(self, env: Environment, reason: str) -> None:
        """调用方必须持有 self._lock。终态流转 + 一次性释放额度。"""
        env.transition(EnvStatus.EXPIRED, reason=reason, clock=self._clock)
        if env.quota_held:
            env.quota_held = False
            self._quota.release(env.owner)
        logger.info("环境到期回收 env=%s owner=%s", env.env_id, env.owner)

    # ---------- 后台回收线程 ----------

    def start_reaper(self, interval: float) -> None:
        """启动后台回收线程（可重复调用，保证只启动一个）。"""
        if interval <= 0:
            raise Rejection(ErrorCode.INVALID_PARAM, "回收间隔必须为正数（秒）")
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._stop.clear()
            self._reaper = threading.Thread(
                target=self._reap_loop,
                args=(interval,),
                name="lease-reaper",
                daemon=True,
            )
            self._reaper.start()
            logger.info("到期回收线程已启动 interval=%ss", interval)

    def stop_reaper(self, timeout: float = 1.0) -> None:
        self._stop.set()
        thread = self._reaper
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def _reap_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.reap_expired()
            except Exception:
                logger.exception("到期回收轮询异常")
