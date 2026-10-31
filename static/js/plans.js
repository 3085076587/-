(() => {
  const { el, toast, api, setBusy, refreshTokenPill, advertiserOptions, queryParam } =
    window.AdHelper;
  const COMMISSION_KEY = "ad_helper_plan_commission";
  const REFUND_EXTRA = 0.05;
  const commissionTimers = {};
  let plansCache = [];
  let listMeta = null;

  function localISODate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function rangeDates() {
    const end = new Date();
    const start = new Date();
    const key = el("plan-range").value || "today";
    if (key === "7") start.setDate(end.getDate() - 6);
    else if (key === "30") start.setDate(end.getDate() - 29);
    return { start: localISODate(start), end: localISODate(end) };
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

  function refundRatio(value) {
    let n = Number(value);
    if (!Number.isFinite(n) || n < 0) return null;
    if (n > 1 && n <= 100) n /= 100;
    if (n > 1) return null;
    return n;
  }

  function displayRefund(value) {
    const n = refundRatio(value);
    if (n == null) return null;
    return Math.min(1, n + REFUND_EXTRA);
  }

  function fmtRate(v) {
    const n = displayRefund(v);
    if (n == null) return "-";
    return `${(n * 100).toFixed(2)}%`;
  }

  function planProfit(item) {
    const pct = commissionPct(item);
    const cost = Number(item.stat_cost);
    const roi = Number(item.pay_roi);
    const refund = displayRefund(item.refund_rate_1h);
    if (pct == null || !Number.isFinite(cost) || !Number.isFinite(roi) || refund == null) return null;
    return cost * roi * (1 - refund) * (pct / 100) - cost;
  }

  function paintProfitCell(id) {
    const item = plansCache.find((row) => String(row.ad_id) === String(id));
    const node = document.querySelector(`[data-profit-for="${id}"]`);
    if (!item || !node) return;
    const profit = planProfit(item);
    node.textContent = profit == null ? "-" : fmtMoney(profit);
    node.classList.toggle("num-neg", profit != null && profit < 0);
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
      ? `利润合计 ${fmtMoney(profits.reduce((sum, n) => sum + n, 0))}`
      : "利润合计 -";
    el("plan-hint").textContent = `1小时内退款率已加 5%。已加载 ${listMeta.total} 条 · ${listMeta.start} ~ ${listMeta.end} · ${profitText}`;
  }

  function renderPlans(list) {
    const tbody = el("plan-tbody");
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="empty">暂无计划</td></tr>';
      return;
    }
    tbody.innerHTML = list
      .map((item) => {
        const checked = item.enabled ? "checked" : "";
        const img = item.product_image
          ? `<img class="plan-thumb" src="${item.product_image}" alt="">`
          : "";
        const profit = planProfit(item);
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
          <td class="col-profit">
            <div class="profit-with-rate">
              <span class="profit-num${profit != null && profit < 0 ? " num-neg" : ""}" data-profit-for="${item.ad_id}">${profit == null ? "-" : fmtMoney(profit)}</span>
              <input class="rate-input" type="number" min="0" max="100" step="0.1" placeholder="佣金%" data-commission-id="${item.ad_id}" value="${commissionPct(item) == null ? "" : commissionPct(item)}">
            </div>
          </td>
          <td class="col-count"><span class="countdown" data-remind-ad="${item.ad_id}">-</span></td>
          <td class="col-op">
            <button class="btn btn-accent"
              data-edit-roi="${item.ad_id}"
              data-roi="${item.roi2_goal ?? ""}"
              data-name="${(item.ad_name || "").replace(/"/g, "&quot;")}">设 ROI</button>
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
    const { start, end } = rangeDates();
    el("plan-hint").textContent = "正在拉取全域计划…";
    const qs = new URLSearchParams({
      advertiser_id: advertiserId,
      start_date: start,
      end_date: end,
    });
    try {
      const data = await api(`/api/plan/uni/list?${qs.toString()}`);
      plansCache = data.plans || [];
      listMeta = { total: data.total || 0, start, end };
      renderPlans(plansCache);
      paintHint();
      await migrateLocalCommission();
    } catch (err) {
      el("plan-tbody").innerHTML = `<tr><td colspan="11" class="empty">${err.message || "拉取失败"}</td></tr>`;
      el("plan-hint").textContent = err.message || "拉取失败";
      throw err;
    }
  }

  function openRoiModal(adId, currentRoi, name) {
    el("roi-ad-id").value = adId;
    el("roi-ad-name").value = name || "";
    el("roi-ad-label").textContent = `${name || "-"}（ID: ${adId}）`;
    el("roi-value").value = currentRoi === "" || currentRoi == null ? "" : currentRoi;
    el("roi-modal").classList.remove("hidden");
  }

  function closeRoiModal() {
    el("roi-modal").classList.add("hidden");
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
    loadPlans().catch((err) => toast(err.message, "error"));
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

  document.body.addEventListener("click", (event) => {
    const roiBtn = event.target.closest("[data-edit-roi]");
    if (roiBtn) {
      openRoiModal(
        roiBtn.getAttribute("data-edit-roi"),
        roiBtn.getAttribute("data-roi"),
        roiBtn.getAttribute("data-name")
      );
    }
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

  el("btn-roi-cancel").addEventListener("click", closeRoiModal);
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
      closeRoiModal();
      await loadPlans();
      if (window.AdReminders) window.AdReminders.refresh();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  (async () => {
    const selected = queryParam("advertiser_id") || "";
    await refreshTokenPill();
    await loadAdvertisers(selected);
    if (selected) await loadPlans();
  })().catch((err) => toast(err.message, "error"));
})();
