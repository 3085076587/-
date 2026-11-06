(() => {
  const { el, toast, api, setBusy, refreshTokenPill, advertiserOptions, queryParam } =
    window.AdHelper;
  const COMMISSION_KEY = "ad_helper_plan_commission";
  const commissionTimers = {};
  let plansCache = [];
  let listMeta = null;
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
    if (text === "") return 0;
    const n = Number(text);
    if (!Number.isFinite(n) || n < 0 || n > 100) return null;
    return n / 100;
  }

  function planProfit(item) {
    const pct = commissionPct(item);
    const cost = Number(item.stat_cost);
    const settle = Number(item.settle_amount);
    const refund = actualRefundRatio(item.actual_refund_rate);
    if (pct == null || !Number.isFinite(cost) || !Number.isFinite(settle) || refund == null) return null;
    return settle * (1 - refund) * (pct / 100) - cost;
  }

  function planProfitRate(item) {
    const profit = planProfit(item);
    const settle = Number(item && item.settle_amount);
    if (profit == null || !Number.isFinite(settle) || settle === 0) return null;
    return profit / settle;
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
    if (!rateNode) return;
    const rate = planProfitRate(item);
    rateNode.textContent = fmtProfitRate(rate);
    rateNode.classList.toggle("num-neg", rate != null && rate < 0);
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
    const profitText = profits.length
      ? `预估利润合计 ${fmtMoney(profits.reduce((sum, n) => sum + n, 0))}`
      : "预估利润合计 -";
    const scope = listMeta.note ? ` · ${listMeta.note}` : "";
    el("plan-hint").textContent = `利润 = 净成交 × (1 − 实际退款率) × 佣金 − 消耗。已加载 ${listMeta.total} 条 · ${listMeta.start} ~ ${listMeta.end} · ${profitText}${scope}`;
  }

  function renderPlans(list) {
    const tbody = el("plan-tbody");
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="12" class="empty">暂无计划</td></tr>';
      return;
    }
    tbody.innerHTML = list
      .map((item) => {
        const checked = item.enabled ? "checked" : "";
        const img = item.product_image
          ? `<img class="plan-thumb" src="${item.product_image}" alt="">`
          : "";
        const profit = planProfit(item);
        const rate = planProfitRate(item);
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

  async function loadAdvertisers(selected) {
    const list = await api("/api/advertiser/list");
    el("plan-advertiser").innerHTML = advertiserOptions(list, selected);
  }

  async function loadPlans() {
    const advertiserId = el("plan-advertiser").value;
    if (!advertiserId) {
      renderPlans([]);
      return;
    }
    const { start, end, custom } = rangeDates();
    if (custom && (!start || !end)) {
      toast("请选择开始和结束日期", "warn");
      return;
    }
    if (custom && start > end) {
      toast("开始日期不能晚于结束日期", "warn");
      return;
    }
    el("plan-hint").textContent = "正在拉取全域计划…";
    const qs = new URLSearchParams({
      advertiser_id: advertiserId,
      start_date: start,
      end_date: end,
    });
    try {
      const data = await api(`/api/plan/uni/list?${qs.toString()}`);
      plansCache = data.plans || [];
      const alive = new Set(plansCache.map((row) => String(row.ad_id)));
      for (const id of [...pickedIds]) {
        if (!alive.has(id)) pickedIds.delete(id);
      }
      listMeta = { total: data.total || 0, start, end, note: data.note || "" };
      renderPlans(plansCache);
      paintBatch();
      paintHint();
      if (opAdId) {
        const current = plansCache.find((row) => String(row.ad_id) === opAdId);
        if (current) paintDrawerSummary(current);
        else closeOpDrawer();
      }
      await migrateLocalCommission();
    } catch (err) {
      el("plan-tbody").innerHTML = `<tr><td colspan="12" class="empty">${err.message || "拉取失败"}</td></tr>`;
      el("plan-hint").textContent = err.message || "拉取失败";
      throw err;
    }
  }

  function paintBatch() {
    const count = pickedIds.size;
    el("btn-delete-plans").textContent = count ? `批量删除（${count}）` : "批量删除";
  }

  function paintDrawerSummary(item) {
    el("op-title").textContent = item.ad_name || "计划操作";
    const budget = item.budget != null ? item.budget : "-";
    el("op-sub").textContent = `ID ${item.ad_id} · ${statusText(item.status_label)} · 预算 ${budget} · ROI目标 ${fmtRoi(item.roi2_goal)}`;
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

  function parsedCommission(raw) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) return null;
    const pct = Number(text);
    if (!Number.isFinite(pct) || pct <= 0) return null;
    return pct;
  }

  async function persistCommission(item) {
    const advertiserId = Number(el("plan-advertiser").value);
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
    for (const item of plansCache) {
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
    if (changed) {
      renderPlans(plansCache);
      paintHint();
    }
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
      await loadPlans();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  el("plan-advertiser").addEventListener("change", () => {
    loadPlans().catch((err) => toast(err.message, "error"));
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
    const advertiserId = el("plan-advertiser").value;
    const adId = Number(toggle.getAttribute("data-toggle-ad"));
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
      await loadPlans();
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
    await api("/api/plan/uni/delete", {
      method: "POST",
      body: JSON.stringify({
        advertiser_id: Number(el("plan-advertiser").value),
        plans: items.map((item) => ({
          ad_id: String(item.ad_id),
          status: item.status || "",
          opt_status: item.opt_status || "",
          status_label: item.status_label || "",
          stat_cost: item.stat_cost,
        })),
      }),
    });
    items.forEach((item) => pickedIds.delete(String(item.ad_id)));
    const removedOpen = items.some((item) => String(item.ad_id) === opAdId);
    toast("计划已删除");
    await loadPlans();
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
    const qs = new URLSearchParams({
      advertiser_id: el("plan-advertiser").value,
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
    const advertiserId = el("plan-advertiser").value;
    const adId = el("roi-ad-id").value;
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
      await loadPlans();
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
      toast("请先选择子账户", "warn");
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
      await loadPlans();
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
    const selected = queryParam("advertiser_id") || "";
    await refreshTokenPill();
    await loadAdvertisers(selected);
    if (selected) await loadPlans();
  })().catch((err) => toast(err.message, "error"));
})();
