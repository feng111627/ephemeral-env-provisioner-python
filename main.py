"""HTTP API：自助申请 / 查询 / 重试 / 销毁 / 续租 临时环境。

错误映射：Rejection.code -> HTTP 状态码 + 可区分 error 字段；
未预期的内部异常统一返回 500 INTERNAL，不泄露堆栈。
"""
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.errors import ErrorCode, Rejection
from app.models import Environment
from app.quota import QuotaManager
from app.service import EnvService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_CODE_TO_HTTP = {
    ErrorCode.INVALID_PARAM: 400,
    ErrorCode.QUOTA_EXCEEDED_USER: 409,
    ErrorCode.QUOTA_EXCEEDED_GLOBAL: 409,
    ErrorCode.DUPLICATE_REQUEST: 409,
    ErrorCode.ILLEGAL_TRANSITION: 409,
    ErrorCode.ENV_NOT_FOUND: 404,
    ErrorCode.ALLOCATE_FAILED: 500,
    ErrorCode.LEASE_EXPIRED: 409,
    ErrorCode.ENV_DESTROYED: 409,
    ErrorCode.ENV_NOT_READY: 409,
    ErrorCode.LEASE_NOT_RENEWABLE: 409,
    ErrorCode.INTERNAL: 500,
}


def create_app(service: EnvService | None = None, reap_interval: float | None = 1.0) -> FastAPI:
    """构造应用；reap_interval 非 None 时在应用生命周期内运行后台到期回收。"""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if reap_interval is not None and service is not None:
            service.start_reaper(reap_interval)
        try:
            yield
        finally:
            if service is not None:
                service.stop_reaper()

    application = FastAPI(title="Ephemeral Env Provisioner", lifespan=lifespan)
    application.state.service = service

    @application.exception_handler(Rejection)
    async def rejection_handler(_: Request, exc: Rejection) -> JSONResponse:
        return JSONResponse(
            status_code=_CODE_TO_HTTP[exc.code],
            content={"error": exc.code.value, "message": exc.message},
        )

    @application.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 参数校验失败统一对外为 400 INVALID_PARAM，不回显 pydantic 内部结构细节
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_PARAM", "message": "请求参数不合法"},
        )

    @application.exception_handler(Exception)
    async def internal_handler(_: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("api").exception("内部异常")
        return JSONResponse(
            status_code=500, content={"error": "INTERNAL", "message": "内部错误"}
        )

    class RequestBody(BaseModel):
        owner: str = Field(min_length=1)
        idempotency_key: str = Field(min_length=1)
        ttl_seconds: float | None = Field(default=None, gt=0)

    class RenewBody(BaseModel):
        owner: str = Field(min_length=1)
        extend_seconds: float = Field(gt=0)

    def svc() -> EnvService:
        return application.state.service

    def _view(env: Environment) -> dict:
        return {
            "env_id": env.env_id,
            "owner": env.owner,
            "status": env.status.value,
            "failure_reason": env.failure_reason,
            "ttl_seconds": env.ttl_seconds,
            "lease_expires_at": env.lease_expires_at,
            "history": env.history,
        }

    @application.post("/envs", status_code=201)
    def request_env(body: RequestBody):
        return _view(svc().request_env(body.owner, body.idempotency_key, body.ttl_seconds))

    @application.get("/envs")
    def list_envs(owner: str):
        return [_view(e) for e in svc().list_by_owner(owner)]

    @application.get("/envs/{env_id}")
    def get_env(env_id: str, owner: str):
        return _view(svc().get(env_id, owner))

    @application.post("/envs/{env_id}/retry")
    def retry_env(env_id: str, owner: str):
        return _view(svc().retry(env_id, owner))

    @application.delete("/envs/{env_id}")
    def destroy_env(env_id: str, owner: str):
        return _view(svc().destroy(env_id, owner))

    @application.post("/envs/{env_id}/renew")
    def renew_env(env_id: str, body: RenewBody):
        return _view(svc().renew(env_id, body.owner, body.extend_seconds))

    @application.post("/reap")
    def reap_expired():
        """确定性地触发一次到期回收（测试/运维用，返回回收数量）。"""
        return {"reaped": svc().reap_expired()}

    @application.get("/quota")
    def quota(owner: str):
        return svc().usage(owner)

    return application


# 默认单例：供 uvicorn main:app 使用
service = EnvService(QuotaManager(per_user_limit=3, global_limit=10))
app = create_app(service)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
