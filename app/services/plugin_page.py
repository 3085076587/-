"""从全域投放页的 table 里抽出计划、整体消耗和净成交金额。"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

ID_RE = re.compile(r"ID[:：]\s*(\d{8,})")
TITLE_RE = re.compile(r"^\d{1,2}月\d{1,2}日|保本")
MONEY_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_uni_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup.select("script, style, noscript, #qc-fangzhou-panel, .qc-profit-badge, .qc-profit-col, .qc-profit-head"):
        node.decompose()
    return _parse_table(soup)


def _parse_table(soup: BeautifulSoup) -> list[dict]:
    metric = None
    cost_index = None
    settle_index = None
    for table in soup.find_all("table"):
        heads = [th.get_text("", strip=True) for th in table.select("thead tr th")]
        cost = next((i for i, text in enumerate(heads) if "整体消耗(元)" in text), None)
        settle = next((i for i, text in enumerate(heads) if "净成交金额(元)" in text), None)
        if cost is not None and settle is not None:
            metric = table
            cost_index = cost
            settle_index = settle
            break
    if metric is None or cost_index is None or settle_index is None:
        return []
    body_rows = metric.select("tbody tr")
    name_rows: list[Tag] = []
    for table in soup.find_all("table"):
        if table is metric:
            continue
        rows = table.select("tbody tr")
        if len(rows) == len(body_rows) and any(ID_RE.search(row.get_text("\n", strip=True)) for row in rows):
            name_rows = rows
            break
    plans: list[dict] = []
    for index, row in enumerate(body_rows):
        cells = row.find_all("td")
        name_text = name_rows[index].get_text("\n", strip=True) if index < len(name_rows) else ""
        text = f"{name_text}\n{row.get_text(chr(10), strip=True)}"
        matched = ID_RE.search(text)
        plans.append(
            {
                "id": matched.group(1) if matched else f"row-{index + 1}",
                "title": _plan_title(text) or f"计划 {index + 1}",
                "cost": _money(cells, cost_index),
                "settle": _money(cells, settle_index),
            }
        )
    return plans


def _plan_title(text: str) -> str:
    for line in str(text or "").splitlines():
        line = " ".join(line.split())
        if not line or line.startswith("ID") or line == "首页" or len(line) > 90:
            continue
        if TITLE_RE.search(line):
            return line
    return ""


def _money(cells: list[Tag], index: int) -> float | None:
    if index >= len(cells):
        return None
    head = cells[index].get_text("\n", strip=True).split("基础", 1)[0].replace(",", "")
    matched = MONEY_RE.search(head)
    if not matched:
        return None
    return float(matched.group())
