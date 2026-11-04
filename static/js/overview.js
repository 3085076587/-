/** 数据总览页：KPI + 四张 ECharts 图。消耗来自工作台，利润/产品来自开放平台商品报表（优先读库）。 */
(() => {
  const { el, toast, api, setBusy, refreshTokenPill } = window.AdHelper;

  const COLORS = {
    cost: "#3b82f6",
    profit: "#22c55e",
    productCost: "#f97316",
    productSales: "#3b82f6",
  };
  let summary = null;
  let isAdmin = false;
  let progressTimer = null;
  let querying = false;
  let fillingPeople = false;
  const charts = {};
  const chartRows = {};

  function localISODate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function todayISO() {
    return localISODate(new Date());
  }

  function shiftISO(days) {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return localISODate(d);
  }

  function rangeDates(key) {
    const today = todayISO();
    if (key === "yesterday") {
      const day = shiftISO(-1);
      return { start: day, end: day };
    }
    if (key === "7") return { start: shiftISO(-6), end: today };
    if (key === "30") return { start: shiftISO(-29), end: today };
    return { start: today, end: today };
  }

  function showCustomDates(show) {
    document.querySelectorAll(".ov-custom").forEach((node) => {
      node.classList.toggle("hidden", !show);
    });
  }

  function applyRange(key) {
    showCustomDates(key === "custom");
    if (key === "custom") {
      if (!el("ov-start").value) el("ov-start").value = todayISO();
      if (!el("ov-end").value) el("ov-end").value = todayISO();
      return;
    }
    const span = rangeDates(key);
    el("ov-start").value = span.start;
    el("ov-end").value = span.end;
  }

  function money(v) {
    return Number(v || 0).toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function initCharts() {
    ["chart-cost", "chart-profit", "chart-product-cost", "chart-product-sales"].forEach((id) => {
      charts[id] = echarts.init(el(id));
    });
    window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
    bindCommissionClick("chart-product-cost", "productCost");
    bindCommissionClick("chart-product-sales", "productSales");
  }

  function bindCommissionClick(chartId, rowsKey) {
    charts[chartId].on("click", (params) => {
      const rows = chartRows[rowsKey] || [];
      editCommission(rows[params.dataIndex]);
    });
  }

  async function editCommission(item) {
    if (!item || !item.pid) {
      toast("该行没有商品 ID，无法改佣金", "warn");
      return;
    }
    if (!isAdmin) {
      toast("仅管理员可修改商品佣金", "warn");
      return;
    }
    const val = window.prompt(
      `商品「${item.name}」佣金百分比（未配置时默认 60）：`,
      item.commission || 60
    );
    if (val === null) return;
    const pct = parseFloat(val);
    if (!(pct > 0 && pct <= 100)) {
      toast("请输入 0-100 之间的数字", "warn");
      return;
    }
    try {
      await api("/api/overview/product-commission", {
        method: "POST",
        body: JSON.stringify({ product_id: item.pid, commission_pct: pct }),
      });
      toast("佣金已保存，重新加载中", "success");
      await load(false);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function lineOption(points, valueKey, color) {
    const list = points || [];
    return {
      tooltip: { trigger: "axis", valueFormatter: (v) => "¥" + money(v) },
      grid: { left: 72, right: 24, top: 36, bottom: 48 },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: list.map((it) => String(it.date || "").slice(5)),
        axisLabel: { interval: list.length > 14 ? "auto" : 0 },
      },
      yAxis: { type: "value" },
      series: [
        {
          type: "line",
          smooth: true,
          showSymbol: list.length <= 14,
          data: list.map((it) => Number(it[valueKey] || 0)),
          itemStyle: { color },
          lineStyle: { width: 2, color },
          areaStyle: { color, opacity: 0.12 },
        },
      ],
    };
  }

  function vBarOption(rows, valueKey, color, prefix) {
    const list = rows;
    return {
      tooltip: { trigger: "axis", valueFormatter: (v) => prefix + money(v) },
      grid: { left: 72, right: 20, top: 42, bottom: 56 },
      xAxis: {
        type: "category",
        data: list.map((it) => it.name),
        axisLabel: { interval: 0, rotate: list.length > 7 ? 25 : 0 },
      },
      yAxis: { type: "value" },
      series: [
        {
          type: "bar",
          barMaxWidth: 46,
          data: list.map((it) => Number(it[valueKey] || 0)),
          itemStyle: { color, borderRadius: [4, 4, 0, 0] },
          label: {
            show: true,
            position: "top",
            color,
            formatter: (p) => prefix + money(p.value),
          },
        },
      ],
    };
  }

  function hBarOption(rows, color, prefix, storeAs) {
    const list = rows.slice(0, 20).reverse();
    if (storeAs) chartRows[storeAs] = list;
    return {
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        valueFormatter: (v) => prefix + money(v),
      },
      grid: { left: 150, right: 100, top: 30, bottom: 40 },
      xAxis: { type: "value" },
      yAxis: {
        type: "category",
        data: list.map((it) => it.name),
        axisLabel: { width: 130, overflow: "truncate" },
      },
      series: [
        {
          type: "bar",
          barMaxWidth: 18,
          data: list.map((it) => Number(it.value || 0)),
          itemStyle: { color, borderRadius: [0, 4, 4, 0] },
          label: {
            show: true,
            position: "right",
            color,
            formatter: (p) => prefix + money(p.value),
          },
        },
      ],
    };
  }

  function renderAll() {
    if (!summary) return;
    const k = summary.kpis || {};
    const board = summary.workbench || {};
    el("kpi-cost").textContent = "¥" + money(board.stat_cost != null ? board.stat_cost : k.total_cost);
    el("kpi-profit").textContent = "¥" + money(k.total_profit);
    el("kpi-profit-rate").textContent = board.profit_rate == null ? "-" : `${(Number(board.profit_rate) * 100).toFixed(2)}%`;
    el("kpi-pay").textContent = "¥" + money(board.pay_amount);
    el("kpi-pay-roi").textContent = board.pay_roi == null ? "-" : Number(board.pay_roi).toFixed(2);
    el("kpi-settle").textContent = "¥" + money(board.settle_amount);
    el("kpi-settle-roi").textContent = board.settle_roi == null ? "-" : Number(board.settle_roi).toFixed(2);
    el("kpi-people").textContent = k.people_count ?? 0;
    const trend = summary.trend;
    if (trend && trend.length > 1) {
      const who = summary.trend_person || "";
      el("chart-cost-title").textContent = who ? `${who}消耗趋势` : "消耗趋势";
      el("chart-profit-title").textContent = who ? `${who}利润趋势` : "利润趋势";
      charts["chart-cost"].setOption(lineOption(trend, "cost", COLORS.cost), true);
      charts["chart-profit"].setOption(lineOption(trend, "profit", COLORS.profit), true);
    } else {
      el("chart-cost-title").textContent = "各运营消耗对比";
      el("chart-profit-title").textContent = "各运营利润对比";
      charts["chart-cost"].setOption(vBarOption(summary.people, "cost", COLORS.cost, "¥"), true);
      charts["chart-profit"].setOption(
        vBarOption(summary.people, "profit", COLORS.profit, "¥"),
        true
      );
    }
    charts["chart-product-cost"].setOption(
      hBarOption(summary.product_cost_top, COLORS.productCost, "¥", "productCost"),
      true
    );
    charts["chart-product-sales"].setOption(
      hBarOption(summary.product_sales_top, COLORS.productSales, "", "productSales"),
      true
    );
    const parts = [`${summary.start_date} ~ ${summary.end_date}`, summary.from_cache ? "来自缓存" : "实时拉取"];
    if (summary.cached_at) parts.push(`缓存时间 ${summary.cached_at}`);
    if (summary.errors && summary.errors.length) parts.push(`有 ${summary.errors.length} 个运营拉取失败`);
    if (summary.note) parts.push(summary.note);
    el("ov-meta").textContent = parts.join(" · ");
  }

  function fillPersonOptions() {
    const sel = el("ov-person");
    const current = sel.value;
    fillingPeople = true;
    sel.innerHTML =
      '<option value="">全部</option>' +
      summary.people
        .map(
          (p) => `<option value="${p.name}"${p.name === current ? " selected" : ""}>${p.name}</option>`
        )
        .join("");
    fillingPeople = false;
  }

  function stopProgress() {
    if (progressTimer) {
      clearInterval(progressTimer);
      progressTimer = null;
    }
  }

  function startProgress(key) {
    stopProgress();
    const beganAt = Date.now();
    el("progress-bar").style.width = "0%";
    el("loading-msg").textContent = "0% 等待后端响应…";
    progressTimer = setInterval(async () => {
      const secs = Math.floor((Date.now() - beganAt) / 1000);
      try {
        const p = await api(`/api/overview/progress?key=${encodeURIComponent(key)}`);
        const pct = Math.max(0, Math.min(100, Number(p.percent || 0)));
        el("progress-bar").style.width = pct + "%";
        el("loading-msg").textContent = `${pct}% ${p.message || ""}（已等待 ${secs} 秒）`;
      } catch (_) {
        el("loading-msg").textContent = `等待后端响应…（已等待 ${secs} 秒）`;
      }
    }, 1000);
  }

  async function load(refresh) {
    const start = el("ov-start").value;
    const end = el("ov-end").value;
    if (!start || !end) {
      toast("请选择日期范围", "warn");
      return;
    }
    if (start > end) {
      toast("开始日期不能晚于结束日期", "warn");
      return;
    }
    el("loading-mask").classList.remove("hidden");
    startProgress(`${start}~${end}`);
    try {
      const qs = new URLSearchParams({ start_date: start, end_date: end });
      if (isAdmin && el("ov-person").value) qs.set("person", el("ov-person").value);
      if (refresh) qs.set("refresh", "true");
      summary = await api(`/api/overview/summary?${qs.toString()}`);
      if (isAdmin) fillPersonOptions();
      renderAll();
    } catch (err) {
      const msg = err.message || "加载失败";
      toast(msg, msg.includes("汇总") || msg.includes("稍后再") ? "warn" : "error");
    } finally {
      stopProgress();
      el("loading-mask").classList.add("hidden");
    }
  }

  async function runQuery(button) {
    if (querying) return;
    querying = true;
    setBusy(button, true);
    try {
      await load(false);
    } finally {
      querying = false;
      setBusy(button, false);
    }
  }

  function csvCell(v) {
    const text = String(v == null ? "" : v);
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function exportCsv() {
    if (!summary) {
      toast("还没有数据", "warn");
      return;
    }
    const lines = ["类型,名称,消耗(元),利润(元),销量"];
    summary.people.forEach((p) =>
      lines.push(["运营", p.name, p.cost, p.profit, ""].map(csvCell).join(","))
    );
    (summary.trend || []).forEach((p) =>
      lines.push(["按天", p.date, p.cost, p.profit, ""].map(csvCell).join(","))
    );
    summary.product_cost_top.forEach((p) =>
      lines.push(["产品消耗", p.name, p.value, "", ""].map(csvCell).join(","))
    );
    summary.product_sales_top.forEach((p) =>
      lines.push(["商品销量", p.name, "", "", p.value].map(csvCell).join(","))
    );
    const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `数据总览_${summary.start_date}_${summary.end_date}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function syncDateRange(changed) {
    const s = el("ov-start").value;
    const e = el("ov-end").value;
    if (!s || !e) return;
    if (changed === "start" && s > e) el("ov-end").value = s;
    if (changed === "end" && e < s) el("ov-start").value = e;
  }

  el("btn-query").addEventListener("click", (e) => runQuery(e.currentTarget));
  el("ov-range").addEventListener("change", async () => {
    const key = el("ov-range").value;
    applyRange(key);
    if (key === "custom" || querying) return;
    querying = true;
    try {
      await load(false);
    } finally {
      querying = false;
    }
  });
  el("ov-person").addEventListener("change", () => {
    if (fillingPeople || querying) return;
    runQuery(el("btn-query"));
  });
  // 手改日期切到自定义，不自动查询，点「查询」才发请求。
  function onCustomDate(changed) {
    if (el("ov-range").value !== "custom") {
      el("ov-range").value = "custom";
      showCustomDates(true);
    }
    syncDateRange(changed);
  }
  el("ov-start").addEventListener("change", () => onCustomDate("start"));
  el("ov-end").addEventListener("change", () => onCustomDate("end"));
  el("btn-export").addEventListener("click", exportCsv);

  (async () => {
    const today = todayISO();
    el("ov-end").max = today;
    el("ov-start").max = today;
    el("ov-range").value = "today";
    applyRange("today");
    await refreshTokenPill();
    try {
      const me = await api("/api/auth/me");
      isAdmin = me && me.username === "admin";
    } catch (_) {
      isAdmin = false;
    }
    if (isAdmin) el("person-field").classList.remove("hidden");
    initCharts();
    await load(false);
  })().catch((err) => toast(err.message, "error"));
})();
