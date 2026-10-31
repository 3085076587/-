(() => {
  const { el, toast, api, setBusy, refreshTokenPill } = window.AdHelper;

  const REFUND_EXTRA = 0.05;
  let rowsCache = [];
  let sortKey = "stat_cost";
  let sortAsc = false;
  let accountKeyword = "";
  let pickedId = "";

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

  function fuzzyHit(item, keyword) {
    const q = String(keyword || "").trim().toLowerCase();
    if (!q) return true;
    const hay = `${item.advertiser_name || ""} ${item.advertiser_id || ""}`.toLowerCase();
    if (hay.includes(q)) return true;
    let i = 0;
    for (const ch of hay) {
      if (ch === q[i]) i += 1;
      if (i >= q.length) return true;
    }
    return false;
  }

  function matchedAccounts(keyword) {
    const q = String(keyword || "").trim();
    if (!q) return rowsCache.slice(0, 12);
    return rowsCache.filter((item) => fuzzyHit(item, q)).slice(0, 12);
  }

  function sortedRows() {
    const list = rowsCache
      .filter((item) => {
        if (pickedId) return String(item.advertiser_id) === String(pickedId);
        return fuzzyHit(item, accountKeyword);
      })
      .slice();
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
      const emptyText = rowsCache.length ? "没有匹配的账户" : "暂无子账户，请先同步";
      tbody.innerHTML = `<tr><td colspan="9" class="empty">${emptyText}</td></tr>`;
      paintSortIndicators();
      const count = el("account-count");
      if (count) count.textContent = rowsCache.length ? `显示 0 / 共 ${rowsCache.length} 个` : "共 0 个";
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
          <td>
            <a class="btn" href="/plans?advertiser_id=${item.advertiser_id}">看计划</a>
          </td>
        </tr>`;
      })
      .join("");
    paintSortIndicators();
    const count = el("account-count");
    if (count) {
      count.textContent = list.length === rowsCache.length
        ? `共 ${rowsCache.length} 个`
        : `显示 ${list.length} / 共 ${rowsCache.length} 个`;
    }
  }

  function hideSearchMenu() {
    const menu = el("account-search-menu");
    if (menu) menu.classList.add("hidden");
  }

  function showSearchMenu() {
    const menu = el("account-search-menu");
    const input = el("account-search");
    if (!menu || !input) return;
    const keyword = input.value;
    const matches = matchedAccounts(keyword);
    if (!keyword.trim() || !matches.length) {
      menu.classList.add("hidden");
      menu.innerHTML = "";
      return;
    }
    menu.innerHTML = matches
      .map((item) => {
        const name = item.advertiser_name || "未命名";
        const safe = String(name)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
        return `<button type="button" data-pick-id="${item.advertiser_id}" data-pick-name="${safe}">
          <div class="cell-title">${safe}</div>
          <div class="cell-sub mono">ID: ${item.advertiser_id}</div>
        </button>`;
      })
      .join("");
    menu.classList.remove("hidden");
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
      const src = data.from_cache ? "缓存" : "实时";
      const stale = Number(data.stale_count) || 0;
      const staleText = stale ? ` · ${stale} 个账户本次请求失败，仍显示上次数据` : "";
      const scope = data.viewer_scope ? ` · ${data.viewer_scope}` : "";
      el("dash-hint").textContent = `${src}${scope} · ${data.start_date} ~ ${data.end_date}${staleText}${
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

  el("account-search").addEventListener("input", () => {
    accountKeyword = el("account-search").value;
    pickedId = "";
    showSearchMenu();
    renderRows();
  });

  el("account-search").addEventListener("focus", () => {
    if (el("account-search").value.trim()) showSearchMenu();
  });

  el("account-search-menu").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-pick-id]");
    if (!btn) return;
    pickedId = btn.getAttribute("data-pick-id");
    const name = btn.getAttribute("data-pick-name") || "";
    accountKeyword = name;
    el("account-search").value = `${name} · ${pickedId}`;
    hideSearchMenu();
    renderRows();
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".account-search")) hideSearchMenu();
  });

  el("btn-reset-filter").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    sortKey = null;
    sortAsc = false;
    accountKeyword = "";
    pickedId = "";
    el("account-search").value = "";
    hideSearchMenu();
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
