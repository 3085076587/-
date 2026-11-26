(() => {
  const { el, toast, api, setBusy, refreshTokenPill, advertiserOptions, queryParam } =
    window.AdHelper;
  const COMMISSION_KEY = "ad_helper_plan_commission";
  let refundRateMap = null;
  const commissionTimers = {};
  const refundTimers = {};
  let plansAll = [];
  let plansCache = [];
  let advertiserRows = [];
  let selectedPerson = "";
  let focusAdvertiser = "";
  let onlyRunning = false;
  let listMeta = null;
  let sortKey = "stat_cost";
  let sortAsc = false;
  const pickedIds = new Set();
  let opAdId = "";

  function localISODate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function syncPlanDates() {
    const custom = el("plan-range").value === "custom";
    document.querySelectorAll("#plan-custom-start, #plan-custom-end").forEach((node) => {
      node.classList.toggle("hidden", !custom);
    });
  }

  function rangeDates() {
    const key = el("plan-range").value || "today";
    if (key === "custom") {
      return { start: el("plan-start").value, end: el("plan-end").value, custom: true };
    }
    const end = new Date();
    const start = new Date();
    if (key === "7") start.setDate(end.getDate() - 6);
    else if (key === "30") start.setDate(end.getDate() - 29);
    return { start: localISODate(start), end: localISODate(end), custom: false };
  }

  function fmtMoney(v) {
    if (v == null || v === "") return "-";
    return Number(v).toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  const STATUS_LABELS = {
    SYSTEM_DISABLE: "系统暂停",
    OFFLINE_BUDGET: "预算不足",
    OFFLINE_BALANCE: "余额不足",
    AUDIT: "审核中",
    REAUDIT: "审核中",
    AUDIT_DENY: "审核不通过",
    TIME_NO_REACH: "未到投放时间",
    NO_SCHEDULE: "不在投放时段",
    FROZEN: "已终止",
    DELETE: "已删除",
    DELETED: "已删除",
  };

  function statusText(label) {
    const raw = label || "-";
    return STATUS_LABELS[raw] || raw;
  }

  function fmtRoi(v) {
    if (v == null || v === "") return "-";
    return Number(v).toFixed(2);
  }

  function loadCommissionMap() {
    try {
      const data = JSON.parse(localStorage.getItem(COMMISSION_KEY) || "{}");
      return data && typeof data === "object" ? data : {};
    } catch (_) {
      return {};
    }
  }

  function forgetLocalCommission(id) {
    const map = loadCommissionMap();
    if (!(String(id) in map)) return;
    delete map[String(id)];
    localStorage.setItem(COMMISSION_KEY, JSON.stringify(map));
  }

  function commissionPct(item) {
    const pct = Number(item && item.commission_pct);
    if (!Number.isFinite(pct) || pct <= 0) return null;
    return pct;
  }

  function commissionFormatWarn(item) {
    const msg = item && item.commission_format_warning;
    if (!msg || !String(msg).trim()) return "";
    const safe = String(msg)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
    return `<span class="commission-warn" title="${safe}" aria-label="佣金格式可能有问题">!</span>`;
  }

  function refundRatio(value) {
    let n = Number(value);
    if (!Number.isFinite(n) || n < 0) return null;
    if (n > 1 && n <= 100) n /= 100;
    if (n > 1) return null;
    return n;
  }

  function fmtRate(v) {
    const n = refundRatio(v);
    if (n == null) return "-";
    return `${(n * 100).toFixed(2)}%`;
  }

  function actualRefundRatio(value) {
    const text = String(value ?? "").replace("%", "").trim();
    if (text === "" || Number(text) === 0) return 0.15;
    const n = Number(text);
    if (!Number.isFinite(n) || n < 0 || n > 100) return null;
    return n / 100;
  }

  function planProfit(item) {
    const pct = commissionPct(item);
    const cost = Number(item.stat_cost);
    const settle = Number(item.settle_amount);
    const refund = 0.15;
    if (pct == null || !Number.isFinite(cost) || !Number.isFinite(settle)) return null;
    return settle * (1 - refund) * (pct / 100) - cost;
  }

  function planProfitRate(item) {
    const profit = planProfit(item);
    const settle = Number(item && item.settle_amount);
    if (profit == null || !Number.isFinite(settle) || settle === 0) return null;
    return profit / settle;
  }

  async function loadRefundRates() {
    if (refundRateMap) return;
    const map = new Map();
    try {
      const rows = await api("/api/product/refund-rates");
      (Array.isArray(rows) ? rows : []).forEach((row) => {
        const id = String((row && row.product_id) || "").trim();
        if (!id) return;
        const rate = row.refund_rate;
        map.set(id, rate == null || rate === "" ? null : Number(rate));
      });
    } catch (_) {
      refundRateMap = map;
      return;
    }
    refundRateMap = map;
  }

  function apiRefundPercent(item) {
    const ratio = apiRefundRatio(item);
    if (ratio == null) return null;
    return ratio * 100;
  }

  function apiRefundRatio(item) {
    if (!refundRateMap) return null;
    const id = String(item && item.product_id || "").trim();
    if (!id || !refundRateMap.has(id)) return null;
    const rate = refundRateMap.get(id);
    if (rate == null || !Number.isFinite(rate) || rate < 0 || rate > 100) return null;
    return rate / 100;
  }

  function planRealProfit(item) {
    const pct = commissionPct(item);
    const cost = Number(item.stat_cost);
    const settle = Number(item.settle_amount);
    const refund = apiRefundRatio(item);
    if (pct == null || !Number.isFinite(cost) || !Number.isFinite(settle) || refund == null) return null;
    return settle * (1 - refund) * (pct / 100) - cost;
  }

  function planRealProfitRate(item) {
    const profit = planRealProfit(item);
    const settle = Number(item && item.settle_amount);
    if (profit == null || !Number.isFinite(settle) || settle === 0) return null;
    return profit / settle;
  }

  function accountRealProfits(list) {
    const sums = new Map();
    list.forEach((item) => {
      const profit = planRealProfit(item);
      if (profit == null) return;
      const id = String(item.advertiser_id || "");
      sums.set(id, (sums.get(id) || 0) + profit);
    });
    return sums;
  }

  function fmtProfitRate(rate) {
    if (rate == null) return "-";
    return `${(rate * 100).toFixed(2)}%`;
  }

  function paintProfitCell(id) {
    const item = plansCache.find((row) => String(row.ad_id) === String(id));
    const node = document.querySelector(`[data-profit-for="${id}"]`);
    const rateNode = document.querySelector(`[data-profit-rate-for="${id}"]`);
    if (!item || !node) return;
    const profit = planProfit(item);
    node.textContent = profit == null ? "-" : fmtMoney(profit);
    node.classList.toggle("num-neg", profit != null && profit < 0);
    if (rateNode) {
      const rate = planProfitRate(item);
      rateNode.textContent = fmtProfitRate(rate);
      rateNode.classList.toggle("num-neg", rate != null && rate < 0);
    }
    const realNode = document.querySelector(`[data-real-profit-for="${id}"]`);
    const realRateNode = document.querySelector(`[data-real-rate-for="${id}"]`);
    const accountNode = document.querySelector(`[data-account-profit-for="${id}"]`);
    if (realNode) {
      const real = planRealProfit(item);
      realNode.textContent = real == null ? "-" : fmtMoney(real);
      realNode.classList.toggle("num-neg", real != null && real < 0);
    }
    if (realRateNode) {
      const realRate = planRealProfitRate(item);
      realRateNode.textContent = fmtProfitRate(realRate);
      realRateNode.classList.toggle("num-neg", realRate != null && realRate < 0);
    }
    if (accountNode) {
      const accountProfit = accountRealProfits(plansCache).get(String(item.advertiser_id || ""));
      accountNode.textContent = accountProfit == null ? "账号利润 -" : `账号利润 ${fmtMoney(accountProfit)}`;
    }
  }

  function moneyCell(value) {
    if (value == null || value === "") return `<td class="col-num">-</td>`;
    const n = Number(value);
    const cls = Number.isFinite(n) && n < 0 ? "col-num num-neg" : "col-num";
    return `<td class="${cls}">${fmtMoney(n)}</td>`;
  }

  function paintHint() {
    if (!listMeta || !el("plan-hint")) return;
    const profits = plansCache.map(planProfit).filter((n) => n != null);
    const realProfits = plansCache.map(planRealProfit).filter((n) => n != null);
    const profitText = profits.length
      ? `预估利润合计 ${fmtMoney(profits.reduce((sum, n) => sum + n, 0))}`
      : "预估利润合计 -";
    const realText = realProfits.length
      ? `利润合计 ${fmtMoney(realProfits.reduce((sum, n) => sum + n, 0))}`
      : "利润合计 -";
    paintSummary();
    const scope = listMeta.note ? ` · ${listMeta.note}` : "";
    const loaded = listMeta.loaded == null ? listMeta.total : listMeta.loaded;
    const countText = loaded === listMeta.total
      ? `已加载 ${loaded} 条`
      : `已加载 ${loaded} 条，当前 ${listMeta.total} 条`;
    el("plan-hint").textContent = `预估利润固定按 15% 退款率。利润和实际退款率用商品退款率接口，缓存 5 小时，没有这个商品的退款率时利润不计算。${countText} · ${listMeta.start} ~ ${listMeta.end} · ${profitText} · ${realText}${scope}`;
  }

  function setSum(id, text, value) {
    const node = el(id);
    if (!node) return;
    node.textContent = text;
    node.classList.toggle("num-gain", value != null && value > 0);
    node.classList.toggle("num-loss", value != null && value < 0);
  }

  function paintSummary() {
    const rows = plansCache;
    let cost = 0;
    let settle = 0;
    let profit = 0;
    let profitSettle = 0;
    let realProfit = 0;
    let realSettle = 0;
    let realCount = 0;
    let running = 0;
    rows.forEach((item) => {
      const itemCost = Number(item.stat_cost);
      const itemSettle = Number(item.settle_amount);
      if (Number.isFinite(itemCost)) cost += itemCost;
      if (Number.isFinite(itemSettle)) settle += itemSettle;
      if (item.enabled) running += 1;
      const one = planProfit(item);
      if (one != null) {
        profit += one;
        if (Number.isFinite(itemSettle)) profitSettle += itemSettle;
      }
      const real = planRealProfit(item);
      if (real != null) {
        realCount += 1;
        realProfit += real;
        if (Number.isFinite(itemSettle)) realSettle += itemSettle;
      }
    });
    const rate = profitSettle > 0 ? profit / profitSettle : null;
    const realRate = realSettle > 0 ? realProfit / realSettle : null;
    setSum("plan-sum-cost", fmtMoney(cost), null);
    setSum("plan-sum-settle", fmtMoney(settle), null);
    setSum("plan-sum-profit", fmtMoney(profit), profit);
    setSum("plan-sum-rate", fmtProfitRate(rate), rate);
    setSum("plan-sum-real-profit", realCount ? fmtMoney(realProfit) : "-", realCount ? realProfit : null);
    setSum("plan-sum-real-rate", realCount ? fmtProfitRate(realRate) : "-", realCount ? realRate : null);
    setSum("plan-sum-on", `${running} / ${rows.length}`, null);
  }

  function renderPlans(list) {
    const tbody = el("plan-tbody");
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="15" class="empty">${onlyRunning ? "没有投放中的计划" : "暂无计划"}</td></tr>`;
      return;
    }
    const accountTotals = accountRealProfits(list);
    tbody.innerHTML = list
      .map((item) => {
        const checked = item.enabled ? "checked" : "";
        const img = item.product_image
          ? `<img class="plan-thumb" src="${item.product_image}" alt="">`
          : "";
        const profit = planProfit(item);
        const rate = planProfitRate(item);
        const real = planRealProfit(item);
        const realRate = planRealProfitRate(item);
        const accountProfit = accountTotals.get(String(item.advertiser_id || ""));
        return `<tr data-ad-id="${item.ad_id}">
          <td class="col-switch">
            <label class="switch">
              <input type="checkbox" data-toggle-ad="${item.ad_id}" ${checked}>
              <span class="slider"></span>
            </label>
          </td>
          <td class="col-info">
            <div class="plan-info">
              ${img}
              <div>
                <div class="cell-title">${item.ad_name || "-"}</div>
                <div class="cell-sub">${esc(item.advertiser_name || "")}</div>
                <div class="cell-sub" data-account-profit-for="${item.ad_id}">${accountProfit == null ? "账号利润 -" : `账号利润 ${fmtMoney(accountProfit)}`}</div>
                <div class="cell-sub">${item.product_name || ""}</div>
                <div class="cell-sub mono">ID: ${item.ad_id}</div>
              </div>
            </div>
          </td>
          <td class="col-status">${statusText(item.status_label)}</td>
          <td class="col-budget">
            <div>预算 ${item.budget != null ? item.budget : "-"}</div>
            <div>ROI目标 <b>${fmtRoi(item.roi2_goal)}</b></div>
          </td>
          ${moneyCell(item.stat_cost)}
          ${moneyCell(item.settle_amount)}
          <td class="col-num">${fmtRoi(item.pay_roi)}</td>
          <td class="col-num">${fmtRate(item.refund_rate_1h)}</td>
          <td class="col-rate">${apiRefundPercent(item) == null ? "-" : `${apiRefundPercent(item).toFixed(2)}%`}</td>
          <td class="col-num${realRate != null && realRate < 0 ? " num-neg" : ""}" data-real-rate-for="${item.ad_id}">${fmtProfitRate(realRate)}</td>
          <td class="col-num${real != null && real < 0 ? " num-neg" : ""}" data-real-profit-for="${item.ad_id}">${real == null ? "-" : fmtMoney(real)}</td>
          <td class="col-num${rate != null && rate < 0 ? " num-neg" : ""}" data-profit-rate-for="${item.ad_id}">${fmtProfitRate(rate)}</td>
          <td class="col-profit">
            <div class="profit-with-rate">
              <span class="profit-num${profit != null && profit < 0 ? " num-neg" : ""}" data-profit-for="${item.ad_id}">${profit == null ? "-" : fmtMoney(profit)}</span>
              <input class="rate-input" type="number" min="0" max="100" step="0.1" placeholder="佣金%" data-commission-id="${item.ad_id}" value="${commissionPct(item) == null ? "" : commissionPct(item)}">${commissionFormatWarn(item)}
            </div>
          </td>
          <td class="col-count"><span class="countdown" data-remind-ad="${item.ad_id}">-</span></td>
          <td class="col-op">
            <button class="btn btn-op" type="button" data-open-op="${item.ad_id}">操作</button>
          </td>
        </tr>`;
      })
      .join("");
    if (window.AdReminders) window.AdReminders.refresh();
  }

  function ownerOf(row) {
    const matched = String((row && row.advertiser_name) || "").match(/\(([^)]+)\)/);
    if (matched) return matched[1].trim();
    return String((row && row.person) || "");
  }

  function planById(adId) {
    return plansAll.find((row) => String(row.ad_id) === String(adId)) || null;
  }

  function advertiserOf(itemOrId) {
    if (itemOrId && typeof itemOrId === "object" && itemOrId.advertiser_id) {
      return Number(itemOrId.advertiser_id);
    }
    const row = planById(itemOrId);
    return row && row.advertiser_id ? Number(row.advertiser_id) : 0;
  }

  function fillCreateAccounts() {
    const rows = advertiserRows.filter((row) => !selectedPerson || ownerOf(row) === selectedPerson);
    const current = focusAdvertiser || (el("plan-advertiser") && el("plan-advertiser").value) || "";
    el("plan-advertiser").innerHTML = advertiserOptions(rows, current);
  }

  async function loadAdvertisers() {
    advertiserRows = await api("/api/advertiser/list");
    fillCreateAccounts();
    renderPeople();
  }

  function renderPeople() {
    const box = el("plan-people");
    if (!box) return;
    const counts = new Map();
    advertiserRows.forEach((row) => {
      const person = ownerOf(row);
      if (!person) return;
      if (!counts.has(person)) counts.set(person, 0);
    });
    plansAll.forEach((plan) => {
      const person = plan.person || ownerOf(plan);
      if (!person) return;
      counts.set(person, (counts.get(person) || 0) + 1);
    });
    const names = [...counts.keys()].sort((a, b) => a.localeCompare(b, "zh"));
    const chips = [`<button type="button" class="chip${selectedPerson ? "" : " active"}" data-person="">全部 ${plansAll.length}</button>`];
    names.forEach((name) => {
      chips.push(`<button type="button" class="chip${selectedPerson === name ? " active" : ""}" data-person="${esc(name)}">${esc(name)} ${counts.get(name) || 0}</button>`);
    });
    box.innerHTML = chips.join("");
    const clearBtn = el("btn-clear-focus");
    if (clearBtn) clearBtn.classList.toggle("hidden", !focusAdvertiser);
  }

  function sortNumber(item, key) {
    if (key === "profit") return planProfit(item);
    if (key === "profit_rate") return planProfitRate(item);
    if (key === "real_profit") return planRealProfit(item);
    if (key === "real_profit_rate") return planRealProfitRate(item);
    if (key === "commission_pct") return commissionPct(item);
    if (key === "actual_refund_rate") return apiRefundPercent(item);
    const value = Number(item[key]);
    return Number.isFinite(value) ? value : null;
  }

  function comparePlans(a, b) {
    const left = sortNumber(a, sortKey);
    const right = sortNumber(b, sortKey);
    if (left == null && right == null) return 0;
    if (left == null) return 1;
    if (right == null) return -1;
    return sortAsc ? left - right : right - left;
  }

  function paintSortMarks() {
    document.querySelectorAll("#plans-table [data-sort]").forEach((node) => {
      const mark = node.querySelector(".sort-ind");
      if (!mark) return;
      mark.textContent = node.getAttribute("data-sort") === sortKey ? (sortAsc ? "↑" : "↓") : "";
    });
  }

  function paintRunningFilter() {
    const card = el("plan-filter-on");
    if (!card) return;
    card.classList.toggle("active", onlyRunning);
  }

  function applyVisible() {
    plansCache = plansAll.filter((row) => {
      if (focusAdvertiser && String(row.advertiser_id) !== String(focusAdvertiser)) return false;
      if (selectedPerson && (row.person || ownerOf(row)) !== selectedPerson) return false;
      if (onlyRunning && !row.enabled) return false;
      return true;
    });
    const alive = new Set(plansCache.map((row) => String(row.ad_id)));
    for (const id of [...pickedIds]) {
      if (!alive.has(id)) pickedIds.delete(id);
    }
    plansCache.sort(comparePlans);
    if (listMeta) listMeta.total = plansCache.length;
    renderPlans(plansCache);
    paintSortMarks();
    paintBatch();
    paintHint();
    paintRunningFilter();
    if (opAdId) {
      const current = planById(opAdId);
      if (current && alive.has(opAdId)) paintDrawerSummary(current);
      else closeOpDrawer();
    }
  }

  function parsePlanBody(text) {
    const fixed = String(text || "")
      .replace(/"product_id"\s*:\s*(\d{15,})/g, '"product_id":"$1"')
      .replace(/"product_ids"\s*:\s*\[([^\]]*)\]/g, (_, inner) => `"product_ids":[${inner.replace(/(\d{15,})/g, '"$1"')}]`);
    let body;
    try {
      body = JSON.parse(fixed);
    } catch (_) {
      throw new Error("接口返回非 JSON");
    }
    if (typeof body.code === "undefined") {
      throw new Error(body.detail ? `接口不可用：${body.detail}` : "接口异常");
    }
    if (body.code === 40100) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = `/login?next=${next}`;
      throw new Error(body.msg || "未登录");
    }
    if (body.code !== 0) throw new Error(body.msg || "请求失败");
    return body.data;
  }

  async function apiPlans(path) {
    const resp = await fetch(path, { headers: { "Content-Type": "application/json" } });
    return parsePlanBody(await resp.text());
  }

  async function loadPlans(refresh) {
    const { start, end, custom } = rangeDates();
    if (custom && (!start || !end)) {
      toast("请选择开始和结束日期", "warn");
      return;
    }
    if (custom && start > end) {
      toast("开始日期不能晚于结束日期", "warn");
      return;
    }
    el("plan-hint").textContent = "正在读取各账户的计划…";
    el("plan-tbody").innerHTML = '<tr><td colspan="15" class="empty">正在读取各账户的计划…</td></tr>';
    const qs = new URLSearchParams({
      start_date: start,
      end_date: end,
      refresh: refresh ? "1" : "0",
    });
    try {
      const [data] = await Promise.all([
        apiPlans(`/api/plan/uni/board?${qs.toString()}`),
        loadRefundRates(),
      ]);
      plansAll = data.plans || [];
      listMeta = {
        total: plansAll.length,
        loaded: data.total || plansAll.length,
        start,
        end,
        note: data.note || "",
      };
      renderPeople();
      applyVisible();
      await migrateLocalCommission();
    } catch (err) {
      el("plan-tbody").innerHTML = `<tr><td colspan="15" class="empty">${esc(err.message || "拉取失败")}</td></tr>`;
      el("plan-hint").textContent = err.message || "拉取失败";
      throw err;
    }
  }

  async function reloadAccount(advertiserId) {
    const { start, end } = rangeDates();
    const qs = new URLSearchParams({
      advertiser_id: String(advertiserId),
      start_date: start,
      end_date: end,
    });
    const data = await apiPlans(`/api/plan/uni/list?${qs.toString()}`);
    const shop = advertiserRows.find((row) => String(row.advertiser_id) === String(advertiserId));
    const fresh = (data.plans || []).map((plan) => {
      plan.advertiser_id = Number(advertiserId);
      plan.advertiser_name = shop ? shop.advertiser_name : (plan.advertiser_name || "");
      plan.person = shop ? ownerOf(shop) : (plan.person || "");
      return plan;
    });
    plansAll = plansAll
      .filter((row) => String(row.advertiser_id) !== String(advertiserId))
      .concat(fresh);
    if (listMeta) listMeta.loaded = plansAll.length;
    renderPeople();
    applyVisible();
  }

  function paintBatch() {
    const count = pickedIds.size;
    el("btn-delete-plans").textContent = count ? `批量删除（${count}）` : "批量删除";
  }

  function paintDrawerSummary(item) {
    el("op-title").textContent = item.ad_name || "计划操作";
    const budget = item.budget != null ? item.budget : "-";
    const account = item.advertiser_name ? `${item.advertiser_name} · ` : "";
    el("op-sub").textContent = `${account}ID ${item.ad_id} · ${statusText(item.status_label)} · 预算 ${budget} · ROI目标 ${fmtRoi(item.roi2_goal)}`;
  }

  function closeOpDrawer() {
    el("op-drawer").classList.add("hidden");
    opAdId = "";
  }

  function openOpDrawer(item) {
    opAdId = String(item.ad_id);
    paintDrawerSummary(item);
    el("roi-ad-id").value = item.ad_id;
    el("roi-ad-name").value = item.ad_name || "";
    el("roi-value").value = item.roi2_goal == null ? "" : item.roi2_goal;
    el("op-pick").checked = pickedIds.has(opAdId);
    el("op-drawer").classList.remove("hidden");
    openPlanLogs(item.ad_id);
  }

  function refundInputValue(item) {
    return String(item && item.actual_refund_rate != null ? item.actual_refund_rate : "").replace("%", "").replace(/"/g, "");
  }

  function parsedRefund(raw) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) return "";
    const n = Number(text);
    if (!Number.isFinite(n) || n < 0 || n > 100) return null;
    return text;
  }

  function applyRefund(productId, text, advertiserId) {
    plansAll.forEach((row) => {
      if (advertiserId && Number(row.advertiser_id) !== Number(advertiserId)) return;
      if (String(row.product_id || "") !== String(productId || "")) return;
      row.actual_refund_rate = text;
      paintProfitCell(row.ad_id);
      const input = document.querySelector(`[data-refund-id="${row.ad_id}"]`);
      if (input && document.activeElement !== input) input.value = text || "";
    });
    paintHint();
  }

  async function persistRefund(item) {
    const advertiserId = advertiserOf(item);
    const productId = String((item && item.product_id) || "").trim();
    if (!advertiserId || !item) return;
    if (!productId || productId === "0") {
      toast("这条计划没有商品，填不了实际退款率", "warn");
      return;
    }
    const typed = parsedRefund(item.actual_refund_rate);
    try {
      const data = await api("/api/product/refund-rate", {
        method: "POST",
        body: JSON.stringify({
          advertiser_id: advertiserId,
          product_id: productId,
          product_name: item.product_name || "",
          actual_refund_rate: typed == null ? "" : typed,
        }),
      });
      applyRefund(productId, data.actual_refund_rate || "", advertiserId);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function parsedCommission(raw) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) return null;
    const pct = Number(text);
    if (!Number.isFinite(pct) || pct <= 0) return null;
    return pct;
  }

  async function persistCommission(item) {
    const advertiserId = advertiserOf(item);
    if (!advertiserId || !item) return;
    try {
      const data = await api("/api/plan/commission", {
        method: "POST",
        body: JSON.stringify({
          advertiser_id: advertiserId,
          ad_id: Number(item.ad_id),
          commission_pct: commissionPct(item),
        }),
      });
      item.commission_pct = data.commission_pct;
      forgetLocalCommission(item.ad_id);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function migrateLocalCommission() {
    const map = loadCommissionMap();
    let changed = false;
    for (const item of plansAll) {
      if (commissionPct(item) != null) {
        forgetLocalCommission(item.ad_id);
        continue;
      }
      const pct = parsedCommission(map[String(item.ad_id)]);
      if (pct == null) continue;
      item.commission_pct = pct;
      await persistCommission(item);
      changed = true;
    }
    if (changed) applyVisible();
  }

  el("plan-tbody").addEventListener("input", (event) => {
    const input = event.target.closest("[data-commission-id]");
    if (!input) return;
    const id = input.getAttribute("data-commission-id");
    const item = plansCache.find((row) => String(row.ad_id) === String(id));
    if (!item) return;
    item.commission_pct = parsedCommission(input.value);
    paintProfitCell(id);
    paintHint();
    clearTimeout(commissionTimers[id]);
    commissionTimers[id] = setTimeout(() => persistCommission(item), 400);
  });

  el("btn-load-plans").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    setBusy(btn, true);
    try {
      await loadPlans(true);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  document.querySelector("#plans-table thead").addEventListener("click", (event) => {
    const sort = event.target.closest("[data-sort]");
    if (!sort) return;
    const key = sort.getAttribute("data-sort");
    if (sortKey === key) sortAsc = !sortAsc;
    else {
      sortKey = key;
      sortAsc = false;
    }
    applyVisible();
  });

  el("plan-people").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-person]");
    if (!btn) return;
    selectedPerson = btn.getAttribute("data-person") || "";
    focusAdvertiser = "";
    fillCreateAccounts();
    renderPeople();
    applyVisible();
  });
  el("btn-clear-focus").addEventListener("click", () => {
    focusAdvertiser = "";
    fillCreateAccounts();
    renderPeople();
    applyVisible();
  });
  el("plan-filter-on").addEventListener("click", () => {
    onlyRunning = !onlyRunning;
    paintRunningFilter();
    if (plansAll.length) applyVisible();
  });
  el("plan-range").addEventListener("change", () => {
    syncPlanDates();
    if (el("plan-range").value === "custom" && (!el("plan-start").value || !el("plan-end").value)) return;
    loadPlans().catch((err) => toast(err.message, "error"));
  });
  el("plan-start").addEventListener("change", () => {
    if (el("plan-end").value) loadPlans().catch((err) => toast(err.message, "error"));
  });
  el("plan-end").addEventListener("change", () => {
    if (el("plan-start").value) loadPlans().catch((err) => toast(err.message, "error"));
  });

  document.body.addEventListener("change", async (event) => {
    const toggle = event.target.closest("[data-toggle-ad]");
    if (!toggle) return;
    const adId = Number(toggle.getAttribute("data-toggle-ad"));
    const advertiserId = advertiserOf(adId);
    const optStatus = toggle.checked ? "ENABLE" : "DISABLE";
    toggle.disabled = true;
    try {
      await api("/api/plan/update-status", {
        method: "POST",
        body: JSON.stringify({
          advertiser_id: Number(advertiserId),
          ad_id: adId,
          opt_status: optStatus,
        }),
      });
      toast(optStatus === "ENABLE" ? "计划已开启" : "计划已暂停");
      await reloadAccount(advertiserId);
    } catch (err) {
      toggle.checked = !toggle.checked;
      toast(err.message, "error");
    } finally {
      toggle.disabled = false;
    }
  });

  function selectedPlans() {
    return plansCache.filter((row) => pickedIds.has(String(row.ad_id)));
  }

  async function deletePlans(items) {
    if (!items.length) {
      toast("请选择要删除的计划", "warn");
      return;
    }
    if (items.length > 10) {
      toast("一次最多删除 10 个计划", "warn");
      return;
    }
    const spent = items.filter((item) => Number(item.stat_cost) > 0);
    const names = items.map((item) => item.ad_name || item.ad_id).join("、");
    let message = `确定删除这 ${items.length} 个计划？${names}`;
    if (spent.length) {
      message += "。其中有已经产生消耗的计划，平台可能拒绝删除，拒绝原因会直接显示。";
    }
    if (!window.confirm(message)) return;
    const groups = new Map();
    items.forEach((item) => {
      const advertiserId = String(advertiserOf(item));
      if (!groups.has(advertiserId)) groups.set(advertiserId, []);
      groups.get(advertiserId).push(item);
    });
    for (const [advertiserId, group] of groups) {
      await api("/api/plan/uni/delete", {
        method: "POST",
        body: JSON.stringify({
          advertiser_id: Number(advertiserId),
          plans: group.map((item) => ({
            ad_id: String(item.ad_id),
            status: item.status || "",
            opt_status: item.opt_status || "",
            status_label: item.status_label || "",
            stat_cost: item.stat_cost,
          })),
        }),
      });
    }
    items.forEach((item) => pickedIds.delete(String(item.ad_id)));
    const removedOpen = items.some((item) => String(item.ad_id) === opAdId);
    toast("计划已删除");
    for (const advertiserId of groups.keys()) {
      await reloadAccount(advertiserId);
    }
    if (removedOpen) closeOpDrawer();
    paintBatch();
  }

  let logAdId = "";
  let logPage = 1;
  let logTotalPage = 1;

  function renderLogs(logs) {
    const body = el("log-tbody");
    const rows = [];
    (logs || []).forEach((entry) => {
      const changes = entry.changes && entry.changes.length
        ? entry.changes
        : [{ field: "", before: "", after: entry.content_title || "-", focus: false, raw: "" }];
      changes.forEach((change) => {
        const before = change.before || (change.field ? "" : change.raw || "-");
        const after = change.after || "-";
        const kind = change.field || entry.content_title || "-";
        rows.push(`<tr class="${change.focus ? "log-focus" : ""}">
          <td>${esc(entry.create_time || "-")}</td>
          <td>${esc(entry.operator_name || "-")}</td>
          <td>${esc(kind)}</td>
          <td>${esc(before || "-")}</td>
          <td>${esc(after || "-")}</td>
        </tr>`);
      });
    });
    body.innerHTML = rows.length
      ? rows.join("")
      : '<tr><td colspan="5" class="empty">这个时间段没有操作日志</td></tr>';
  }

  async function loadPlanLogs(page) {
    if (!logAdId) return;
    logPage = page;
    const row = planById(logAdId);
    const qs = new URLSearchParams({
      advertiser_id: row ? String(row.advertiser_id) : "",
      ad_id: logAdId,
      start_date: el("log-start").value,
      end_date: el("log-end").value,
      page: String(page),
    });
    const data = await api(`/api/plan/uni/logs?${qs.toString()}`);
    logTotalPage = data.total_page || 1;
    el("log-page-label").textContent = `第 ${data.page || page} / ${logTotalPage} 页，共 ${data.total || 0} 条`;
    renderLogs(data.logs || []);
  }

  function openPlanLogs(adId) {
    logAdId = String(adId);
    const range = rangeDates();
    el("log-start").value = range.start;
    el("log-end").value = range.end;
    el("log-tbody").innerHTML = '<tr><td colspan="5" class="empty">正在查询…</td></tr>';
    loadPlanLogs(1).catch((err) => {
      el("log-tbody").innerHTML = `<tr><td colspan="5" class="empty">${esc(err.message)}</td></tr>`;
      toast(err.message, "error");
    });
  }

  document.body.addEventListener("click", (event) => {
    const openBtn = event.target.closest("[data-open-op]");
    if (!openBtn) return;
    const item = plansCache.find((row) => String(row.ad_id) === openBtn.getAttribute("data-open-op"));
    if (!item) return;
    openOpDrawer(item);
  });

  el("btn-delete-plans").addEventListener("click", () => {
    deletePlans(selectedPlans()).catch((err) => toast(err.message, "error"));
  });
  el("op-close").addEventListener("click", closeOpDrawer);
  el("op-drawer").addEventListener("click", (event) => {
    if (event.target === el("op-drawer")) closeOpDrawer();
  });
  el("op-pick").addEventListener("change", () => {
    if (!opAdId) return;
    if (el("op-pick").checked) pickedIds.add(opAdId);
    else pickedIds.delete(opAdId);
    paintBatch();
  });
  el("op-delete").addEventListener("click", () => {
    const item = plansCache.find((row) => String(row.ad_id) === opAdId);
    if (!item) return;
    deletePlans([item]).catch((err) => toast(err.message, "error"));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el("op-drawer").classList.contains("hidden")) closeOpDrawer();
  });
  el("btn-log-search").addEventListener("click", () => {
    loadPlanLogs(1).catch((err) => toast(err.message, "error"));
  });
  el("btn-log-prev").addEventListener("click", () => {
    if (logPage <= 1) return;
    loadPlanLogs(logPage - 1).catch((err) => toast(err.message, "error"));
  });
  el("btn-log-next").addEventListener("click", () => {
    if (logPage >= logTotalPage) return;
    loadPlanLogs(logPage + 1).catch((err) => toast(err.message, "error"));
  });

  function remindSeconds() {
    const mode = el("roi-remind").value;
    if (mode === "custom") {
      const mins = Number(el("roi-remind-custom").value || 0);
      if (!mins || mins < 1) return null;
      return Math.round(mins * 60);
    }
    return Number(mode || 0);
  }

  el("roi-remind").addEventListener("change", () => {
    const custom = el("roi-remind").value === "custom";
    el("roi-remind-custom-wrap").classList.toggle("hidden", !custom);
  });

  el("btn-roi-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const adId = el("roi-ad-id").value;
    const advertiserId = advertiserOf(adId);
    const adName = el("roi-ad-name").value;
    const roi2Goal = Number(el("roi-value").value);
    const remindAfter = remindSeconds();
    if (!advertiserId || !adId || !roi2Goal) {
      toast("请填写完整 ROI 信息", "warn");
      return;
    }
    if (el("roi-remind").value === "custom" && remindAfter == null) {
      toast("请填写自定义提醒分钟数", "warn");
      return;
    }
    setBusy(btn, true);
    try {
      const data = await api("/api/plan/update-roi", {
        method: "POST",
        body: JSON.stringify({
          advertiser_id: Number(advertiserId),
          ad_id: Number(adId),
          ad_name: adName,
          roi2_goal: roi2Goal,
          remind_after_seconds: remindAfter || null,
        }),
      });
      toast(data.reminder ? "ROI 已更新，倒计时已开始" : "ROI 已更新");
      await reloadAccount(advertiserId);
      if (window.AdReminders) window.AdReminders.refresh();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  function esc(text) {
    return String(text || "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  const pickedProducts = new Set();
  let productPage = 1;
  let productTotalPage = 1;
  let uploadedVideo = null;

  function createAdvertiserId() {
    const advertiserId = el("plan-advertiser").value;
    if (!advertiserId) {
      toast("请选择投放账户", "warn");
      return "";
    }
    return advertiserId;
  }

  function hasAweme() {
    return Boolean(el("create-aweme").value);
  }

  function paintCreateMode() {
    const custom = el("create-bid").value === "SMART_BID_CUSTOM";
    el("create-roi-wrap").classList.toggle("hidden", !custom);
    el("create-dates").classList.toggle("hidden", el("create-schedule").value !== "SCHEDULE_START_END");
  }

  async function postForm(path, form) {
    const resp = await fetch(path, { method: "POST", body: form });
    let body;
    try {
      body = await resp.json();
    } catch (_) {
      throw new Error(`接口返回非 JSON（HTTP ${resp.status}）`);
    }
    if (body.code === 40100) {
      location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
      throw new Error(body.msg || "未登录");
    }
    if (body.code !== 0) throw new Error(body.msg || "请求失败");
    return body.data;
  }

  async function uploadMaterial(input) {
    const file = input.files && input.files[0];
    if (!file) return null;
    const advertiserId = createAdvertiserId();
    if (!advertiserId) {
      input.value = "";
      return null;
    }
    const form = new FormData();
    form.append("advertiser_id", advertiserId);
    form.append("file", file);
    const data = await postForm("/api/plan/uni/upload-video", form);
    uploadedVideo = data;
    el("create-video-name").textContent = `已上传 ${file.name}（${data.image_mode === "VIDEO_VERTICAL" ? "竖版" : "横版"}）`;
    return data;
  }

  function renderProducts(list) {
    const box = el("create-products");
    if (!list.length) {
      box.innerHTML = '<div class="pick-row muted">没有可显示的商品</div>';
      return;
    }
    box.innerHTML = list.map((item) => {
      const checked = pickedProducts.has(String(item.product_id)) ? "checked" : "";
      const disabled = item.selectable ? "" : "disabled";
      const reason = item.reason ? `<div class="cell-sub">${esc(item.reason)}</div>` : "";
      return `<label class="pick-row ${disabled}">
        <input type="checkbox" data-product-id="${item.product_id}" ${checked} ${disabled}>
        <span>
          <div>${esc(item.name || "未命名商品")}</div>
          <div class="cell-sub mono">ID: ${item.product_id}</div>
          ${reason}
        </span>
      </label>`;
    }).join("");
  }

  async function loadAwemes() {
    const advertiserId = createAdvertiserId();
    const select = el("create-aweme");
    select.innerHTML = '<option value="">无号商家</option>';
    if (!advertiserId) return;
    const rows = await api(`/api/plan/uni/awemes?advertiser_id=${advertiserId}`);
    select.innerHTML += (rows || []).map((item) => {
      const label = `${esc(item.aweme_name || "未命名")}（${esc(item.aweme_show_id || item.aweme_id)}）`;
      const reason = item.reason ? ` ${esc(item.reason)}` : "";
      return `<option value="${item.aweme_id}" ${item.disabled ? "disabled" : ""}>${label}${item.disabled ? " 不可用" : ""}${reason}</option>`;
    }).join("");
    const usable = (rows || []).find((item) => !item.disabled);
    if (usable) select.value = String(usable.aweme_id);
  }

  async function loadCreateProducts(page) {
    const advertiserId = createAdvertiserId();
    if (!advertiserId) return;
    productPage = page;
    const qs = new URLSearchParams({
      advertiser_id: advertiserId,
      keyword: el("create-keyword").value.trim(),
      page: String(page),
    });
    const awemeId = el("create-aweme").value;
    if (awemeId) qs.set("aweme_id", awemeId);
    let data;
    try {
      data = await api(`/api/plan/uni/products?${qs.toString()}`);
    } catch (err) {
      el("create-products").innerHTML = `<div class="pick-row muted">${esc(err.message)}</div>`;
      el("create-page-label").textContent = awemeId ? "商品加载失败" : "请先选择抖音号";
      throw err;
    }
    productTotalPage = data.total_page || 1;
    el("create-page-label").textContent = `第 ${data.page || page} / ${productTotalPage} 页，已选 ${pickedProducts.size} 个`;
    renderProducts(data.products || []);
  }

  function resetCreateUploads() {
    uploadedVideo = null;
    el("create-video").value = "";
    el("create-video-name").textContent = "未上传";
    const commission = el("create-commission");
    if (commission) commission.value = "";
  }

  async function openCreate() {
    if (!createAdvertiserId()) return;
    pickedProducts.clear();
    resetCreateUploads();
    el("create-modal").classList.remove("hidden");
    paintCreateMode();
    try {
      await loadAwemes();
      paintCreateMode();
      await loadCreateProducts(1);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  el("btn-create-plan").addEventListener("click", () => {
    openCreate().catch((err) => toast(err.message, "error"));
  });
  el("btn-create-cancel").addEventListener("click", () => {
    el("create-modal").classList.add("hidden");
  });
  el("create-bid").addEventListener("change", paintCreateMode);
  el("create-schedule").addEventListener("change", paintCreateMode);
  el("create-aweme").addEventListener("change", () => {
    pickedProducts.clear();
    paintCreateMode();
    loadCreateProducts(1).catch((err) => toast(err.message, "error"));
  });
  el("btn-search-products").addEventListener("click", () => {
    loadCreateProducts(1).catch((err) => toast(err.message, "error"));
  });
  el("btn-product-prev").addEventListener("click", () => {
    if (productPage <= 1) return;
    loadCreateProducts(productPage - 1).catch((err) => toast(err.message, "error"));
  });
  el("btn-product-next").addEventListener("click", () => {
    if (productPage >= productTotalPage) return;
    loadCreateProducts(productPage + 1).catch((err) => toast(err.message, "error"));
  });
  el("create-products").addEventListener("change", (event) => {
    const box = event.target.closest("[data-product-id]");
    if (!box) return;
    const id = box.getAttribute("data-product-id");
    if (box.checked) {
      if (pickedProducts.size >= 30) {
        box.checked = false;
        toast("最多选择 30 个商品", "warn");
        return;
      }
      pickedProducts.add(id);
    } else {
      pickedProducts.delete(id);
    }
    el("create-page-label").textContent = `第 ${productPage} / ${productTotalPage} 页，已选 ${pickedProducts.size} 个`;
  });

  async function onMaterialChange(input) {
    try {
      await uploadMaterial(input);
    } catch (err) {
      input.value = "";
      toast(err.message, "error");
    }
  }
  el("create-video").addEventListener("change", (event) => onMaterialChange(event.target));

  el("btn-create-save").addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const advertiserId = createAdvertiserId();
    if (!advertiserId) return;
    if (!pickedProducts.size) {
      toast("请选择商品", "warn");
      return;
    }
    const awemeId = el("create-aweme").value;
    const body = {
      advertiser_id: Number(advertiserId),
      name: el("create-name").value.trim(),
      aweme_id: awemeId ? Number(awemeId) : null,
      smart_bid_type: el("create-bid").value,
      roi2_goal: el("create-bid").value === "SMART_BID_CUSTOM" ? Number(el("create-roi").value) : null,
      budget: Number(el("create-budget").value),
      video_schedule_type: el("create-schedule").value,
      start_time: el("create-start").value || null,
      end_time: el("create-end").value || null,
      qcpx_mode: el("create-qcpx").value,
      deep_external_action: el("create-goal").value,
      product_ids: Array.from(pickedProducts),
      title: el("create-title").value.trim(),
      video_id: uploadedVideo ? uploadedVideo.video_id : null,
      video_image_mode: uploadedVideo ? uploadedVideo.image_mode : null,
    };
    if (!body.name || !body.budget) {
      toast("请填写计划名称和日预算", "warn");
      return;
    }
    if (!body.video_id) {
      toast("请先上传视频", "warn");
      return;
    }
    setBusy(btn, true);
    try {
      const data = await api("/api/plan/uni/create", {
        method: "POST",
        body: JSON.stringify(body),
      });
      const pct = parsedCommission(el("create-commission").value);
      if (pct != null) {
        await api("/api/plan/commission", {
          method: "POST",
          body: JSON.stringify({
            advertiser_id: Number(advertiserId),
            ad_id: data.ad_id,
            commission_pct: pct,
          }),
        });
      }
      toast(`计划已创建，ID ${data.ad_id}`);
      el("create-modal").classList.add("hidden");
      await reloadAccount(advertiserId);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  (async () => {
    const today = localISODate(new Date());
    el("plan-start").value = today;
    el("plan-end").value = today;
    syncPlanDates();
    focusAdvertiser = queryParam("advertiser_id") || "";
    await refreshTokenPill();
    await loadAdvertisers();
    if (focusAdvertiser) {
      const row = advertiserRows.find((item) => String(item.advertiser_id) === String(focusAdvertiser));
      if (row) selectedPerson = ownerOf(row);
      fillCreateAccounts();
    }
    await loadPlans();
  })().catch((err) => toast(err.message, "error"));
})();
