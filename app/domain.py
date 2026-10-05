"""领域模型：环境状态机与环境记录。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class EnvStatus(enum.Enum):
    """环境生命周期阶段。"""

    REQUESTED = "requested"      # 已申请
    PROVISIONING = "provisioning"  # 分配中
    READY = "ready"              # 就绪
    DESTROYING = "destroying"    # 销毁中
    DESTROYED = "destroyed"      # 已销毁


# 合法状态流转表：当前状态 -> 允许的下一状态集合
ALLOWED_TRANSITIONS: dict[EnvStatus, frozenset[EnvStatus]] = {
    EnvStatus.REQUESTED: frozenset({EnvStatus.PROVISIONING}),
    EnvStatus.PROVISIONING: frozenset({EnvStatus.READY, EnvStatus.REQUESTED}),
    EnvStatus.READY: frozenset({EnvStatus.DESTROYING}),
    EnvStatus.DESTROYING: frozenset({EnvStatus.DESTROYED}),
    EnvStatus.DESTROYED: frozenset(),
}


def can_transition(current: EnvStatus, target: EnvStatus) -> bool:
    """判断 current -> target 是否为合法流转。"""
    return target in ALLOWED_TRANSITIONS[current]


@dataclass
class Environment:
    """一条临时环境记录。"""

    env_id: str
    owner: str
    request_id: str
    status: EnvStatus = EnvStatus.REQUESTED
    detail: str = ""
    history: list[tuple[EnvStatus, EnvStatus]] = field(default_factory=list)
