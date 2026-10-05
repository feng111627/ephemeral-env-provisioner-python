"""额度管理：每用户上限 + 全局限额。

- 所有计数变更都在同一把锁内完成，保证并发下不超发、不少算。
- acquire 是"全有或全无"：任一维度超限即拒绝且不产生任何计数变化。
- release 与 acquire 严格配对；对未持有额度的用户 release 视为内部错误，
  抛出 AssertionError（由上层兜底为 INTERNAL，不泄露细节）。
"""
from __future__ import annotations

import threading

from .errors import ErrorCode, Rejection


class QuotaManager:
    def __init__(self, per_user_limit: int, global_limit: int) -> None:
        if per_user_limit <= 0 or global_limit <= 0:
            raise Rejection(ErrorCode.INVALID_PARAM, "额度上限必须为正整数")
        if per_user_limit > global_limit:
            raise Rejection(ErrorCode.INVALID_PARAM, "单用户额度不能超过全局额度")
        self._per_user_limit = per_user_limit
        self._global_limit = global_limit
        self._lock = threading.Lock()
        self._user_used: dict[str, int] = {}
        self._global_used = 0

    def acquire(self, user: str) -> None:
        """为用户占用 1 份额度；超限则抛出可区分原因的 Rejection。"""
        with self._lock:
            if self._global_used >= self._global_limit:
                raise Rejection(
                    ErrorCode.QUOTA_EXCEEDED_GLOBAL,
                    f"全局额度已达上限 {self._global_limit}",
                )
            used = self._user_used.get(user, 0)
            if used >= self._per_user_limit:
                raise Rejection(
                    ErrorCode.QUOTA_EXCEEDED_USER,
                    f"用户 {user} 额度已达上限 {self._per_user_limit}",
                )
            self._user_used[user] = used + 1
            self._global_used += 1

    def release(self, user: str) -> None:
        """释放 1 份额度，必须与 acquire 配对。"""
        with self._lock:
            used = self._user_used.get(user, 0)
            assert used > 0 and self._global_used > 0, "额度释放与占用不配对"
            self._user_used[user] = used - 1
            self._global_used -= 1

    def usage(self, user: str) -> dict[str, int]:
        with self._lock:
            return {
                "user_used": self._user_used.get(user, 0),
                "user_limit": self._per_user_limit,
                "global_used": self._global_used,
                "global_limit": self._global_limit,
            }
