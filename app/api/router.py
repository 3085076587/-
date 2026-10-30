from fastapi import APIRouter

from app.api import advertiser, auth, oauth, plan, query, reminder, token

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(oauth.router)
api_router.include_router(advertiser.router)
api_router.include_router(plan.router)
api_router.include_router(reminder.router)
api_router.include_router(token.router)
api_router.include_router(query.router)
