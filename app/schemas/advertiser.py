from pydantic import BaseModel, Field


class AdvertiserSyncOut(BaseModel):
    agent_id: int
    total: int = Field(..., description="接口返回的子账户总数")
    created: int
    updated: int
