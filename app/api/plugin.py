from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.response import ok
from app.services.plugin_page import parse_uni_html

router = APIRouter(tags=["Plugin"])


class PageHtmlIn(BaseModel):
    html: str = Field("", description="全域投放页当前 HTML")


@router.post("/plugin/page", summary="用 BeautifulSoup 解析全域投放页 HTML")
def parse_plugin_page(body: PageHtmlIn):
    return ok({"plans": parse_uni_html(body.html)})
