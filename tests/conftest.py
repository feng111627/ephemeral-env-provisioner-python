import logging

import pytest

from app.quota import QuotaManager
from app.service import EnvService


@pytest.fixture(autouse=True)
def log_case_boundary(request, caplog):
    """让每个用例在日志中标注所属业务域与用例名，便于审阅覆盖情况。"""
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test")
    log.info("=== 用例开始 [%s] %s ===", request.node.path.name, request.node.name)
    yield
    log.info("=== 用例结束 [%s] %s ===", request.node.path.name, request.node.name)


@pytest.fixture
def service():
    return EnvService(QuotaManager(per_user_limit=2, global_limit=4))
