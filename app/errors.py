"""可区分的错误码与拒绝结果定义（骨架）。"""
from __future__ import annotations

import enum


class ErrorCode(enum.Enum):
    """所有对调用方可区分的拒绝/失败原因。"""

    INVALID_PARAM = "INVALID_PARAM"
    QUOTA_EXCEEDED_USER = "QUOTA_EXCEEDED_USER"
    QUOTA_EXCEEDED_GLOBAL = "QUOTA_EXCEEDED_GLOBAL"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    ENV_NOT_FOUND = "ENV_NOT_FOUND"
    ALLOCATE_FAILED = "ALLOCATE_FAILED"
    LEASE_EXPIRED = "LEASE_EXPIRED"              # 租约已到期（可能已被回收）
    ENV_DESTROYED = "ENV_DESTROYED"              # 环境已手动销毁
    ENV_NOT_READY = "ENV_NOT_READY"              # 环境尚未就绪，不可续租
    LEASE_NOT_RENEWABLE = "LEASE_NOT_RENEWABLE"  # 环境未设置租约（永久保留）
    INTERNAL = "INTERNAL"


class Rejection(Exception):
    """业务拒绝：携带可区分错误码，绝不泄露内部异常细节。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
