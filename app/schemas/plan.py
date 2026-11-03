from pydantic import BaseModel, Field


class PlanSyncOut(BaseModel):
    advertiser_id: int
    total: int
    created: int
    updated: int


class PlanReportSyncOut(BaseModel):
    advertiser_id: int
    start_date: str
    end_date: str
    total: int
    created: int
    updated: int


class SaveCommissionIn(BaseModel):
    advertiser_id: int = Field(..., description="千川子账户ID")
    ad_id: int = Field(..., description="计划ID")
    commission_pct: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="佣金比例百分数。空或 0 表示不计算利润",
    )


class UpdateRoiIn(BaseModel):
    advertiser_id: int = Field(..., description="千川子账户ID")
    ad_id: int = Field(..., description="计划ID")
    roi2_goal: float = Field(..., gt=0, description="净成交 ROI 目标，最多两位小数")
    deep_external_action: str | None = Field(
        default="AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",
        description="优化目标，默认净成交 ROI",
    )
    ad_name: str | None = Field(default=None, description="计划名称，提醒展示用")
    remind_after_seconds: int | None = Field(
        default=None,
        ge=0,
        description="设置 ROI 后多少秒提醒，0/空表示不提醒",
    )


class UpdateRoiOut(BaseModel):
    advertiser_id: int
    ad_id: int
    roi2_goal: float
    oceanengine: dict | None = None


class UpdateStatusIn(BaseModel):
    advertiser_id: int
    ad_id: int
    opt_status: str = Field(..., description="ENABLE / DISABLE")


class DeletePlanItem(BaseModel):
    ad_id: str
    status: str = ""
    opt_status: str = ""
    status_label: str = ""
    stat_cost: float | None = None


class DeletePlansIn(BaseModel):
    advertiser_id: int
    plans: list[DeletePlanItem] = Field(..., min_length=1, max_length=10)


class CreateUniPlanIn(BaseModel):
    advertiser_id: int
    name: str = Field(..., min_length=1, max_length=100)
    aweme_id: int | None = None
    smart_bid_type: str
    roi2_goal: float | None = None
    budget: float = Field(..., gt=0)
    video_schedule_type: str = "SCHEDULE_FROM_NOW"
    start_time: str | None = None
    end_time: str | None = None
    qcpx_mode: str = "QCPX_MODE_OFF"
    deep_external_action: str = "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI"
    product_ids: list[str] = Field(..., min_length=1, max_length=30)
    title: str = ""
    video_id: str | None = None
    video_cover_id: str | None = None
    video_image_mode: str | None = None
    card_image_id: str | None = None
    selling_point: str | None = None
    action_button: str = "查看详情"
