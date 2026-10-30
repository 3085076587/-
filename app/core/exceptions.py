"""业务异常与全局异常处理。"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.response import fail


class AppError(Exception):
    """业务异常，最终转成统一响应。"""

    def __init__(self, msg: str, code: int = 40001, data: Any = None):
        super().__init__(msg)
        self.msg = msg
        self.code = code
        self.data = data


class OceanEngineApiError(AppError):
    """巨量开放平台返回的业务错误。"""

    def __init__(self, msg: str, ocean_code: int | None = None, data: Any = None):
        super().__init__(msg=msg, code=ocean_code or 50201, data=data)
        self.ocean_code = ocean_code


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        return JSONResponse(
            status_code=200,
            content=fail(exc.code, exc.msg, exc.data),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=200,
            content=fail(42200, "请求参数校验失败", exc.errors()),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception):
        return JSONResponse(
            status_code=200,
            content=fail(50000, f"服务器异常：{exc}"),
        )
