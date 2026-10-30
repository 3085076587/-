(() => {
  const { el, toast, api, setBusy, refreshTokenPill, advertiserOptions, queryParam } =
    window.AdHelper;
  const COMMISSION_KEY = "ad_helper_commission_pct";
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

  function fmtRoi(v) {
    if (v == null || v === "") return "-";
    return Number(v).toFixed(2);
  }

  function commissionRate() {
    const pct = Number(el("plan-commission").value);
    if (!Number.isFinite(pct) || pct < 0) return null;
    return pct / 100;
  }

  function planMoney(item) {
    const cost = Number(item.stat_cost) || 0;
    const pay = Number(item.pay_amount) || 0;
    const settle = Number(item.settle_amount) || 0;
    const roiRaw = item.pay_roi;
    const roi = roiRaw == null || roiRaw === "" ? null : Number(roiRaw);
    const gmv = roi != null && Number.isFinite(roi) && roi > 0 && cost > 0 ? cost * roi : pay;
    const base = gmv > 0 ? gmv : pay;
    let rate = 0;
    if (base > 0) rate = Math.min(1, Math.max(0, (base - settle) / base));
    return { cost, gmv: base, rate };
  }

  function planProfit(item) {
    const commission = commissionRate();
    if (commission == null) return null;
    const { cost, gmv, rate } = planMoney(item);
    return gmv * (1 - rate) * commission - cost;
  }

  function moneyCell(value) {
    if (value == null || value === "") return `<td class="col-num">-</td>`;
    const n = Number(value);
    const cls = Number.isFinite(n) && n < 0 ? "col-num num-neg" : "col-num";
    return `<td class="${cls}">${fmtMoney(n)}</td>`;
  }

  function paintHint() {
    if (!listMeta || !el("plan-hint")) return;
    const profitSum = plansCache.reduce((sum, item) => {
      const p = planProfit(item);
      return p == null ? sum : sum + p;
    }, 0);
    const profitText =
      commissionRate() == null ? "填写佣金比例后计算利润" : `利润合计 ${fmtMoney(profitSum)}`;
    el("plan-hint").textContent = `已加载 ${listMeta.total} 条 · ${listMeta.start} ~ ${listMeta.end} · ${profitText}`;
  }

  function renderPlans(list) {
    const tbody = el("plan-tbody");
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">暂无计划</td></tr>';
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
          <td class="col-status">${item.status_label || "-"}</td>
          <td class="col-budget">
            <div>预算 ${item.budget != null ? item.budget : "-"}</div>
            <div>ROI目标 <b>${fmtRoi(item.roi2_goal)}</b></div>
          </td>
          ${moneyCell(item.stat_cost)}
          ${moneyCell(item.settle_amount)}
          <td class="col-num">${fmtRoi(item.pay_roi)}</td>
          ${moneyCell(profit)}
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
    } catch (err) {
      el("plan-tbody").innerHTML = `<tr><td colspan="10" class="empty">${err.message || "拉取失败"}</td></tr>`;
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

  const savedCommission = localStorage.getItem(COMMISSION_KEY);
  if (savedCommission != null) el("plan-commission").value = savedCommission;
  el("plan-commission").addEventListener("input", () => {
    localStorage.setItem(COMMISSION_KEY, el("plan-commission").value);
    if (plansCache.length) {
      renderPlans(plansCache);
      paintHint();
    }
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
