"""HTTP API：自助申请 / 查询 / 重试 / 销毁临时环境。

错误映射：Rejection.code -> HTTP 状态码 + 可区分 error 字段；
未预期的内部异常统一返回 500 INTERNAL，不泄露堆栈。
"""
import logging

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.errors import ErrorCode, Rejection
from app.models import Environment
from app.quota import QuotaManager
from app.service import EnvService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Ephemeral Env Provisioner")
service = EnvService(QuotaManager(per_user_limit=3, global_limit=10))

_CODE_TO_HTTP = {
    ErrorCode.INVALID_PARAM: 400,
    ErrorCode.QUOTA_EXCEEDED_USER: 409,
    ErrorCode.QUOTA_EXCEEDED_GLOBAL: 409,
    ErrorCode.DUPLICATE_REQUEST: 409,
    ErrorCode.ILLEGAL_TRANSITION: 409,
    ErrorCode.ENV_NOT_FOUND: 404,
    ErrorCode.ALLOCATE_FAILED: 500,
    ErrorCode.INTERNAL: 500,
}


@app.exception_handler(Rejection)
async def rejection_handler(_: Request, exc: Rejection) -> JSONResponse:
    return JSONResponse(
        status_code=_CODE_TO_HTTP[exc.code],
        content={"error": exc.code.value, "message": exc.message},
    )


@app.exception_handler(Exception)
async def internal_handler(_: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("api").exception("内部异常")
    return JSONResponse(status_code=500, content={"error": "INTERNAL", "message": "内部错误"})


class RequestBody(BaseModel):
    owner: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


def _view(env: Environment) -> dict:
    return {
        "env_id": env.env_id,
        "owner": env.owner,
        "status": env.status.value,
        "failure_reason": env.failure_reason,
        "history": env.history,
    }


@app.post("/envs", status_code=201)
def request_env(body: RequestBody):
    return _view(service.request_env(body.owner, body.idempotency_key))


@app.get("/envs")
def list_envs(owner: str):
    return [_view(e) for e in service.list_by_owner(owner)]


@app.get("/envs/{env_id}")
def get_env(env_id: str, owner: str):
    return _view(service.get(env_id, owner))


@app.post("/envs/{env_id}/retry")
def retry_env(env_id: str, owner: str):
    return _view(service.retry(env_id, owner))


@app.delete("/envs/{env_id}")
def destroy_env(env_id: str, owner: str):
    return _view(service.destroy(env_id, owner))


@app.get("/quota")
def quota(owner: str):
    return service.usage(owner)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
