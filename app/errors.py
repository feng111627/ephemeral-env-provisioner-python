"""可区分的拒绝/失败原因与业务异常。"""
from __future__ import annotations

import enum


class RejectionReason(enum.Enum):
    """请求被拒绝或失败的可区分原因。"""

    INVALID_ARGUMENT = "invalid_argument"        # 非法参数
    QUOTA_EXCEEDED_USER = "quota_exceeded_user"  # 超出个人额度
    QUOTA_EXCEEDED_GLOBAL = "quota_exceeded_global"  # 超出整体额度
    ILLEGAL_TRANSITION = "illegal_transition"    # 非法状态流转
    NOT_FOUND = "not_found"                      # 环境不存在
    FORBIDDEN = "forbidden"                      # 非属主操作
    INTERNAL = "internal"                        # 内部异常（不暴露细节）


class EnvError(Exception):
    """业务异常：携带可区分的原因，内部细节不直接暴露给调用方。"""

    def __init__(self, reason: RejectionReason, message: str = "") -> None:
        self.reason = reason
        self.message = message or reason.value
        super().__init__(self.message)
