"""pytest 公共配置：让测试日志能看出覆盖的业务与各用例结果。"""
from __future__ import annotations

import logging

import pytest

logger = logging.getLogger("env_provisioner.tests")

# 测试文件 -> 业务领域说明，用于日志展示覆盖范围
BUSINESS_AREAS = {
    "test_lifecycle": "生命周期状态机流转",
    "test_quota_concurrency": "并发额度控制",
    "test_retry_idempotency": "失败重试与幂等",
    "test_api": "HTTP API 错误映射",
}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    area = BUSINESS_AREAS.get(item.module.__name__.split(".")[-1], "其他")
    logger.info("[用例开始] 业务=%s 用例=%s", area, item.name)


@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call":
        area = BUSINESS_AREAS.get(report.nodeid.split("::")[0].split("/")[-1].removesuffix(".py"), "其他")
        logger.info(
            "[用例结束] 业务=%s 用例=%s 结果=%s 耗时=%.3fs",
            area, report.nodeid, report.outcome.upper(), report.duration,
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    logger.info("[测试汇总] 覆盖业务=%s 退出码=%s", sorted(BUSINESS_AREAS.values()), exitstatus)
