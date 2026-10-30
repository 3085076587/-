(() => {
  const { el, toast, api, setBusy, refreshTokenPill } = window.AdHelper;

  const COMMISSION_KEY = "ad_helper_commission_pct";
  let rowsCache = [];
  let sortKey = "stat_cost";
  let sortAsc = false;

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

  function fmtRate(v) {
    if (v == null || v === "") return "-";
    let n = Number(v);
    if (!Number.isFinite(n) || n < 0) return "-";
    if (n > 1 && n <= 100) n /= 100;
    if (n > 1) return "-";
    return `${(n * 100).toFixed(2)}%`;
  }

  function commissionRate() {
    const input = el("dash-commission");
    let raw = input ? String(input.value).trim() : "";
    if (!raw) {
      raw = (localStorage.getItem(COMMISSION_KEY) || "").trim();
    }
    if (!raw) raw = "20";
    const pct = Number(raw);
    if (!Number.isFinite(pct) || pct < 0) return 0.2;
    if (input && input.value !== raw) input.value = raw;
    return pct / 100;
  }

  function accountProfit(item) {
    const commission = commissionRate();
    if (commission == null) return null;
    const cost = Number(item.stat_cost) || 0;
    const pay = Number(item.pay_amount) || 0;
    const roiRaw = item.pay_roi;
    const roi = roiRaw == null || roiRaw === "" ? null : Number(roiRaw);
    const gmv = roi != null && Number.isFinite(roi) && roi > 0 && cost > 0 ? cost * roi : pay;
    let refund = Number(item.refund_rate_1h);
    if (!Number.isFinite(refund) || refund < 0) refund = 0;
    if (refund > 1 && refund <= 100) refund /= 100;
    if (refund > 1) refund = 1;
    return gmv * (1 - refund) * commission - cost;
  }

  function paintAvgProfit() {
    const node = el("avg-plan-profit");
    if (!node) return;
    if (commissionRate() == null) {
      node.textContent = "利润合计 -";
      return;
    }
    const total = rowsCache.reduce((sum, item) => sum + (accountProfit(item) || 0), 0);
    node.textContent = rowsCache.length ? `利润合计 ${fmtMoney(total)}` : "利润合计 -";
  }

  function localISODate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function syncCustomVisibility() {
    const custom = el("dash-range").value === "custom";
    document.querySelectorAll(".custom-dates").forEach((n) => {
      n.classList.toggle("hidden", !custom);
    });
  }

  function currentQuery(refresh) {
    const key = el("dash-range").value || "today";
    if (key === "custom") {
      const start = el("dash-start").value;
      const end = el("dash-end").value;
      return {
        key,
        label: "自定义",
        qs: new URLSearchParams({ start_date: start, end_date: end }),
        start,
        end,
      };
    }
    const end = new Date();
    const start = new Date();
    if (key === "7") start.setDate(end.getDate() - 6);
    if (key === "30") start.setDate(end.getDate() - 29);
    const qs = new URLSearchParams({ range: key });
    if (refresh) qs.set("refresh", "true");
    return {
      key,
      label: key === "7" ? "近7天" : key === "30" ? "近30天" : "今天",
      qs,
      start: localISODate(start),
      end: localISODate(end),
    };
  }

  function showLoading(show, title) {
    el("loading-mask").classList.toggle("hidden", !show);
    if (title) el("loading-title").textContent = title;
    if (!show) {
      el("progress-bar").style.width = "0%";
      el("loading-msg").textContent = "0%";
    }
  }

  function setProgress(percent, msg) {
    const p = Math.max(0, Math.min(100, Number(percent) || 0));
    el("progress-bar").style.width = `${p}%`;
    el("loading-msg").textContent = msg || `${p}%`;
  }

  async function pollProgress(rangeKey, stopFlag) {
    while (!stopFlag.done) {
      try {
        const p = await api(
          `/api/advertiser/dashboard/progress?range=${encodeURIComponent(rangeKey)}`
        );
        setProgress(p.percent || 0, p.message || `${p.percent || 0}%`);
        if (p.status === "done") break;
      } catch (_) {}
      await new Promise((r) => setTimeout(r, 600));
    }
  }

  function renderOverview(ov) {
    const data = ov || {};
    el("ov-cost").textContent = fmtMoney(data.stat_cost);
    el("ov-pay").textContent = fmtMoney(data.pay_amount);
    el("ov-roi").textContent = fmtRoi(data.pay_roi);
    el("ov-settle").textContent = fmtMoney(data.settle_amount);
    el("ov-settle-roi").textContent = fmtRoi(data.settle_roi);
  }

  function sortedRows() {
    const list = rowsCache.map((item) => ({
      ...item,
      avg_plan_profit: accountProfit(item),
    }));
    if (!sortKey) return list;
    list.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      const an = av == null || av === "" ? -Infinity : Number(av);
      const bn = bv == null || bv === "" ? -Infinity : Number(bv);
      if (an === bn) return 0;
      return sortAsc ? an - bn : bn - an;
    });
    return list;
  }

  function paintSortIndicators() {
    document.querySelectorAll("#adv-table th.sortable").forEach((th) => {
      const ind = th.querySelector(".sort-ind");
      if (!ind) return;
      if (th.dataset.sort === sortKey) ind.textContent = sortAsc ? "↑" : "↓";
      else ind.textContent = "";
    });
  }

  function renderRows() {
    const list = sortedRows();
    const tbody = el("advertiser-tbody");
    if (!list.length) {
      tbody.innerHTML =
        '<tr><td colspan="10" class="empty">暂无子账户，请先同步</td></tr>';
      paintSortIndicators();
      return;
    }
    tbody.innerHTML = list
      .map((item) => {
        const name = item.advertiser_name || "未命名";
        return `<tr>
          <td>
            <div class="cell-title">${name}</div>
            <div class="cell-sub mono">ID: ${item.advertiser_id}</div>
          </td>
          <td><span class="tag-active">${item.status || "-"}</span></td>
          <td>${fmtMoney(item.stat_cost)}</td>
          <td>${fmtRoi(item.pay_roi)}</td>
          <td>${fmtMoney(item.pay_amount)}</td>
          <td>${fmtMoney(item.settle_amount)}</td>
          <td>${fmtRoi(item.settle_roi)}</td>
          <td>${fmtRate(item.refund_rate_1h)}</td>
          <td>${fmtMoney(item.avg_plan_profit)}</td>
          <td>
            <a class="btn" href="/plans?advertiser_id=${item.advertiser_id}">看计划</a>
          </td>
        </tr>`;
      })
      .join("");
    paintSortIndicators();
    paintAvgProfit();
  }

  async function loadDashboard({ refresh = false } = {}) {
    const q = currentQuery(refresh);
    if (q.key === "custom" && (!q.start || !q.end)) {
      toast("请选择自定义开始/结束日期", "warn");
      return;
    }
    el("dash-range-text").textContent = `${q.start} ~ ${q.end}`;
    const isPreset = q.key !== "custom";
    const stopFlag = { done: false };
    showLoading(true, refresh || !isPreset ? "正在拉取实时数据…" : "正在加载…");
    setProgress(refresh ? 1 : 8, refresh ? "清除缓存并重拉…" : "读取中…");

    // 预设区间：无论进页跟预热还是点刷新，都轮询共用进度
    let pollTask = null;
    if (isPreset) {
      pollTask = pollProgress(q.key, stopFlag);
    }

    try {
      const data = await api(`/api/advertiser/dashboard?${q.qs.toString()}`);
      stopFlag.done = true;
      setProgress(100, "完成");
      rowsCache = data.accounts || [];
      renderOverview(data.overview);
      renderRows();
      el("account-count").textContent = `共 ${data.total || 0} 个`;
      const src = data.from_cache ? "缓存" : "实时";
      el("dash-hint").textContent = `${src} · ${data.start_date} ~ ${data.end_date}${
        data.cached_at ? ` · 缓存于 ${data.cached_at}` : ""
      }`;
    } finally {
      stopFlag.done = true;
      if (pollTask) await pollTask.catch(() => {});
      setTimeout(() => showLoading(false), 280);
    }
  }

  el("dash-range").addEventListener("change", async () => {
    syncCustomVisibility();
    if (el("dash-range").value === "custom") {
      const today = localISODate(new Date());
      if (!el("dash-start").value) el("dash-start").value = today;
      if (!el("dash-end").value) el("dash-end").value = today;
      return;
    }
    try {
      await loadDashboard({ refresh: false });
    } catch (err) {
      toast(err.message, "error");
    }
  });

  el("btn-load-dash").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    setBusy(btn, true);
    try {
      const refresh = el("dash-range").value !== "custom";
      await loadDashboard({ refresh });
      toast(refresh ? "已刷新并重建缓存" : "自定义区间已拉取");
    } catch (err) {
      toast(err.message, "error");
      el("dash-hint").textContent = err.message || "拉取失败";
      showLoading(false);
    } finally {
      setBusy(btn, false);
    }
  });

  el("btn-sync").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    setBusy(btn, true);
    try {
      const data = await api("/api/advertiser/sync-all");
      toast(`同步完成：新增 ${data.created}，更新 ${data.updated}`);
      await loadDashboard({ refresh: true });
      await refreshTokenPill();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  const savedCommission = (localStorage.getItem(COMMISSION_KEY) || "").trim();
  if (savedCommission && Number(savedCommission) >= 0) {
    el("dash-commission").value = savedCommission;
  } else if (!el("dash-commission").value) {
    el("dash-commission").value = "20";
  }
  el("dash-commission").addEventListener("input", () => {
    localStorage.setItem(COMMISSION_KEY, el("dash-commission").value);
    if (rowsCache.length) renderRows();
  });

  el("btn-reset-filter").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    sortKey = null;
    sortAsc = false;
    paintSortIndicators();
    el("dash-range").value = "today";
    const today = localISODate(new Date());
    el("dash-start").value = today;
    el("dash-end").value = today;
    syncCustomVisibility();
    setBusy(btn, true);
    try {
      await loadDashboard({ refresh: false });
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  document.querySelectorAll("#adv-table th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortKey === key) sortAsc = !sortAsc;
      else {
        sortKey = key;
        sortAsc = false;
      }
      renderRows();
    });
  });

  (async () => {
    const today = localISODate(new Date());
    el("dash-start").value = today;
    el("dash-end").value = today;
    syncCustomVisibility();
    await refreshTokenPill();
    await loadDashboard({ refresh: false });
  })().catch((err) => toast(err.message, "error"));
})();
