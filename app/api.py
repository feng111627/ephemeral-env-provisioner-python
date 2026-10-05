"""HTTP API 层：参数校验与错误映射，内部异常不暴露给调用方。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.domain import Environment
from app.errors import EnvError, RejectionReason
from app.service import EnvService

logger = logging.getLogger("env_provisioner.api")

# 拒绝原因 -> HTTP 状态码映射，保证不同原因可区分
_REASON_STATUS = {
    RejectionReason.INVALID_ARGUMENT: 400,
    RejectionReason.QUOTA_EXCEEDED_USER: 409,
    RejectionReason.QUOTA_EXCEEDED_GLOBAL: 409,
    RejectionReason.ILLEGAL_TRANSITION: 409,
    RejectionReason.NOT_FOUND: 404,
    RejectionReason.FORBIDDEN: 403,
    RejectionReason.INTERNAL: 500,
}


class ApplyRequest(BaseModel):
    owner: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class DestroyRequest(BaseModel):
    operator: str = Field(min_length=1)


def _env_view(env: Environment) -> dict:
    return {
        "env_id": env.env_id,
        "owner": env.owner,
        "request_id": env.request_id,
        "status": env.status.value,
        "history": [[a.value, b.value] for a, b in env.history],
    }


def create_app(service: EnvService | None = None) -> FastAPI:
    app = FastAPI(title="ephemeral-env-provisioner")
    app.state.service = service or EnvService()

    @app.exception_handler(EnvError)
    async def env_error_handler(_req: Request, exc: EnvError) -> JSONResponse:
        return JSONResponse(
            status_code=_REASON_STATUS[exc.reason],
            content={"reason": exc.reason.value, "message": exc.message},
        )

    @app.exception_handler(Exception)
    async def unknown_error_handler(_req: Request, exc: Exception) -> JSONResponse:
        logger.exception("未预期的内部异常")
        return JSONResponse(
            status_code=500,
            content={
                "reason": RejectionReason.INTERNAL.value,
                "message": "内部错误",
            },
        )

    def svc(request: Request) -> EnvService:
        return request.app.state.service

    @app.post("/envs", status_code=201)
    def apply(body: ApplyRequest, request: Request) -> dict:
        env = svc(request).apply(body.owner, body.request_id)
        return _env_view(env)

    @app.post("/envs/retry", status_code=200)
    def retry(body: ApplyRequest, request: Request) -> dict:
        env = svc(request).retry_provision(body.owner, body.request_id)
        return _env_view(env)

    @app.get("/envs/{env_id}")
    def get(env_id: str, request: Request) -> dict:
        return _env_view(svc(request).get(env_id))

    @app.post("/envs/{env_id}/destroy")
    def destroy(env_id: str, body: DestroyRequest, request: Request) -> dict:
        return _env_view(svc(request).destroy(env_id, body.operator))

    @app.get("/quota/{owner}")
    def quota(owner: str, request: Request) -> dict:
        return svc(request).quota_usage(owner)

    return app
