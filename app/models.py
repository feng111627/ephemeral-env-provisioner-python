"""环境状态机与数据模型。

状态流转（合法边）：
    REQUESTED  -> ALLOCATING   开始分配
    ALLOCATING -> READY        分配成功
    ALLOCATING -> REQUESTED    分配失败，释放占用，允许重试
    REQUESTED  -> DESTROYING   未就绪前取消
    READY      -> DESTROYING   发起销毁
    DESTROYING -> DESTROYED    销毁完成
其余流转一律拒绝，并携带可区分原因。
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
    DESTROYED = "DESTROYED"        # 已销毁


# 合法流转表：源状态 -> 允许的目标状态集合
_ALLOWED: dict[EnvStatus, frozenset[EnvStatus]] = {
    EnvStatus.REQUESTED: frozenset({EnvStatus.ALLOCATING, EnvStatus.DESTROYING}),
    EnvStatus.ALLOCATING: frozenset({EnvStatus.READY, EnvStatus.REQUESTED}),
    EnvStatus.READY: frozenset({EnvStatus.DESTROYING}),
    EnvStatus.DESTROYING: frozenset({EnvStatus.DESTROYED}),
    EnvStatus.DESTROYED: frozenset(),
}

# 占用额度的状态（已销毁不再占用）
_QUOTA_HOLDING = frozenset({EnvStatus.REQUESTED, EnvStatus.ALLOCATING, EnvStatus.READY})


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

    def __post_init__(self) -> None:
        self.history.append(f"{self.status.value}@{self.created_at:.3f}")

    @property
    def holds_quota(self) -> bool:
        return self.status in _QUOTA_HOLDING

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
