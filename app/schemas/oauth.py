from datetime import datetime

from pydantic import BaseModel, Field


class SaveTokenIn(BaseModel):
    access_token: str = Field(..., description="巨量 access_token")
    refresh_token: str = Field(..., description="巨量 refresh_token")
    expires_in: int = Field(..., description="access_token 有效秒数，通常 86400")
    refresh_token_expires_in: int = Field(
        ..., description="refresh_token 有效秒数，通常 2592000"
    )


class SaveTokenOut(BaseModel):
    id: int
    expires_at: datetime
    refresh_expires_at: datetime
    agent_id: int | None = None


class FetchAgentIdOut(BaseModel):
    agent_id: int
    agent_name: str | None = None
    account_role: str | None = None


class TokenRefreshOut(BaseModel):
    expires_at: datetime
    refresh_expires_at: datetime
    agent_id: int | None = None
