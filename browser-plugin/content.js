(() => {
  if (globalThis.__qcFangzhouLoaded) return;
  globalThis.__qcFangzhouLoaded = true;
  const ROI_KEY = "qc_breakeven_roi_v1";
  const POS_KEY = "qc_panel_pos_v1";
  const PANEL_ID = "qc-fangzhou-panel";
  let roiMap = {};
  let timer = 0;
  let painting = false;
  let panelOpen = true;
  const metricCache = {};

  function onUniPage() {
    return location.pathname === "/uni-prom" || location.pathname.startsWith("/uni-prom/");
  }

  function clearInjected() {
    const panel = document.getElementById(PANEL_ID);
    if (panel) panel.remove();
    document.querySelectorAll(".qc-profit-badge, .qc-profit-col, .qc-profit-head, #qc-profit-summary, #qc-roi-mode-bar, .qc-profit-cell").forEach((node) => node.remove());
  }

  function placePanel(panel) {
    if (!panel || !panelPos) return;
    panel.style.right = "auto";
    panel.style.left = `${panelPos.left}px`;
    panel.style.top = `${panelPos.top}px`;
  }

  function enableDrag(panel) {
    const header = panel.querySelector("header");
    if (!header || header.dataset.drag) return;
    header.dataset.drag = "1";
    header.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button, input, label, a")) return;
      const rect = panel.getBoundingClientRect();
      const offsetX = event.clientX - rect.left;
      const offsetY = event.clientY - rect.top;
      const move = (ev) => {
        const width = panel.offsetWidth || rect.width;
        const left = Math.min(window.innerWidth - 48, Math.max(48 - width, ev.clientX - offsetX));
        const top = Math.min(window.innerHeight - 36, Math.max(0, ev.clientY - offsetY));
        panel.style.right = "auto";
        panel.style.left = `${left}px`;
        panel.style.top = `${top}px`;
      };
      const up = () => {
        header.removeEventListener("pointermove", move);
        header.removeEventListener("pointerup", up);
        panelPos = { left: parseFloat(panel.style.left), top: parseFloat(panel.style.top) };
        chrome.storage.local.set({ [POS_KEY]: panelPos });
      };
      header.setPointerCapture(event.pointerId);
      header.addEventListener("pointermove", move);
      header.addEventListener("pointerup", up);
    });
  }
  function loadRoi() {
    chrome.storage.local.get([ROI_KEY, POS_KEY], (data) => {
      roiMap = (data && data[ROI_KEY]) || {};
      const pos = data && data[POS_KEY];
      if (pos && Number.isFinite(pos.left) && Number.isFinite(pos.top)) panelPos = pos;
      placePanel(document.getElementById(PANEL_ID));
      watchTable();
    });
  }

  function recordOf(planId) {
    const saved = roiMap[planId];
    if (saved && typeof saved === "object") return { commission: saved.commission || "", refund: saved.refund || "" };
    return { commission: "", refund: "" };
  }

  function saveField(planId, field, value, after) {
    const current = recordOf(planId);
    current[field] = String(value ?? "").trim();
    if (!current.commission && !current.refund) delete roiMap[planId];
    else roiMap[planId] = current;
    chrome.storage.local.set({ [ROI_KEY]: roiMap });
    pagePlans.forEach((plan) => {
      if (plan.id !== planId) return;
      plan.saved = { commission: current.commission || "", refund: current.refund || "" };
      plan.refund = parseRefund(plan.saved.refund);
    });
    if (after) after();
    else paint();
  }

  function parseRefund(text) {
    if (String(text ?? "").trim() === "") return 0;
    let num = Number(String(text).replace("%", "").trim());
    if (!Number.isFinite(num) || num < 0) return null;
    if (num > 1) num /= 100;
    if (num > 1) return null;
    return num;
  }

  function commissionFromName(text) {
    const named = String(text || "").match(/佣金\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%/);
    if (!named) return null;
    const pct = Number(named[1]);
    if (pct > 0 && pct <= 100) return pct / 100;
    return null;
  }

  function commissionOf(saved, text) {
    const raw = String(saved.commission || "").replace("%", "").trim();
    if (raw !== "") {
      let num = Number(raw);
      if (!Number.isFinite(num) || num <= 0) return null;
      if (num > 1) num /= 100;
      if (num > 1) return null;
      return num;
    }
    return commissionFromName(text);
  }

  function filledLine(saved) {
    const parts = [];
    const refund = String(saved.refund || "").replace("%", "").trim();
    const commission = String(saved.commission || "").replace("%", "").trim();
    if (refund !== "") parts.push(`实际退款率 ${refund}%`);
    if (commission !== "") parts.push(`佣金 ${commission}%`);
    return parts.join(" · ");
  }

  function moneyText(value) {
    const abs = Math.abs(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (value > 0) return `+¥${abs}`;
    if (value < 0) return `-¥${abs}`;
    return "¥0.00";
  }

  function skipped(el) {
    return !el || !!el.closest(`#${PANEL_ID}, .qc-profit-badge, .qc-profit-col, .qc-profit-head`);
  }

  function normLabel(text) {
    return String(text || "")
      .replace(/[\s\u200b\u200c\u200d\ufeff]/g, "")
      .replace(/[（(][^）)]*[）)]?/g, "");
  }

  function planText(textNode) {
    let el = textNode.parentElement;
    let best = "";
    for (let depth = 0; depth < 14 && el && el !== document.body; depth += 1) {
      const ids = String(el.innerText || "").match(/ID[:：]\s*\d{8,}/g) || [];
      if (ids.length > 1) break;
      const text = el.innerText || "";
      if (text.length > best.length && text.length < 2500) best = text;
      el = el.parentElement;
    }
    return best;
  }

  function labelHit(compact, names) {
    return names.some((name) => {
      if (compact === name) return true;
      if (!compact.startsWith(name)) return false;
      if (!name.includes("ROI") && compact.includes("ROI")) return false;
      return compact.length <= name.length + 2;
    });
  }

  function overlapsX(cell, column) {
    return cell.right >= column.left - 28 && cell.left <= column.right + 28;
  }

  function eachElement(visitor) {
    const visit = (root) => {
      if (!root || !root.querySelectorAll) return;
      root.querySelectorAll("div, span, td, th, p, b, strong, em, a, button, label").forEach((el) => {
        visitor(el);
        if (el.shadowRoot) visit(el.shadowRoot);
      });
    };
    visit(document.body);
  }

  function headerCandidates(names) {
    const found = [];
    eachElement((el) => {
      if (skipped(el)) return;
      const rough = el.textContent || "";
      if (!rough || rough.length > 32) return;
      const compact = normLabel(rough);
      if (!compact || compact.length > 16 || !labelHit(compact, names)) return;
      const tighter = [...el.children].some((child) => labelHit(normLabel(child.textContent || ""), names));
      if (tighter) return;
      let node = el;
      let rect = node.getBoundingClientRect();
      for (let i = 0; i < 4 && node.parentElement; i += 1) {
        const parent = node.parentElement;
        const parentLabel = normLabel(parent.textContent || "");
        const parentRect = parent.getBoundingClientRect();
        if (!parentLabel || parentLabel.length > 16) break;
        if (parentRect.width > 280 || parentRect.height > 88 || parentRect.width + 1 < rect.width) break;
        node = parent;
        rect = parentRect;
      }
      if (rect.width < 20 || rect.height < 8 || rect.width > 280 || rect.height > 88) return;
      found.push({
        el: node,
        rect,
        x: rect.left + rect.width / 2,
        left: rect.left,
        right: rect.right,
        top: rect.top,
      });
    });
    return found;
  }

  function numberNodes() {
    const found = [];
    eachElement((el) => {
      if (skipped(el)) return;
      const packed = String(el.textContent || "").replace(/\s/g, "");
      if (!packed || packed.length > 36 || !/^-?\d+(?:\.\d+)?/.test(packed)) return;
      const first = String(el.innerText || "").split("\n").map((line) => line.trim()).find(Boolean) || "";
      const raw = first.replace(/,/g, "").replace(/\s/g, "");
      if (!/^-?\d+(?:\.\d+)?$/.test(raw)) return;
      const rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8 || rect.width > 240 || rect.height > 72) return;
      found.push({ value: Number(raw), rect, el, hasBase: packed.includes("基础") });
    });
    return found;
  }

  function findColumn(names, numbers, rowTop) {
    const headers = headerCandidates(names);
    let best = null;
    let bestScore = -1;
    headers.forEach((header) => {
      const hits = numbers.filter((item) => item.rect.top >= header.rect.bottom - 2 && overlapsX(item.rect, header));
      const nearRow = Math.abs(header.rect.bottom - rowTop);
      const score = hits.length * 1000 - nearRow;
      if (score > bestScore) {
        best = header;
        bestScore = score;
      }
    });
    return best;
  }

  function moneyInBand(band, column, numbers) {
    if (!column) return null;
    const mid = (band.top + band.bottom) / 2;
    let best = null;
    let bestScore = Infinity;
    numbers.forEach((item) => {
      const cy = item.rect.top + item.rect.height / 2;
      if (cy < band.top + 2 || cy > band.bottom - 2) return;
      if (!overlapsX(item.rect, column)) return;
      const dx = Math.abs(item.rect.left + item.rect.width / 2 - column.x);
      const dy = Math.abs(cy - mid);
      const score = dx + dy * 0.08 + item.rect.height * 0.02 - (item.hasBase ? 12 : 0);
      if (score < bestScore) {
        best = item;
        bestScore = score;
      }
    });
    return best;
  }

  function rowBand(textNode) {
    let el = textNode.parentElement;
    let best = null;
    for (let depth = 0; depth < 18 && el && el !== document.body; depth += 1) {
      const ids = String(el.innerText || "").match(/ID[:：]\s*\d{8,}/g) || [];
      if (ids.length > 1) break;
      const rect = el.getBoundingClientRect();
      if (rect.height >= 48 && rect.height <= 480 && rect.width >= 160 && (!best || rect.height >= best.height)) {
        best = rect;
      }
      el = el.parentElement;
    }
    return best || textNode.parentElement.getBoundingClientRect();
  }

  function planTitle(text) {
    const lines = String(text || "").split("\n").map((item) => item.trim()).filter(Boolean);
    for (const line of lines) {
      if (/^ID[:：]/.test(line) || line.length > 90 || line === "首页") continue;
      if (/^\d{1,2}月\d{1,2}日/.test(line) || /保本/.test(line)) return line;
    }
    return "";
  }

  function boundedText(el, limit) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const parts = [];
    let node = walker.nextNode();
    let steps = 0;
    while (node && steps < limit) {
      steps += 1;
      const text = String(node.nodeValue || "").replace(/\s+/g, " ").trim();
      if (text) parts.push(text);
      node = walker.nextNode();
    }
    return parts.join("\n");
  }

  function readId(node) {
    const raw = String(node.nodeValue || "").replace(/\u00a0/g, " ").trim();
    if (!raw || raw.length > 180) return "";
    const direct = raw.match(/ID[:：]\s*(\d{8,})/);
    if (direct) return direct[1];
    const digits = raw.replace(/\s/g, "");
    if (!/^\d{8,}$/.test(digits)) return "";
    const parent = node.parentElement;
    if (!parent) return "";
    const prev = parent.previousElementSibling ? String(parent.previousElementSibling.textContent || "").slice(-16) : "";
    const own = String(parent.textContent || "").slice(0, 32);
    if (/ID[:：]\s*$/.test(prev.replace(/\s+/g, " ")) || /ID[:：]/.test(`${prev}${own}`)) return digits;
    return "";
  }

  function planBlock(textNode) {
    let el = textNode.parentElement;
    let best = null;
    for (let depth = 0; depth < 8 && el && el !== document.body; depth += 1) {
      if (el.childElementCount > 48) break;
      const text = boundedText(el, 40);
      if ((text.match(/ID[:：]\s*\d{8,}/g) || []).length > 1) break;
      const title = planTitle(text);
      const height = el.offsetHeight;
      const width = el.offsetWidth;
      if (title && height >= 40 && height <= 420 && width >= 160) {
        best = { el, text, title };
        if (width <= 860) break;
      }
      el = el.parentElement;
    }
    return best;
  }

  function measureHeader(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 16 || rect.height < 8 || rect.width > 280 || rect.height > 80) return null;
    if (rect.bottom < 0 || rect.top > window.innerHeight) return null;
    return { el, rect, x: rect.left + rect.width / 2, left: rect.left, right: rect.right, top: rect.top };
  }

  function numbersIn(row) {
    const found = [];
    const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    let steps = 0;
    while (node && steps < 80 && found.length < 20) {
      steps += 1;
      const parent = node.parentElement;
      const raw = String(node.nodeValue || "").replace(/[\s,]/g, "");
      if (parent && !skipped(parent) && /^-?\d+(?:\.\d+)?$/.test(raw)) {
        const rect = parent.getBoundingClientRect();
        if (rect.width >= 8 && rect.height >= 8 && rect.width <= 240 && rect.height <= 72) {
          found.push({ value: Number(raw), rect, el: parent, hasBase: false });
        }
      }
      node = walker.nextNode();
    }
    return found;
  }

  function infoCell(textNode) {
    let el = textNode.parentElement;
    for (let depth = 0; depth < 8 && el && el.parentElement; depth += 1) {
      const ids = String(el.innerText || "").match(/ID[:：]\s*\d{8,}/g) || [];
      if (ids.length > 1) break;
      const rect = el.getBoundingClientRect();
      if (rect.top < 96) return null;
      if (rect.height >= 48 && rect.height <= 220 && rect.width >= 160 && rect.width <= 520) return el;
      el = el.parentElement;
    }
    return null;
  }

  function rememberMetric(id, field, value) {
    const slot = metricCache[id] || (metricCache[id] = {});
    const hold = slot.hold || (slot.hold = {});
    if (value == null) return slot[field] ?? null;
    if (slot[field] == null || slot[field] === value) {
      slot[field] = value;
      hold[field] = null;
      return slot[field];
    }
    if (!hold[field] || hold[field].value !== value) hold[field] = { value, n: 1 };
    else hold[field].n += 1;
    if (hold[field].n >= 2) {
      slot[field] = value;
      hold[field] = null;
    }
    return slot[field];
  }

  function cellRow(el) {
    let node = el;
    for (let depth = 0; depth < 8 && node && node.parentElement; depth += 1) {
      const parent = node.parentElement;
      if (!parent || parent === document.body || skipped(parent)) return null;
      const kids = [...parent.children].filter((kid) => {
        if (kid.classList.contains("qc-profit-col") || kid.classList.contains("qc-profit-head")) return false;
        const rect = kid.getBoundingClientRect();
        return rect.width > 24 && rect.height > 16;
      });
      if (kids.length >= 4) {
        const top = kids[0].getBoundingClientRect().top;
        const sameLine = kids.every((kid) => Math.abs(kid.getBoundingClientRect().top - top) < 14);
        const display = getComputedStyle(parent).display;
        const horizontal = display === "flex" || display === "inline-flex" || display === "table-row" || parent.tagName === "TR";
        if (sameLine && horizontal && parent.getBoundingClientRect().width >= 640) return { row: parent, cell: node };
      }
      node = parent;
    }
    return null;
  }

  let pagePlans = [];
  let panelPos = null;

  function moneyOf(cell) {
    if (!cell) return null;
    const head = String(cell.textContent || "").split("基础")[0].replace(/,/g, "");
    const matched = head.match(/-?\d+(?:\.\d+)?/);
    return matched ? Number(matched[0]) : null;
  }

  function pageDocs() {
    const docs = [document];
    document.querySelectorAll("iframe").forEach((frame) => {
      try {
        if (frame.contentDocument && frame.contentDocument.body) docs.push(frame.contentDocument);
      } catch (_) {}
    });
    return docs;
  }

  function headerText(cell) {
    return String(cell.textContent || "").replace(/\s/g, "");
  }

  function directCells(el) {
    return [...el.children].filter((child) => {
      if (!child.tagName || child.tagName === "COL" || child.tagName === "COLGROUP") return false;
      if (child.classList && (child.classList.contains("qc-profit-badge") || child.classList.contains("qc-profit-col"))) return false;
      return true;
    });
  }

  function findHeader() {
    let found = null;
    pageDocs().forEach((doc) => {
      if (found) return;
      doc.querySelectorAll("table thead tr").forEach((tr) => {
        if (found) return;
        const heads = [...tr.querySelectorAll("th, td")];
        const idxCost = heads.findIndex((cell) => headerText(cell).includes("整体消耗"));
        const idxSettle = heads.findIndex((cell) => headerText(cell).includes("净成交金额"));
        if (idxCost >= 0 && idxSettle >= 0) found = { table: tr.closest("table"), heads, idxCost, idxSettle };
      });
    });
    return found;
  }

  function scopeOf(table) {
    let el = table.parentElement || table;
    let best = el;
    for (let depth = 0; depth < 4 && el.parentElement && el.parentElement !== document.body; depth += 1) {
      const parent = el.parentElement;
      if (parent.querySelectorAll("table").length > 8) break;
      best = parent;
      el = parent;
    }
    return best;
  }

  function rootsOf(scope) {
    const roots = [scope];
    const nodes = scope.querySelectorAll("*");
    const cap = Math.min(nodes.length, 1500);
    for (let i = 0; i < cap; i += 1) {
      if (nodes[i].shadowRoot) roots.push(nodes[i].shadowRoot);
    }
    return roots;
  }

  function columnAttr(cell) {
    for (const attr of cell.attributes) {
      if (!attr.name.startsWith("data-") || /^data-v-/.test(attr.name)) continue;
      if (!attr.value || attr.value.length > 80) continue;
      if (/key|col|field|prop|name/i.test(attr.name)) return attr;
    }
    return null;
  }

  function markedCells(roots, headerCell) {
    const found = [];
    const seen = new Set();
    const aria = headerCell.getAttribute("aria-colindex");
    const attr = columnAttr(headerCell);
    const take = (list) => {
      list.forEach((el) => {
        if (seen.has(el) || el === headerCell || el.closest("thead")) return;
        seen.add(el);
        found.push(el);
      });
    };
    roots.forEach((root) => {
      if (!root.querySelectorAll) return;
      if (aria) take([...root.querySelectorAll(`[aria-colindex="${CSS.escape(aria)}"]`)]);
      if (attr) take([...root.querySelectorAll(`[${CSS.escape(attr.name)}="${CSS.escape(attr.value)}"]`)]);
    });
    return found;
  }

  function rowBox(cell, scope) {
    let el = cell.parentElement;
    while (el && el !== scope && el !== document.body) {
      if (!el.closest("thead") && directCells(el).length >= 4 && !el.querySelector("thead")) return el;
      el = el.parentElement;
    }
    return cell.parentElement;
  }

  function titleOf(text) {
    const named = planTitle(text);
    if (named) return named;
    const flat = String(text || "").replace(/\s+/g, " ");
    const hit = flat.match(/\d{1,2}月\d{1,2}日.{0,48}?保本(?:（[^）]*）|\([^)]*\))?/);
    if (hit) return hit[0].trim();
    const day = flat.match(/\d{1,2}月\d{1,2}日\S{0,40}/);
    return day ? day[0].trim() : "";
  }

  function cellFor(kids, index, headCount) {
    if (kids.length === headCount) return kids[index] || null;
    const shift = headCount - kids.length;
    if (shift > 0 && index - shift >= 0 && index - shift < kids.length && moneyOf(kids[index - shift]) != null) return kids[index - shift];
    return kids[index] || null;
  }

  function buildPlan(row, costCell, settleCell, index) {
    const text = row ? row.textContent || "" : "";
    const idMatch = text.match(/ID[:：]\s*(\d{8,})/);
    const id = idMatch ? idMatch[1] : `row-${index + 1}`;
    const saved = recordOf(id);
    return {
      id,
      title: titleOf(text) || `计划 ${index + 1}`,
      text,
      cost: moneyOf(costCell),
      settle: moneyOf(settleCell),
      cell: costCell || row,
      saved,
      refund: parseRefund(saved.refund),
    };
  }

  function usable(list) {
    return list.filter((plan) => plan.cost != null || plan.settle != null);
  }

  function readTbody(header) {
    const need = Math.max(header.idxCost, header.idxSettle);
    const headCount = header.heads.length;
    const bodyRows = [];
    pageDocs().forEach((doc) => {
      doc.querySelectorAll("table tbody tr").forEach((tr) => {
        if (directCells(tr).length > need) bodyRows.push(tr);
      });
    });
    const aligned = bodyRows.filter((tr) => directCells(tr).length === headCount);
    const rows = aligned.length ? aligned : bodyRows;
    return rows.map((tr, index) => {
      const tds = directCells(tr);
      return buildPlan(tr, tds[header.idxCost], tds[header.idxSettle], index);
    });
  }

  function readMarked(roots, header, scope) {
    const settleByRow = new Map();
    markedCells(roots, header.heads[header.idxSettle]).forEach((cell) => {
      const row = rowBox(cell, scope);
      if (row && !settleByRow.has(row)) settleByRow.set(row, cell);
    });
    const plans = [];
    const seen = new Set();
    markedCells(roots, header.heads[header.idxCost]).forEach((costCell) => {
      const row = rowBox(costCell, scope);
      if (!row || seen.has(row)) return;
      const settleCell = settleByRow.get(row);
      if (!settleCell) return;
      seen.add(row);
      plans.push(buildPlan(row, costCell, settleCell, plans.length));
    });
    return plans;
  }

  function readDivRows(roots, header) {
    const need = Math.max(header.idxCost, header.idxSettle);
    const headCount = header.heads.length;
    const groups = new Map();
    roots.forEach((root) => {
      if (!root.querySelectorAll) return;
      const nodes = root.querySelectorAll("div, tr, li");
      const cap = Math.min(nodes.length, 2000);
      for (let i = 0; i < cap; i += 1) {
        const el = nodes[i];
        if (el.closest(`thead, #${PANEL_ID}`)) continue;
        if (el.querySelector("thead")) continue;
        const kids = directCells(el);
        if (kids.length <= need || kids.length > headCount + 4) continue;
        if (kids.some((kid) => {
          const count = directCells(kid).length;
          return count > need && count <= headCount + 4;
        })) continue;
        const parent = el.parentElement;
        if (!parent || parent.closest(`#${PANEL_ID}`)) continue;
        if (!groups.has(parent)) groups.set(parent, []);
        groups.get(parent).push({ el, kids });
      }
    });
    const ranked = [...groups.values()].sort((a, b) => {
      const hit = (list) => list.filter(({ kids }) => moneyOf(cellFor(kids, header.idxCost, headCount)) != null || moneyOf(cellFor(kids, header.idxSettle, headCount)) != null).length;
      return hit(b) - hit(a) || b.length - a.length;
    });
    return (ranked[0] || []).map(({ el, kids }, index) => buildPlan(el, cellFor(kids, header.idxCost, headCount), cellFor(kids, header.idxSettle, headCount), index));
  }

  function badgeHost(root) {
    const nodes = root.querySelectorAll("div, span, p");
    for (let i = 0; i < nodes.length; i += 1) {
      const text = (nodes[i].textContent || "").replace(/\s+/g, "");
      if (/^ID[:：]\d{8,}$/.test(text)) return nodes[i];
    }
    let best = null;
    nodes.forEach((node) => {
      const text = (node.textContent || "").replace(/\s+/g, " ").trim();
      if (text.length > 180 || text.length < 8) return;
      if (!/ID[:：]\s*\d{8,}/.test(text) || !/保本|\d{1,2}月\d{1,2}日/.test(text)) return;
      if (!best || text.length < best.text.length) best = { node, text };
    });
    return best ? best.node : root;
  }

  function attachNames(plans, scope) {
    const hits = [];
    const nodes = scope.querySelectorAll("div, tr");
    const cap = Math.min(nodes.length, 2000);
    for (let i = 0; i < cap && hits.length < plans.length; i += 1) {
      const el = nodes[i];
      if (el.closest(`thead, #${PANEL_ID}`)) continue;
      const text = el.textContent || "";
      if (text.length > 500 || text.length < 8) continue;
      if ((text.match(/ID[:：]\s*\d{8,}/g) || []).length !== 1) continue;
      if (hits.some((item) => item.contains(el))) continue;
      hits.push(el);
    }
    if (hits.length !== plans.length) return plans;
    return plans.map((plan, index) => {
      const text = `${hits[index].textContent || ""}\n${plan.text || ""}`;
      const idMatch = text.match(/ID[:：]\s*(\d{8,})/);
      const id = idMatch ? idMatch[1] : plan.id;
      const saved = recordOf(id);
      return { ...plan, id, title: titleOf(text) || plan.title, text, cell: badgeHost(hits[index]), saved, refund: parseRefund(saved.refund) };
    });
  }

  let lastReport = "";

  function readPlans() {
    const header = findHeader();
    if (!header) return [];
    const scope = scopeOf(header.table);
    const roots = rootsOf(scope);
    let source = "tbody";
    let plans = usable(readTbody(header));
    if (!plans.length) {
      source = "列标记";
      plans = usable(readMarked(roots, header, scope));
    }
    if (!plans.length) {
      source = "div行";
      plans = usable(readDivRows(roots, header));
    }
    if (plans.length) plans = attachNames(plans, scope);
    if (!plans.length) {
      reportRead(`表头已找到：整体消耗是第 ${header.idxCost} 列，净成交金额是第 ${header.idxSettle} 列，共 ${header.heads.length} 列。tbody 是空的，表头外层的 div 行也没有对上这两列。`, [], { header, scope, source });
      return [];
    }
    reportRead("", plans, { header, scope, source });
    return plans;
  }

  function reportRead(error, plans, meta) {
    const sig = `${error}|${plans.map((plan) => `${plan.id}:${plan.cost}:${plan.settle}`).join(",")}`;
    if (sig === lastReport) return;
    lastReport = sig;
    console.group(`[千川方舟] ${error ? "读取失败" : "读取成功"}`);
    console.log("location.pathname =", location.pathname);
    if (error) console.log(error);
    else console.log("条数", plans.length, "读取方式", meta && meta.source);
    if (meta && meta.header) {
      const costCell = meta.header.heads[meta.header.idxCost];
      const settleCell = meta.header.heads[meta.header.idxSettle];
      console.log("表头外层 class =", String((meta.scope && meta.scope.className) || ""));
      console.log("整体消耗表头属性 =", [...costCell.attributes].map((attr) => `${attr.name}=${attr.value}`));
      console.log("净成交金额表头属性 =", [...settleCell.attributes].map((attr) => `${attr.name}=${attr.value}`));
    }
    pageDocs().forEach((doc, docIndex) => {
      const tables = [...doc.querySelectorAll("table")];
      console.log("文档", docIndex, "是 iframe =", doc !== document, "table 数量 =", tables.length);
      tables.forEach((table, tableIndex) => {
        const heads = [...table.querySelectorAll("thead tr")].map((tr) => [...tr.querySelectorAll("th, td")].map((cell) => headerText(cell)));
        console.log("table", tableIndex, "thead =", heads, "tbody 行数 =", table.querySelectorAll("tbody tr").length);
      });
    });
    if (error && meta && meta.scope) {
      const classes = new Map();
      const nodes = meta.scope.querySelectorAll("div");
      const cap = Math.min(nodes.length, 600);
      for (let i = 0; i < cap; i += 1) {
        const cls = String(nodes[i].className || "").trim().split(/\s+/).slice(0, 2).join(".");
        if (!cls) continue;
        classes.set(cls, (classes.get(cls) || 0) + 1);
      }
      console.log("外层里出现最多的 div class", [...classes.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15));
    }
    if (plans.length) console.table(plans.map((plan) => ({ id: plan.id, title: plan.title, cost: plan.cost, settle: plan.settle, 方式: meta && meta.source })));
    console.groupEnd();
  }

  function placeBadge(idNode, badge) {
    let column = idNode;
    let node = idNode;
    for (let depth = 0; depth < 8 && node.parentElement && node.parentElement !== document.body; depth += 1) {
      const parent = node.parentElement;
      if (parent.closest(`#${PANEL_ID}, thead`)) break;
      const text = parent.textContent || "";
      if (text.length > 500) break;
      if ((text.match(/ID[:：]\s*\d{8,}/g) || []).length > 1) break;
      if (parent.querySelector("img")) break;
      if (/保本|\d{1,2}月\d{1,2}日/.test(text)) column = parent;
      node = parent;
    }
    if (badge.parentElement !== column) column.appendChild(badge);
    const display = getComputedStyle(column).display;
    const direction = getComputedStyle(column).flexDirection;
    if (display.includes("flex") && direction.indexOf("column") !== 0) column.style.flexWrap = "wrap";
  }

  function yuan(value) {
    if (value == null) return "未读到";
    return `¥${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function sourceHtml(plan) {
    const costCls = plan.cost == null ? "miss" : "";
    const settleCls = plan.settle == null ? "miss" : "";
    return `<div class="qc-source"><div><span>整体消耗</span><b class="${costCls}">${yuan(plan.cost)}</b></div><div><span>净成交金额</span><b class="${settleCls}">${yuan(plan.settle)}</b></div></div>`;
  }

  function viewOf(plan) {
    const filled = filledLine(plan.saved);
    const extra = `${sourceHtml(plan)}${filled ? `<span class="qc-filled">${filled}</span>` : ""}`;
    const refund = plan.refund;
    const commission = commissionOf(plan.saved, plan.text);
    const miss = (sub) => ({ money: "—", cls: "miss", sub, extra, profit: null, calculated: false, counted: false, pending: true });
    if (refund == null) return miss("实际退款率不正确");
    if (commission == null) return miss("缺少佣金");
    if (plan.settle == null || plan.cost == null) {
      return miss(plan.cost == null ? "还没读到整体消耗" : "还没读到净成交金额");
    }
    const profit = plan.settle * (1 - refund) * commission - plan.cost;
    const cents = Math.round(profit * 100);
    const shown = cents / 100;
    const rate = plan.settle ? `${cents > 0 ? "+" : ""}${((shown / plan.settle) * 100).toFixed(1)}%` : "保本";
    if (cents > 0) return { money: moneyText(shown), cls: "pos", sub: rate, extra, profit: shown, calculated: true, counted: true, pending: false };
    if (cents < 0) return { money: moneyText(shown), cls: "neg", sub: rate, extra, profit: shown, calculated: true, counted: true, pending: false };
    return { money: "¥0.00", cls: "zero", sub: "保本", extra, profit: 0, calculated: true, counted: true, pending: false };
  }

  function profitHtml(view) {
    return `<b class="${view.cls}">${view.money}</b><span>${view.sub}</span>${view.extra || ""}`;
  }

  function mountColumn(plan, view) {
    if (!plan.costEl || !plan.costHost) return false;
    const body = cellRow(plan.costEl);
    const head = cellRow(plan.costHost.el);
    if (!body || !head || body.row === head.row) return false;
    let headCell = head.row.querySelector(":scope > .qc-profit-head");
    if (!headCell) {
      headCell = document.createElement(head.row.tagName === "TR" ? "th" : "div");
      headCell.className = "qc-profit-head";
      headCell.textContent = "预估利润";
      head.row.insertBefore(headCell, head.cell);
    }
    let cell = body.row.querySelector(`:scope > .qc-profit-col[data-plan="${plan.id}"]`);
    if (!cell) {
      cell = document.createElement(body.row.tagName === "TR" ? "td" : "div");
      cell.className = "qc-profit-col";
      cell.dataset.plan = plan.id;
      body.row.insertBefore(cell, body.cell);
    }
    const html = profitHtml(view);
    if (cell.innerHTML !== html) cell.innerHTML = html;
    plan.mounted = cell;
    return true;
  }

  function paintBadge(plan, view) {
    const host = plan.cell;
    if (!host || host.closest("thead")) return;
    let badge = document.querySelector(`.qc-profit-badge[data-plan="${plan.id}"]`);
    if (!badge) {
      badge = document.createElement("div");
      badge.className = "qc-profit-badge";
      badge.dataset.plan = plan.id;
    }
    placeBadge(host, badge);
    const html = `<b class="${view.cls}">${view.profit == null ? view.sub : `利润${String(view.money).replace(/^\+/, "")}`}</b>`;
    if (badge.innerHTML !== html) badge.innerHTML = html;
    plan.mounted = badge;
  }

  function paintPanel(plans) {
    let panel = document.getElementById(PANEL_ID);
    if (!panelOpen) {
      if (panel) panel.remove();
      return;
    }
    if (!panel) {
      panel = document.createElement("aside");
      panel.id = PANEL_ID;
      panel.innerHTML = `<header><strong>千川方舟</strong><span><button type="button" id="qc-fangzhou-refresh">刷新</button><button type="button" id="qc-fangzhou-close">关闭</button></span></header><div class="qc-formula">预估利润 = 净成交金额 × (1 − 实际退款率) × 佣金率 − 整体消耗<br>整体消耗、净成交金额从表格读取。退款率需要填写，不填按 0。佣金从计划名称里的「佣金:xx%」提取，没有就在下方填写，填了以填写的为准。</div><div id="qc-fangzhou-total"></div><div id="qc-fangzhou-list"></div>`;
      document.documentElement.appendChild(panel);
      placePanel(panel);
      enableDrag(panel);
      panel.querySelector("#qc-fangzhou-close").addEventListener("click", () => {
        panelOpen = false;
        panel.remove();
      });
      panel.querySelector("#qc-fangzhou-refresh").addEventListener("click", () => {
        lastReport = "";
        pagePlans = readPlans();
        paint();
      });
      panel.addEventListener("input", (event) => {
        const input = event.target.closest("[data-field]");
        if (!input) return;
        saveField(input.dataset.plan, input.dataset.field, input.value);
      });
      panel.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        const input = event.target.closest("[data-field]");
        if (!input) return;
        event.preventDefault();
        event.stopPropagation();
        saveField(input.dataset.plan, input.dataset.field, input.value, () => {
          input.blur();
          lastReport = "";
          pagePlans = readPlans();
          paint();
        });
      });
    }
    const views = plans.map((plan) => viewOf(plan));
    let total = 0;
    let counted = 0;
    let win = 0;
    let loss = 0;
    let even = 0;
    let pending = 0;
    views.forEach((view) => {
      if (!view.counted || view.profit == null) {
        pending += 1;
        return;
      }
      counted += 1;
      total += view.profit;
      const cents = Math.round(view.profit * 100);
      if (cents > 0) win += 1;
      else if (cents < 0) loss += 1;
      else even += 1;
    });
    const totalNode = panel.querySelector("#qc-fangzhou-total");
    const sumText = counted ? moneyText(total) : "—";
    const sumCls = !counted ? "zero" : total > 0 ? "pos" : total < 0 ? "neg" : "zero";
    const pendingHtml = pending ? `<div><span>未算</span><b>${pending}</b></div>` : "";
    const summary = `<div class="qc-sum-main"><span>总预估利润</span><strong class="${sumCls}">${sumText}</strong></div><div class="qc-sum-split"><div><span class="pos">盈利</span><b class="pos">${win}</b></div><div><span>保本</span><b>${even}</b></div><div><span class="neg">亏损</span><b class="neg">${loss}</b></div>${pendingHtml}</div>`;
    if (totalNode.innerHTML !== summary) totalNode.innerHTML = summary;
    const list = panel.querySelector("#qc-fangzhou-list");
    if (document.activeElement && list.contains(document.activeElement)) {
      const lines = list.querySelectorAll(".qc-profit-line");
      plans.forEach((plan, index) => {
        const line = lines[index];
        if (!line) return;
        const view = views[index];
        const filled = filledLine(plan.saved);
        const html = view.profit == null
          ? `<strong class="miss">${view.money}</strong><em>${view.sub}</em>${filled ? `<em class="qc-filled">${filled}</em>` : ""}`
          : `<strong class="${view.cls}">${view.money}</strong>${filled ? `<em class="qc-filled">${filled}</em>` : ""}`;
        if (line.innerHTML !== html) line.innerHTML = html;
      });
      return;
    }
    if (!plans.length) {
      const empty = `<div class="qc-plan-card">表头已能识别，数据行不在 tbody 里。点刷新后再看控制台里的「千川方舟」。</div>`;
      if (list.innerHTML !== empty) list.innerHTML = empty;
      return;
    }
    const html = plans.map((plan, index) => {
      const view = views[index];
      const refund = String(plan.saved.refund || "").replace(/"/g, "&quot;");
      const commission = String(plan.saved.commission || "").replace(/"/g, "&quot;");
      const filled = filledLine(plan.saved);
      return `<div class="qc-plan-card"><b>${plan.title}</b><div class="qc-sub">ID ${plan.id}</div>${sourceHtml(plan)}<div class="qc-profit-line">${view.profit == null ? `<strong class="miss">${view.money}</strong><em>${view.sub}</em>` : `<strong class="${view.cls}">${view.money}</strong>`}${filled ? `<em class="qc-filled">${filled}</em>` : ""}</div><div class="qc-roi-line"><span>实际退款率%</span><input data-plan="${plan.id}" data-field="refund" value="${refund}" inputmode="decimal" placeholder="未填按0"></div><div class="qc-roi-line"><span>佣金%</span><input data-plan="${plan.id}" data-field="commission" value="${commission}" inputmode="decimal" placeholder="可从名称取"></div></div>`;
    }).join("");
    if (list.innerHTML !== html) list.innerHTML = html;
  }

  function cleanupOldNodes() {
    document.querySelectorAll("#qc-profit-summary, #qc-roi-mode-bar, .qc-profit-cell").forEach((node) => node.remove());
  }

  function paint() {
    if (!onUniPage()) {
      clearInjected();
      return;
    }
    if (painting) return;
    painting = true;
    try {
      cleanupOldNodes();
      document.querySelectorAll(".qc-profit-col, .qc-profit-head").forEach((node) => node.remove());
      const plans = pagePlans;
      const used = new Set();
      plans.forEach((plan) => {
        paintBadge(plan, viewOf(plan));
        if (plan.mounted) used.add(plan.mounted);
      });
      document.querySelectorAll(".qc-profit-badge").forEach((node) => {
        if (!used.has(node)) node.remove();
      });
      paintPanel(plans);
    } finally {
      painting = false;
    }
  }

  function watchTable() {
    if (!onUniPage() || !document.body) return;
    if (watchTable.timer) window.clearInterval(watchTable.timer);
    const read = () => {
      if (!onUniPage() || document.hidden) return;
      pagePlans = readPlans();
      paint();
    };
    read();
    let tries = 0;
    watchTable.timer = window.setInterval(() => {
      tries += 1;
      if (!onUniPage() || tries > 20 || pagePlans.length) {
        window.clearInterval(watchTable.timer);
        return;
      }
      read();
    }, 3000);
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== "toggle-panel") return;
    panelOpen = !document.getElementById(PANEL_ID);
    if (!panelOpen) {
      const panel = document.getElementById(PANEL_ID);
      if (panel) panel.remove();
    } else {
      paint();
    }
    sendResponse({ ok: true });
  });

  let seenPath = location.pathname;
  function onRoute() {
    if (location.pathname === seenPath) return;
    seenPath = location.pathname;
    if (!onUniPage()) {
      pagePlans = [];
      clearInjected();
      return;
    }
    watchTable();
  }
  window.addEventListener("popstate", onRoute);
  const pushState = history.pushState;
  const replaceState = history.replaceState;
  history.pushState = function () {
    pushState.apply(this, arguments);
    onRoute();
  };
  history.replaceState = function () {
    replaceState.apply(this, arguments);
    onRoute();
  };
  loadRoi();
})();
