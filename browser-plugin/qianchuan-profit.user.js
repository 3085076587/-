// ==UserScript==
// @name         千川方舟 推商品预估利润
// @namespace    qianchuan-fangzhou
// @version      2.0.9
// @description  读取千川 /uni-prom 推商品虚拟表格的可视行，计算预估利润
// @match        https://qianchuan.jinritemai.com/*
// @run-at       document-idle
// @grant        GM_getValue
// @grant        GM_setValue
// ==/UserScript==

(function () {
  "use strict";

  const LOG = "[千川方舟]";
  const ROI_KEY = "qc_breakeven_roi_v1";
  const SCROLL_KEY = "qc_scroll_load_enabled";
  const POS_KEY = "qc_panel_pos_v1";
  const PANEL_ID = "qc-fangzhou-panel";
  let roiMap = {};
  let pagePlans = [];
  let panelPos = null;
  let panelOpen = true;
  let scrolling = false;
  let holdAll = false;
  let readTimer = 0;
  let lastSig = "";

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
        GM_setValue(POS_KEY, JSON.stringify(panelPos));
      };
      header.setPointerCapture(event.pointerId);
      header.addEventListener("pointermove", move);
      header.addEventListener("pointerup", up);
    });
  }

  function log() {
    console.log(LOG, ...arguments);
  }

  function warn() {
    console.warn(LOG, ...arguments);
  }

  function onUniPage() {
    const path = location.pathname || "";
    const hit = path === "/uni-prom" || path.indexOf("/uni-prom/") === 0 || path.indexOf("/uni-prom") !== -1;
    log("location.pathname =", path, "href =", location.href, "是否匹配推商品页 =", hit);
    return hit;
  }

  function loadMap() {
    try {
      const raw = GM_getValue(ROI_KEY, "{}");
      const data = typeof raw === "string" ? JSON.parse(raw || "{}") : raw || {};
      return data && typeof data === "object" ? data : {};
    } catch (error) {
      warn("读取本地佣金/退款率失败", error);
      return {};
    }
  }

  function saveMap() {
    GM_setValue(ROI_KEY, JSON.stringify(roiMap));
  }

  function scrollEnabled() {
    return GM_getValue(SCROLL_KEY, false) === true;
  }

  function setScrollEnabled(value) {
    GM_setValue(SCROLL_KEY, !!value);
    log("滚动加载全部开关 =", !!value);
  }

  function recordOf(planId) {
    const saved = roiMap[planId];
    if (saved && typeof saved === "object") return { commission: saved.commission || "", refund: saved.refund || "" };
    return { commission: "", refund: "" };
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

  function yuan(value) {
    if (value == null) return "未读到";
    return `¥${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function headerText(cell) {
    return String(cell && cell.textContent || "").replace(/[\s\u200b\u200c\u200d\ufeff]/g, "");
  }

  function moneyOf(cell) {
    if (!cell) return null;
    const head = String(cell.textContent || "").split("基础")[0].replace(/,/g, "");
    const matched = head.match(/-?\d+(?:\.\d+)?/);
    return matched ? Number(matched[0]) : null;
  }

  function planTitle(text) {
    const lines = String(text || "").split("\n").map((item) => item.trim()).filter(Boolean);
    for (const line of lines) {
      if (/^ID[:：]/.test(line) || line.length > 90 || line === "首页") continue;
      if (/^\d{1,2}月\d{1,2}日/.test(line) || /保本/.test(line)) return line;
    }
    return "";
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

  function directCells(el) {
    return [...el.children].filter((child) => child.tagName && child.tagName !== "COL" && child.tagName !== "COLGROUP" && !(child.classList && child.classList.contains("qc-profit-badge")));
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

  function cellFor(kids, index, headCount) {
    if (kids.length === headCount) return kids[index] || null;
    const shift = headCount - kids.length;
    if (shift > 0 && index - shift >= 0 && index - shift < kids.length && moneyOf(kids[index - shift]) != null) return kids[index - shift];
    return kids[index] || null;
  }

  function readDivPlans(table, idxCost, idxSettle, headCount) {
    const scope = scopeOf(table);
    const need = Math.max(idxCost, idxSettle);
    const groups = new Map();
    const nodes = scope.querySelectorAll("div, tr, li");
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
      if (!parent) continue;
      if (!groups.has(parent)) groups.set(parent, []);
      groups.get(parent).push({ el, kids });
    }
    const ranked = [...groups.values()].sort((a, b) => {
      const hit = (list) => list.filter(({ kids }) => moneyOf(cellFor(kids, idxCost, headCount)) != null || moneyOf(cellFor(kids, idxSettle, headCount)) != null).length;
      return hit(b) - hit(a) || b.length - a.length;
    });
    log("div 行候选组数 =", ranked.length, "最大组行数 =", ranked[0] ? ranked[0].length : 0, "外层 class =", String(scope.className || ""));
    return (ranked[0] || []).map(({ el, kids }, index) => {
      const costCell = cellFor(kids, idxCost, headCount);
      const settleCell = cellFor(kids, idxSettle, headCount);
      const text = el.textContent || "";
      const idMatch = text.match(/ID[:：]\s*(\d{8,})/);
      const id = idMatch ? idMatch[1] : `row-${index + 1}`;
      const saved = recordOf(id);
      return {
        id,
        title: planTitle(text) || `计划 ${index + 1}`,
        text,
        cost: moneyOf(costCell),
        settle: moneyOf(settleCell),
        cell: costCell || el,
        saved,
        refund: parseRefund(saved.refund),
      };
    }).filter((plan) => plan.cost != null || plan.settle != null);
  }

  function readVisible() {
    const headerRows = [];
    pageDocs().forEach((doc) => {
      doc.querySelectorAll("table thead tr").forEach((tr) => {
        const cells = [...tr.querySelectorAll("th, td")];
        if (!cells.length) return;
        headerRows.push(cells.map((cell, index) => ({ index, text: headerText(cell) })));
      });
    });
    log("识别到的表头行数 =", headerRows.length);
    headerRows.forEach((row, rowIndex) => {
      log("表头第", rowIndex, "行", row.map((item) => `${item.index}:${item.text}`).join(" | "));
    });
    let idxCost = -1;
    let idxSettle = -1;
    let headCount = 0;
    headerRows.some((row) => {
      const cost = row.find((item) => item.text.includes("整体消耗"));
      const settle = row.find((item) => item.text.includes("净成交金额"));
      if (!cost || !settle) return false;
      idxCost = cost.index;
      idxSettle = settle.index;
      headCount = row.length;
      return true;
    });
    log("表头下标 整体消耗 =", idxCost, "净成交金额 =", idxSettle, "表头列数 =", headCount);
    if (idxCost < 0 || idxSettle < 0) {
      const error = headerRows.length
        ? "找不到表头：有 thead，但没有任何一行同时包含「整体消耗」和「净成交金额」"
        : "找不到表头：页面上没有 table thead tr";
      reportFailure(error);
      return { plans: [], error };
    }
    const groups = [];
    pageDocs().forEach((doc) => {
      doc.querySelectorAll("table").forEach((table) => {
        const rows = [...table.querySelectorAll("tbody tr")].filter((tr) => tr.querySelectorAll("td").length);
        if (rows.length) groups.push(rows);
      });
    });
    log("tbody 分组数 =", groups.length, "各组行数 =", groups.map((rows) => rows.length).join(","));
    const dataRows = groups
      .map((rows) => rows.filter((tr) => tr.querySelectorAll("td").length > Math.max(idxCost, idxSettle)))
      .find((rows) => rows.length) || [];
    if (!dataRows.length) {
      const headerTable = pageDocs().map((doc) => [...doc.querySelectorAll("table thead tr")]).flat().map((tr) => tr.closest("table")).find(Boolean);
      const virtual = headerTable ? readDivPlans(headerTable, idxCost, idxSettle, headCount) : [];
      log("tbody 为空，改读 div 行，得到", virtual.length);
      if (virtual.length) {
        console.group(LOG + " 读取成功");
        console.log("读取方式 = div行");
        console.table(virtual.map((plan) => ({ id: plan.id, title: plan.title, cost: plan.cost, settle: plan.settle })));
        console.groupEnd();
        return { plans: virtual, error: "" };
      }
      const error = groups.length
        ? "找不到tbody行：表头已找到，但没有任何一行的 td 数量够到消耗/净成交这两列"
        : `表头已找到：整体消耗是第 ${idxCost} 列，净成交金额是第 ${idxSettle} 列，共 ${headCount} 列。tbody 是空的，表头外层的 div 行也没有对上这两列。`;
      reportFailure(error);
      return { plans: [], error };
    }
    const aligned = dataRows.filter((tr) => tr.querySelectorAll("td").length === headCount);
    const rows = aligned.length ? aligned : dataRows;
    log("本次可视 DOM 行数 =", rows.length, "按表头列数对齐的行数 =", aligned.length);
    const nameRows = groups.find((list) => {
      if (list === rows || list.length !== rows.length) return false;
      return list.some((tr) => /ID[:：]\s*\d{8,}|保本|\d{1,2}月\d{1,2}日/.test(tr.innerText || ""));
    }) || [];
    const plans = rows.map((tr, index) => {
      const tds = [...tr.querySelectorAll("td")];
      const nameRow = nameRows[index];
      const text = `${nameRow ? nameRow.innerText : ""}\n${tr.innerText || ""}`;
      const idMatch = text.match(/ID[:：]\s*(\d{8,})/);
      const id = idMatch ? idMatch[1] : `row-${index + 1}`;
      const saved = recordOf(id);
      const plan = {
        id,
        title: planTitle(text) || `计划 ${index + 1}`,
        text,
        cost: moneyOf(tds[idxCost]),
        settle: moneyOf(tds[idxSettle]),
        cell: (nameRow || tr).querySelector("td") || tr,
        saved,
        refund: parseRefund(saved.refund),
      };
      log("计划原始数据", { id: plan.id, title: plan.title, cost: plan.cost, settle: plan.settle });
      return plan;
    }).filter((plan) => plan.cost != null || plan.settle != null);
    log("有效计划条数 =", plans.length);
    if (!plans.length) {
      const error = "行已经找到，但整体消耗和净成交金额都没有解析出数字";
      reportFailure(error);
      return { plans: [], error };
    }
    console.group(LOG + " 读取成功");
    console.table(plans.map((plan) => ({
      id: plan.id,
      title: plan.title,
      cost: plan.cost,
      settle: plan.settle,
    })));
    console.groupEnd();
    return { plans, error: "" };
  }

  function reportFailure(error) {
    console.group(LOG + " 读取失败");
    console.error(error);
    pageDocs().forEach((doc, docIndex) => {
      const tables = [...doc.querySelectorAll("table")];
      console.log("文档", docIndex, "iframe =", doc !== document, "table 数量 =", tables.length);
      tables.forEach((table, tableIndex) => {
        const heads = [...table.querySelectorAll("thead tr")].map((tr) => [...tr.querySelectorAll("th, td")].map((cell) => headerText(cell)));
        const bodyCount = table.querySelectorAll("tbody tr").length;
        const firstCells = table.querySelector("tbody tr") ? table.querySelector("tbody tr").querySelectorAll("td").length : 0;
        console.log("table", tableIndex, "thead 行 =", heads, "tbody 行数 =", bodyCount, "第一行 td 数 =", firstCells);
      });
      const hits = [];
      doc.querySelectorAll("th, td, div, span").forEach((el) => {
        const text = headerText(el);
        if (!text || text.length > 40) return;
        if (text.includes("整体消耗") || text.includes("净成交金额")) {
          hits.push(`${el.tagName}.${el.className || "-"} => ${text}`);
        }
      });
      console.log("短文本里带关键词的节点", hits.slice(0, 30));
    });
    console.groupEnd();
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

  function helpHtml() {
    return `<button type="button" class="qc-help" aria-label="计算公式">?<span class="qc-help-tip">预估利润 = 净成交金额 × (1 − 实际退款率) × 佣金率 − 整体消耗。整体消耗、净成交金额从当前表格读取。实际退款率需要填写，不填按 0。佣金：计划名称里有「佣金:76.32%」就自动提取，没有就在插件里填写；填了以填写的为准。</span></button>`;
  }

  function paintBadge(plan, view) {
    const host = plan.cell;
    if (!host || !host.isConnected) return;
    let badge = host.nextElementSibling && host.nextElementSibling.classList.contains("qc-profit-badge") ? host.nextElementSibling : null;
    if (!badge) {
      badge = document.createElement("div");
      badge.className = "qc-profit-badge";
      host.insertAdjacentElement("afterend", badge);
    }
    badge.dataset.plan = plan.id;
    const html = `<b class="${view.cls}">${view.profit == null ? view.sub : `利润${String(view.money).replace(/^\+/, "")}`}</b>${helpHtml()}`;
    if (badge.innerHTML !== html) badge.innerHTML = html;
    plan.mounted = badge;
  }

  function paint() {
    if (!onUniPage()) {
      const old = document.getElementById(PANEL_ID);
      if (old) old.remove();
      return;
    }
    let panel = document.getElementById(PANEL_ID);
    if (!panelOpen) {
      if (panel) panel.remove();
      return;
    }
    if (!panel) {
      panel = document.createElement("aside");
      panel.id = PANEL_ID;
      panel.innerHTML = `<header><strong>千川方舟</strong><span class="qc-actions"><button type="button" id="qc-fangzhou-refresh">刷新读取</button><label class="qc-switch"><input type="checkbox" id="qc-fangzhou-scroll">滚动加载</label><button type="button" id="qc-fangzhou-all">加载全部</button><button type="button" id="qc-fangzhou-close">关闭</button></span></header><div id="qc-fangzhou-total"></div><div id="qc-fangzhou-list"></div>`;
      document.documentElement.appendChild(panel);
      placePanel(panel);
      enableDrag(panel);
      panel.querySelector("#qc-fangzhou-scroll").checked = scrollEnabled();
      panel.querySelector("#qc-fangzhou-scroll").addEventListener("change", (event) => {
        setScrollEnabled(event.target.checked);
      });
      panel.querySelector("#qc-fangzhou-close").addEventListener("click", () => {
        panelOpen = false;
        panel.remove();
      });
      panel.querySelector("#qc-fangzhou-refresh").addEventListener("click", () => {
        holdAll = false;
        readNow("manual");
      });
      panel.querySelector("#qc-fangzhou-all").addEventListener("click", () => {
        loadAll();
      });
      const onField = (event) => {
        const input = event.target.closest("[data-field]");
        if (!input) return;
        const current = recordOf(input.dataset.plan);
        current[input.dataset.field] = String(input.value ?? "").trim();
        if (!current.commission && !current.refund) delete roiMap[input.dataset.plan];
        else roiMap[input.dataset.plan] = current;
        saveMap();
        pagePlans.forEach((plan) => {
          if (plan.id !== input.dataset.plan) return;
          plan.saved = recordOf(plan.id);
          plan.refund = parseRefund(plan.saved.refund);
        });
        paint();
      };
      panel.addEventListener("input", onField);
      panel.addEventListener("change", onField);
      panel.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        const input = event.target.closest("[data-field]");
        if (!input) return;
        event.preventDefault();
        event.stopPropagation();
        onField(event);
        input.blur();
        readNow("manual");
      });
    }
    const views = pagePlans.map((plan) => viewOf(plan));
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
    totalNode.innerHTML = `<div class="qc-sum-main"><span>总预估利润</span><strong class="${sumCls}">${sumText}</strong></div><div class="qc-sum-split"><div><span class="pos">盈利</span><b class="pos">${win}</b></div><div><span>保本</span><b>${even}</b></div><div><span class="neg">亏损</span><b class="neg">${loss}</b></div>${pendingHtml}</div>`;
    const list = panel.querySelector("#qc-fangzhou-list");
    if (document.activeElement && list.contains(document.activeElement)) {
      const lines = list.querySelectorAll(".qc-profit-line");
      pagePlans.forEach((plan, index) => {
        const line = lines[index];
        if (!line) return;
        const view = views[index];
        const filled = filledLine(plan.saved);
        const html = view.profit == null
          ? `<strong class="miss">${view.money}</strong><em>${view.sub}</em>${helpHtml()}${filled ? `<em class="qc-filled">${filled}</em>` : ""}`
          : `<strong class="${view.cls}">${view.money}</strong>${helpHtml()}${filled ? `<em class="qc-filled">${filled}</em>` : ""}`;
        if (line.innerHTML !== html) line.innerHTML = html;
      });
      paintVisibleBadges(views);
      return;
    }
    if (!pagePlans.length) {
      list.innerHTML = `<div class="qc-plan-card">${pagePlans.readError || "当前可视区还没有读到计划。"}<br>打开浏览器控制台，筛选「千川方舟」可以看到表头、table 数量和失败原因。</div>`;
    } else {
      list.innerHTML = pagePlans.map((plan, index) => {
        const view = views[index];
        const refund = String(plan.saved.refund || "").replace(/"/g, "&quot;");
        const commission = String(plan.saved.commission || "").replace(/"/g, "&quot;");
        const filled = filledLine(plan.saved);
        return `<div class="qc-plan-card"><b>${plan.title}</b><div class="qc-sub">ID ${plan.id}</div>${sourceHtml(plan)}<div class="qc-profit-line">${view.profit == null ? `<strong class="miss">${view.money}</strong><em>${view.sub}</em>` : `<strong class="${view.cls}">${view.money}</strong>`}${helpHtml()}${filled ? `<em class="qc-filled">${filled}</em>` : ""}</div><div class="qc-roi-line"><span>实际退款率%</span><input data-plan="${plan.id}" data-field="refund" value="${refund}" inputmode="decimal" placeholder="未填按0"></div><div class="qc-roi-line"><span>佣金%</span><input data-plan="${plan.id}" data-field="commission" value="${commission}" inputmode="decimal" placeholder="可从名称取"></div></div>`;
      }).join("");
    }
    paintVisibleBadges(views);
  }

  function paintVisibleBadges(views) {
    const used = new Set();
    pagePlans.forEach((plan, index) => {
      if (!plan.cell || !plan.cell.isConnected) return;
      paintBadge(plan, views[index]);
      if (plan.mounted) used.add(plan.mounted);
    });
    document.querySelectorAll(".qc-profit-badge").forEach((node) => {
      if (!used.has(node)) node.remove();
    });
  }

  function readNow(reason) {
    if (!onUniPage()) {
      warn("本次不读取：当前路径不是推商品页", location.pathname);
      return;
    }
    if (scrolling) {
      log("本次跳过：正在滚动加载", reason);
      return;
    }
    if (holdAll && reason !== "manual" && reason !== "load-all") return;
    let result;
    try {
      result = readVisible();
    } catch (error) {
      console.group(LOG + " 读取抛错");
      console.error(error);
      console.groupEnd();
      result = { plans: [], error: `脚本抛错：${error && error.message ? error.message : error}` };
    }
    const sig = `${result.error}|${result.plans.map((plan) => `${plan.id}:${plan.cost}:${plan.settle}`).join("|")}`;
    if (reason !== "manual" && reason !== "boot" && reason !== "load-all" && sig === lastSig) return;
    lastSig = sig;
    if (result.error) console.error(LOG, "结论：失败。", result.error, "触发 =", reason);
    else console.log(LOG, "结论：成功。", result.plans.length, "条。触发 =", reason);
    pagePlans = result.plans;
    pagePlans.readError = result.error;
    paint();
  }

  function scheduleRead(reason) {
    window.clearTimeout(readTimer);
    readTimer = window.setTimeout(() => readNow(reason), 800);
  }

  function findScroller() {
    const row = document.querySelector("table tbody tr");
    let el = row ? row.parentElement : null;
    while (el && el !== document.body) {
      if (el.scrollHeight > el.clientHeight + 40) return el;
      el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  async function loadAll() {
    if (!scrollEnabled()) {
      warn("加载全部未执行：滚动加载开关是关闭的");
      return;
    }
    if (scrolling) return;
    scrolling = true;
    holdAll = false;
    const button = document.getElementById("qc-fangzhou-all");
    if (button) button.textContent = "加载中";
    const collected = new Map();
    const scroller = findScroller();
    const start = scroller.scrollTop;
    let idle = 0;
    log("开始缓慢滚动加载，默认关闭，本次由手动按钮触发");
    try {
      for (let step = 0; step < 80 && idle < 4; step += 1) {
        const batch = readVisible().plans;
        let added = 0;
        batch.forEach((plan) => {
          if (!collected.has(plan.id)) added += 1;
          collected.set(plan.id, plan);
        });
        log("滚动步", step, "新增", added, "累计", collected.size);
        if (!added) idle += 1;
        else idle = 0;
        const next = scroller.scrollTop + Math.max(160, Math.floor(scroller.clientHeight * 0.55));
        scroller.scrollTop = next;
        if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 8) idle += 1;
        await sleep(800);
      }
      scroller.scrollTop = start;
      pagePlans = [...collected.values()];
      holdAll = true;
      lastSig = "";
      log("滚动加载结束，累计计划", pagePlans.length);
      paint();
    } finally {
      scrolling = false;
      if (button) button.textContent = "加载全部";
    }
  }

  function installStyle() {
    if (document.getElementById("qc-fangzhou-style")) return;
    const style = document.createElement("style");
    style.id = "qc-fangzhou-style";
    style.textContent = `
      #qc-fangzhou-panel{position:fixed;top:84px;right:16px;z-index:2147483646;width:380px;max-height:calc(100vh - 108px);display:flex;flex-direction:column;background:#fff;border:1px solid #e5e6eb;border-radius:8px;box-shadow:0 8px 28px rgba(29,33,41,.12);overflow:hidden;font:13px/1.4 "PingFang SC","Microsoft YaHei",sans-serif;color:#1d2129}
      #qc-fangzhou-panel header{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 14px;border-bottom:1px solid #f2f3f5;cursor:move;user-select:none;touch-action:none}
      #qc-fangzhou-panel header strong{font-size:14px}
      #qc-fangzhou-panel .qc-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end}
      #qc-fangzhou-panel button{border:0;background:transparent;color:#4e5969;cursor:pointer;font:inherit}
      #qc-fangzhou-panel .qc-switch{color:#4e5969;font-size:12px;display:flex;align-items:center;gap:4px}
      #qc-fangzhou-total{display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;padding:12px 14px;border-bottom:1px solid #f2f3f5}
      #qc-fangzhou-total .qc-sum-split{display:flex;gap:14px;align-items:flex-end}
      #qc-fangzhou-total span{display:block;color:#86909c;font-size:12px}
      #qc-fangzhou-total b,#qc-fangzhou-total strong{display:block;margin-top:2px;font-size:16px}
      #qc-fangzhou-total .qc-sum-main strong{font-size:22px}
      #qc-fangzhou-total .pos,.qc-profit-line strong.pos,.qc-profit-badge b.pos{color:#00b42a}
      #qc-fangzhou-total .neg,.qc-profit-line strong.neg,.qc-profit-badge b.neg{color:#f53f3f}
      #qc-fangzhou-list{overflow:auto;padding:8px}
      .qc-plan-card{border:1px solid #f2f3f5;border-radius:8px;padding:8px 10px;margin-bottom:8px}
      .qc-plan-card .qc-sub,.qc-profit-line em,.qc-profit-badge span{color:#86909c;font-size:12px;font-style:normal}
      .qc-source{display:flex;flex-direction:column;gap:2px;margin:6px 0;padding:6px 8px;background:#f7f8fa;border-radius:6px}
      .qc-source div{display:flex;justify-content:space-between;gap:8px;font-size:12px}
      .qc-source span{color:#86909c}
      .qc-source b.miss{color:#c9cdd4;font-weight:500}
      .qc-roi-line{display:flex;gap:6px;align-items:center;margin-top:6px}
      .qc-roi-line span{width:78px;color:#4e5969;font-size:12px}
      .qc-roi-line input{width:88px;height:28px;border:1px solid #e5e6eb;border-radius:4px;padding:0 8px}
      .qc-profit-badge{display:flex;flex-direction:row;align-items:center;gap:4px;width:max-content;max-width:220px;margin-top:6px;font:12px/1.35 "PingFang SC","Microsoft YaHei",sans-serif}
      .qc-profit-badge b{font-size:12px;font-weight:600;line-height:1.3}
      .qc-help{position:relative;display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;padding:0;border:1px solid #c9cdd4;border-radius:50%;background:#fff;color:#86909c;font:600 11px/14px "PingFang SC","Microsoft YaHei",sans-serif;cursor:help}
      .qc-help-tip{display:none !important;position:absolute;left:18px;top:16px;z-index:2147483647;width:280px;padding:8px 10px;border:1px solid #e5e6eb;border-radius:6px;background:#fff;box-shadow:0 8px 20px rgba(29,33,41,.12);color:#1d2129;font:400 12px/1.55 "PingFang SC","Microsoft YaHei",sans-serif;white-space:normal;text-align:left}
      .qc-help:hover .qc-help-tip{display:block !important}
      .qc-filled{color:#4e5969;font-size:12px}
    `;
    document.documentElement.appendChild(style);
  }

  function boot() {
    roiMap = loadMap();
    try {
      const raw = GM_getValue(POS_KEY, "");
      const pos = typeof raw === "string" ? JSON.parse(raw || "null") : raw;
      if (pos && Number.isFinite(pos.left) && Number.isFinite(pos.top)) panelPos = pos;
    } catch (error) {
      panelPos = null;
    }
    installStyle();
    if (!onUniPage()) return;
    readNow("boot");
    window.setInterval(() => {
      if (document.hidden || scrolling || !onUniPage()) return;
      scheduleRead("poll");
    }, 3000);
    const observer = new MutationObserver((mutations) => {
      if (scrolling || !onUniPage()) return;
      const own = mutations.every((mutation) => {
        const node = mutation.target.nodeType === 1 ? mutation.target : mutation.target.parentElement;
        return node && node.closest && node.closest(`#${PANEL_ID}, .qc-profit-badge`);
      });
      if (!own) scheduleRead("dom");
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    let seenPath = location.pathname;
    const onRoute = () => {
      if (location.pathname === seenPath) return;
      seenPath = location.pathname;
      holdAll = false;
      lastSig = "";
      log("路由变化", seenPath);
      if (!onUniPage()) {
        pagePlans = [];
        const panel = document.getElementById(PANEL_ID);
        if (panel) panel.remove();
        return;
      }
      scheduleRead("route");
    };
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
  }

  boot();
})();
