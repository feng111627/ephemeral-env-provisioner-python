"""环境状态机与数据模型。

状态流转（合法边）：
    REQUESTED  -> ALLOCATING   开始分配
    ALLOCATING -> READY        分配成功
    ALLOCATING -> REQUESTED    分配失败，释放占用，允许重试
    REQUESTED  -> DESTROYING   未就绪前取消
    READY      -> DESTROYING   发起销毁
    DESTROYING -> DESTROYED    销毁完成
    REQUESTED  -> EXPIRED      租约到期回收（未就绪）
    READY      -> EXPIRED      租约到期回收
其余流转一律拒绝，并携带可区分原因。

EXPIRED 与 DESTROYED 同为终态：已回收/已销毁的环境不能再被续租或重试拉回。
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Callable

from .errors import ErrorCode, Rejection


class EnvStatus(enum.Enum):
    REQUESTED = "REQUESTED"        # 申请
    ALLOCATING = "ALLOCATING"      # 分配中
    READY = "READY"                # 就绪
    DESTROYING = "DESTROYING"      # 销毁中
    DESTROYED = "DESTROYED"        # 已销毁
    EXPIRED = "EXPIRED"            # 租约到期已回收（终态）


# 合法流转表：源状态 -> 允许的目标状态集合
_ALLOWED: dict[EnvStatus, frozenset[EnvStatus]] = {
    EnvStatus.REQUESTED: frozenset({EnvStatus.ALLOCATING, EnvStatus.DESTROYING, EnvStatus.EXPIRED}),
    EnvStatus.ALLOCATING: frozenset({EnvStatus.READY, EnvStatus.REQUESTED}),
    EnvStatus.READY: frozenset({EnvStatus.DESTROYING, EnvStatus.EXPIRED}),
    EnvStatus.DESTROYING: frozenset({EnvStatus.DESTROYED}),
    EnvStatus.DESTROYED: frozenset(),
    EnvStatus.EXPIRED: frozenset(),
}

# 占用额度的状态（已销毁/已回收不再占用）
_QUOTA_HOLDING = frozenset(
    {EnvStatus.REQUESTED, EnvStatus.ALLOCATING, EnvStatus.READY}
)


@dataclass
class Environment:
    env_id: str
    owner: str
    idempotency_key: str
    status: EnvStatus = EnvStatus.REQUESTED
    failure_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    history: list[str] = field(default_factory=list)
    # 租约：ttl_seconds 为 None 表示未指定时长，环境永久保留（旧用法）。
    # lease_expires_at 为绝对到期时间；None 表示无租约。
    ttl_seconds: float | None = None
    lease_expires_at: float | None = None
    # 是否实际占用着 1 份额度：与 QuotaManager 计数一一对应。
    # 分配失败回滚后状态仍是 REQUESTED，但已不占额度，靠该标记区分。
    quota_held: bool = False

    def __post_init__(self) -> None:
        self.history.append(f"{self.status.value}@{self.created_at:.3f}")

    @property
    def holds_quota(self) -> bool:
        return self.status in _QUOTA_HOLDING and self.quota_held

    def is_expired(self, now: float) -> bool:
        """是否已到租约到期时间。无租约的环境永不过期。"""
        return self.lease_expires_at is not None and now >= self.lease_expires_at

    def extend_lease(self, extend_seconds: float, now: float) -> None:
        """把租约到期时间向后延 extend_seconds 秒，不改变任何额度计数。

        从当前到期时间与当前时间中较晚者起算，允许到期前/临界时多次续租。
        """
        base = max(self.lease_expires_at or now, now)
        self.lease_expires_at = base + extend_seconds
        self.updated_at = now
        self.history.append(f"RENEWED@{now:.3f}(until={self.lease_expires_at:.3f})")

    def transition(self, target: EnvStatus, reason: str | None = None,
                   clock: Callable[[], float] = time.time) -> None:
        """执行状态流转；非法流转抛出带 ILLEGAL_TRANSITION 码的 Rejection。"""
        if target not in _ALLOWED[self.status]:
            raise Rejection(
                ErrorCode.ILLEGAL_TRANSITION,
                f"环境 {self.env_id} 不允许从 {self.status.value} 流转到 {target.value}",
            )
        self.status = target
        self.updated_at = time.time()
        self.failure_reason = reason
        entry = f"{target.value}@{self.updated_at:.3f}"
        if reason:
            entry += f"({reason})"
        self.history.append(entry)
