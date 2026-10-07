"""环境状态机与数据模型。

状态流转（合法边）：
    REQUESTED  -> ALLOCATING   开始分配
    ALLOCATING -> READY        分配成功
    ALLOCATING -> REQUESTED    分配失败，释放占用，允许重试
    REQUESTED  -> DESTROYING   未就绪前取消
    READY      -> DESTROYING   发起销毁
    READY      -> EXPIRED      租约到期自动回收
    DESTROYING -> DESTROYED    销毁完成
其余流转一律拒绝，并携带可区分原因。

DESTROYED / EXPIRED 均为终态：已回收或已销毁的环境不能再被续租、重试拉回。
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field

from .errors import ErrorCode, Rejection


class EnvStatus(enum.Enum):
    REQUESTED = "REQUESTED"        # 申请
    ALLOCATING = "ALLOCATING"      # 分配中
    READY = "READY"                # 就绪
    DESTROYING = "DESTROYING"      # 销毁中
    DESTROYED = "DESTROYED"        # 已销毁（终态）
    EXPIRED = "EXPIRED"            # 租约到期回收（终态）


# 合法流转表：源状态 -> 允许的目标状态集合
_ALLOWED: dict[EnvStatus, frozenset[EnvStatus]] = {
    EnvStatus.REQUESTED: frozenset({EnvStatus.ALLOCATING, EnvStatus.DESTROYING}),
    EnvStatus.ALLOCATING: frozenset({EnvStatus.READY, EnvStatus.REQUESTED}),
    EnvStatus.READY: frozenset({EnvStatus.DESTROYING, EnvStatus.EXPIRED}),
    EnvStatus.DESTROYING: frozenset({EnvStatus.DESTROYED}),
    EnvStatus.DESTROYED: frozenset(),
    EnvStatus.EXPIRED: frozenset(),
}

# 终态：任何后续操作都不允许
TERMINAL_STATUSES = frozenset({EnvStatus.DESTROYED, EnvStatus.EXPIRED})


@dataclass
class Environment:
    env_id: str
    owner: str
    idempotency_key: str
    status: EnvStatus = EnvStatus.REQUESTED
    failure_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 租约：ttl_seconds 为 None 表示未指定时长（永不过期，保持旧用法）
    ttl_seconds: int | None = None
    expires_at: float | None = None
    # 额度持有标志：与额度计数严格配对，保证释放只发生一次
    quota_held: bool = True
    history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.history.append(f"{self.status.value}@{self.created_at:.3f}")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def has_lease(self) -> bool:
        """是否带租约（指定了时长）。"""
        return self.ttl_seconds is not None

    def lease_active(self, now: float) -> bool:
        """租约是否仍有效：带租约、已就绪、且未到到期时间。"""
        return (
            self.status is EnvStatus.READY
            and self.expires_at is not None
            and now < self.expires_at
        )

    def apply_renewal(self, expires_at: float, now: float) -> None:
        """续租只改到期时间，不触碰状态与额度。"""
        self.expires_at = expires_at
        self.updated_at = now
        self.history.append(f"RENEWED@{now:.3f}(until={expires_at:.3f})")

    def transition(self, target: EnvStatus, reason: str | None = None) -> None:
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
