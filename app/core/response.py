"""统一响应体：{"code": int, "msg": str, "data": any}。"""

from typing import Any

from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    code: int = Field(0, description="0 成功，非 0 业务异常")
    msg: str = Field("ok", description="提示信息")
    data: Any = Field(None, description="业务数据")


def ok(data: Any = None, msg: str = "ok") -> dict[str, Any]:
    return {"code": 0, "msg": msg, "data": data}


def fail(code: int, msg: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}
