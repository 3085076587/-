from fastapi import APIRouter

from app.core.response import ok
from app.services import reminder_service

router = APIRouter(tags=["Reminder"])


@router.get("/reminders", summary="列出 ROI 提醒（倒计时+待弹窗）")
async def list_reminders():
    data = await reminder_service.list_active()
    return ok(data)


@router.get("/reminders/due", summary="取出需要弹窗的到期提醒")
async def due_reminders():
    data = await reminder_service.pop_due_for_notify()
    return ok(data)


@router.post("/reminders/{reminder_id}/ack", summary="确认已读提醒弹窗")
async def ack_reminder(reminder_id: str):
    ok_flag = await reminder_service.ack_reminder(reminder_id)
    return ok({"acked": ok_flag, "id": reminder_id})
