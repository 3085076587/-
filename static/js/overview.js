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
  let accountRows = [];
  let accountsReady = false;
  let realByAccount = new Map();
  let focusId = "";
  let sortKey = "stat_cost";
  let sortAsc = false;
  let isAdmin = false;
  let progressTimer = null;
  let querying = false;
  let fillingPeople = false;
  const charts = {};
  const chartRows = {};
  const TREND_STORE = "ad_helper_overview_trend_v2";
  const TREND_DEFAULT = ["real_profit", "real_profit_rate"];
  const TREND_SERIES = [
    { key: "cost", name: "总消耗", axis: "money", color: "#3b82f6" },
    { key: "profit", name: "预估利润", axis: "money", color: "#22c55e" },
    { key: "real_profit", name: "利润", axis: "money", color: "#0f766e" },
    { key: "profit_rate", name: "预估总利润率", axis: "rate", color: "#7c3aed" },
    { key: "real_profit_rate", name: "利润率", axis: "rate", color: "#db2777" },
    { key: "pay_amount", name: "用户实际支付金额", axis: "money", color: "#f97316" },
    { key: "settle_amount", name: "净成交金额", axis: "money", color: "#0d9488" },
  ];

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
    if (key === "month") {
      const now = new Date();
      return { start: localISODate(new Date(now.getFullYear(), now.getMonth(), 1)), end: today };
    }
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

  function fmtRefund(value) {
    if (value == null || value === "") return "-";
    let n = Number(value);
    if (!Number.isFinite(n) || n < 0) return "-";
    if (n > 1 && n <= 100) n /= 100;
    if (n > 1) return "-";
    return `${(n * 100).toFixed(2)}%`;
  }

  function fmtActualRefund(value) {
    if (value == null || value === "" || Number(value) === 0) return "0.00%";
    const text = fmtRefund(value);
    return text === "-" ? "0.00%" : text;
  }

  function money(v) {
    return Number(v || 0).toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function esc(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function ownerOf(name) {
    const match = String(name || "").match(/\(([^)]+)\)/);
    return match ? match[1].trim() : "";
  }

  function profitClass(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n === 0) return "";
    return n < 0 ? "num-loss" : "num-gain";
  }

  function scopedAccounts() {
    const person = isAdmin && el("ov-person") ? el("ov-person").value : "";
    const keyword = (el("ov-search") && el("ov-search").value || "").trim().toLowerCase();
    return accountRows.filter((item) => {
      if (focusId && String(item.advertiser_id) !== String(focusId)) return false;
      if (person && ownerOf(item.advertiser_name) !== person) return false;
      if (!keyword) return true;
      const hay = `${item.advertiser_name || ""} ${item.advertiser_id || ""}`.toLowerCase();
      return hay.includes(keyword);
    });
  }

  function renderAccounts() {
    const body = el("account-tbody");
    const title = el("account-title");
    const clear = el("btn-clear-focus");
    if (!body) return;
    const person = isAdmin && el("ov-person") ? el("ov-person").value : "";
    const counted = accountRows.filter((item) => !person || ownerOf(item.advertiser_name) === person);
    paintRealKpis(counted);
    const countNode = el("kpi-accounts");
    if (countNode) {
      if (accountsReady) {
        countNode.textContent = String(counted.length);
        countNode.classList.remove("kpi-empty");
      } else {
        countNode.textContent = "—";
        countNode.classList.add("kpi-empty");
      }
    }
    if (clear) clear.classList.toggle("hidden", !focusId);
    const list = scopedAccounts().slice().sort((a, b) => {
      const av = Number(a[sortKey]);
      const bv = Number(b[sortKey]);
      const left = Number.isFinite(av) ? av : 0;
      const right = Number.isFinite(bv) ? bv : 0;
      return sortAsc ? left - right : right - left;
    });
    if (title) title.textContent = `账户明细（${list.length} 个账户）`;
    document.querySelectorAll("#account-table th.sortable").forEach((th) => {
      const ind = th.querySelector(".sort-ind");
      if (!ind) return;
      ind.textContent = th.dataset.sort === sortKey ? (sortAsc ? "↑" : "↓") : "";
    });
    if (!list.length) {
      body.innerHTML = `<tr><td colspan="12" class="empty">${accountRows.length ? "没有匹配的账户" : "这段时间还没有账户明细"}</td></tr>`;
      return;
    }
    body.innerHTML = list
      .map((item) => {
        const name = esc(item.advertiser_name || "未命名");
        const id = esc(item.advertiser_id);
        const payRoi = item.pay_roi == null ? "—" : Number(item.pay_roi).toFixed(2);
        const settleRoi = item.settle_roi == null ? "—" : Number(item.settle_roi).toFixed(2);
        const profit = item.profit == null ? "—" : "¥" + money(item.profit);
        const real = item.real_profit == null ? "—" : "¥" + money(item.real_profit);
        return `<tr>
          <td><button type="button" class="name-btn" data-focus="${id}">${name}</button></td>
          <td class="mono">${id}<button type="button" class="copy-btn" data-copy="${id}">复制</button></td>
          <td>¥${money(item.stat_cost)}</td>
          <td>¥${money(item.pay_amount)}</td>
          <td>${payRoi}</td>
          <td>¥${money(item.settle_amount)}</td>
          <td>${settleRoi}</td>
          <td>${fmtRefund(item.refund_rate_1h)}</td>
          <td>${fmtActualRefund(item.actual_refund_rate)}</td>
          <td class="${profitClass(item.profit)}">${profit}</td>
          <td class="${profitClass(item.real_profit)}">${real}</td>
          <td><a class="btn" href="/plans?advertiser_id=${id}">看计划</a></td>
        </tr>`;
      })
      .join("");
  }

  function dashboardQuery(start, end) {
    const today = todayISO();
    const qs = new URLSearchParams();
    if (start === today && end === today) qs.set("range", "today");
    else if (end === today && start === shiftISO(-6)) qs.set("range", "7");
    else if (end === today && start === shiftISO(-29)) qs.set("range", "30");
    else {
      qs.set("start_date", start);
      qs.set("end_date", end);
    }
    return qs;
  }

  function parsePlanBody(text) {
    const fixed = String(text || "")
      .replace(/"product_id"\s*:\s*(\d{15,})/g, '"product_id":"$1"')
      .replace(/"product_ids"\s*:\s*\[([^\]]*)\]/g, (_, inner) => `"product_ids":[${inner.replace(/(\d{15,})/g, '"$1"')}]`);
    const body = JSON.parse(fixed);
    if (body.code === 40100) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = `/login?next=${next}`;
      throw new Error(body.msg || "未登录");
    }
    if (body.code !== 0) throw new Error(body.msg || "请求失败");
    return body.data;
  }

  function planMoney(plan) {
    const pct = Number(plan && plan.commission_pct);
    const cost = Number(plan && plan.stat_cost);
    const settle = Number(plan && plan.settle_amount);
    if (!Number.isFinite(pct) || pct <= 0 || !Number.isFinite(cost) || !Number.isFinite(settle)) return null;
    return { pct, cost, settle };
  }

  function planEstimateProfit(plan) {
    const money = planMoney(plan);
    if (!money) return null;
    return money.settle * (1 - 0.15) * (money.pct / 100) - money.cost;
  }

  function planRealProfit(plan, rates) {
    const money = planMoney(plan);
    const id = String((plan && plan.product_id) || "").trim();
    if (!money || !id || !rates.has(id)) return null;
    const rate = rates.get(id);
    if (rate == null || !Number.isFinite(rate) || rate < 0 || rate > 100) return null;
    return money.settle * (1 - rate / 100) * (money.pct / 100) - money.cost;
  }

  function addProfit(sums, advertiserId, profit) {
    if (profit == null || !advertiserId) return;
    sums.set(advertiserId, Math.round(((sums.get(advertiserId) || 0) + profit) * 100) / 100);
  }

  async function loadRealProfits(start, end) {
    const qs = new URLSearchParams({ start_date: start, end_date: end, refresh: "0" });
    const [resp, rows] = await Promise.all([
      fetch(`/api/plan/uni/board?${qs.toString()}`),
      api("/api/product/refund-rates"),
    ]);
    const data = parsePlanBody(await resp.text());
    const rates = new Map();
    (Array.isArray(rows) ? rows : []).forEach((row) => {
      const id = String((row && row.product_id) || "").trim();
      if (!id) return;
      const rate = row.refund_rate;
      rates.set(id, rate == null || rate === "" ? null : Number(rate));
    });
    const real = new Map();
    const estimate = new Map();
    (data.plans || []).forEach((plan) => {
      const adv = String(plan.advertiser_id || "");
      addProfit(estimate, adv, planEstimateProfit(plan));
      addProfit(real, adv, planRealProfit(plan, rates));
    });
    return { real, estimate };
  }

  let trendToken = 0;
  const dayRealCache = new Map();

  function scopedAdvertiserIds() {
    const person = isAdmin && el("ov-person") ? el("ov-person").value : "";
    return new Set(
      accountRows
        .filter((item) => !person || ownerOf(item.advertiser_name) === person)
        .map((item) => String(item.advertiser_id || ""))
    );
  }

  function stampTrendPoint(point, profit, count) {
    if (!count) {
      point.real_profit = null;
      point.real_profit_rate = null;
      return;
    }
    const rounded = Math.round(Number(profit) * 100) / 100;
    point.real_profit = rounded;
    const pay = Number(point.pay_amount);
    point.real_profit_rate = pay > 0 ? rounded / pay : null;
  }

  function stampTrendFromAccounts() {
    const rows = (summary && summary.daily_trend) || [];
    if (rows.length !== 1) return false;
    const ids = scopedAdvertiserIds();
    let profit = 0;
    let count = 0;
    ids.forEach((id) => {
      const value = realByAccount.get(id);
      if (value == null || !Number.isFinite(Number(value))) return;
      count += 1;
      profit += Number(value);
    });
    stampTrendPoint(rows[0], profit, count);
    return true;
  }

  async function dayRealProfit(day, rates, ids) {
    const cacheKey = `${isAdmin && el("ov-person") ? el("ov-person").value : ""}|${day}`;
    if (dayRealCache.has(cacheKey)) return dayRealCache.get(cacheKey);
    const qs = new URLSearchParams({ start_date: day, end_date: day, refresh: "0" });
    const data = parsePlanBody(await (await fetch(`/api/plan/uni/board?${qs.toString()}`)).text());
    let profit = 0;
    let count = 0;
    (data.plans || []).forEach((plan) => {
      if (!ids.has(String(plan.advertiser_id || ""))) return;
      const one = planRealProfit(plan, rates);
      if (one == null) return;
      count += 1;
      profit += one;
    });
    const value = { profit, count };
    dayRealCache.set(cacheKey, value);
    return value;
  }

  async function fillDailyReal() {
    const token = ++trendToken;
    const rows = (summary && summary.daily_trend) || [];
    if (!rows.length) return;
    if (stampTrendFromAccounts()) {
      renderDailyTrend();
      return;
    }
    let rates;
    try {
      rates = await refundRateMap();
    } catch (_) {
      return;
    }
    if (token !== trendToken) return;
    const ids = scopedAdvertiserIds();
    const queue = rows.slice();
    async function worker() {
      while (queue.length && token === trendToken) {
        const point = queue.shift();
        try {
          const found = await dayRealProfit(point.date, rates, ids);
          if (token !== trendToken) return;
          stampTrendPoint(point, found.profit, found.count);
        } catch (_) {
          if (token !== trendToken) return;
          stampTrendPoint(point, 0, 0);
        }
        renderDailyTrend();
      }
    }
    await Promise.all(Array.from({ length: Math.min(4, rows.length) }, () => worker()));
  }

  async function refundRateMap() {
    const rows = await api("/api/product/refund-rates");
    const rates = new Map();
    (Array.isArray(rows) ? rows : []).forEach((row) => {
      const id = String((row && row.product_id) || "").trim();
      if (!id) return;
      const rate = row.refund_rate;
      rates.set(id, rate == null || rate === "" ? null : Number(rate));
    });
    return rates;
  }

  function applyEstimateOverlay(estimates) {
    if (!summary || !estimates || !estimates.size) return;
    const person = isAdmin && el("ov-person") ? el("ov-person").value : "";
    let delta = 0;
    const byPerson = new Map();
    accountRows.forEach((item) => {
      const who = ownerOf(item.advertiser_name) || "未分组";
      if (person && who !== person) return;
      const current = Number(item.profit);
      if (item.profit == null || !Number.isFinite(current) || current !== 0) return;
      const estimate = estimates.get(String(item.advertiser_id || ""));
      if (estimate == null || !Number.isFinite(Number(estimate)) || Number(estimate) === 0) return;
      item.profit = Math.round(Number(estimate) * 100) / 100;
      delta += item.profit;
      byPerson.set(who, (byPerson.get(who) || 0) + item.profit);
    });
    if (!delta) return;
    const kpis = summary.kpis || (summary.kpis = {});
    kpis.total_profit = Math.round(((Number(kpis.total_profit) || 0) + delta) * 100) / 100;
    const board = summary.workbench || {};
    const pay = Number(board.pay_amount);
    if (pay > 0) board.profit_rate = kpis.total_profit / pay;
    (summary.people || []).forEach((row) => {
      const extra = byPerson.get(row.name);
      if (!extra) return;
      row.profit = Math.round((Number(row.profit) + extra) * 100) / 100;
    });
    const trend = summary.daily_trend || [];
    if (trend.length === 1) {
      trend[0].profit = Math.round(((Number(trend[0].profit) || 0) + delta) * 100) / 100;
      const dayPay = Number(trend[0].pay_amount);
      trend[0].profit_rate = dayPay > 0 ? trend[0].profit / dayPay : null;
    }
  }

  function paintRealKpis(rows) {
    const profitNode = el("kpi-real-profit");
    const rateNode = el("kpi-real-rate");
    if (!profitNode) return;
    let profit = 0;
    let pay = 0;
    let count = 0;
    rows.forEach((item) => {
      const paid = Number(item.pay_amount);
      if (Number.isFinite(paid)) pay += paid;
      const value = item.real_profit;
      if (value == null || !Number.isFinite(Number(value))) return;
      count += 1;
      profit += Number(value);
    });
    profitNode.classList.remove("num-gain", "num-loss");
    if (!count) {
      profitNode.textContent = "-";
      if (rateNode) rateNode.textContent = "-";
      return;
    }
    profitNode.textContent = "¥" + money(profit);
    profitNode.classList.add(profit < 0 ? "num-loss" : "num-gain");
    if (rateNode) rateNode.textContent = pay > 0 ? `${((profit / pay) * 100).toFixed(2)}%` : "-";
  }

  function loadTrendPicks() {
    const raw = localStorage.getItem(TREND_STORE);
    if (raw == null) return TREND_DEFAULT.slice();
    try {
      const data = JSON.parse(raw);
      if (!Array.isArray(data)) return TREND_DEFAULT.slice();
      const allow = new Set(TREND_SERIES.map((item) => item.key));
      return data.filter((key) => allow.has(key));
    } catch (_) {
      return TREND_DEFAULT.slice();
    }
  }

  function initTrendPicks() {
    const box = el("trend-picks");
    const picked = new Set(loadTrendPicks());
    box.innerHTML = TREND_SERIES.map(
      (item) =>
        `<label><input type="checkbox" data-trend="${item.key}"${picked.has(item.key) ? " checked" : ""}>${item.name}</label>`
    ).join("");
    box.addEventListener("change", () => {
      const keys = [...box.querySelectorAll("input:checked")].map((node) => node.dataset.trend);
      localStorage.setItem(TREND_STORE, JSON.stringify(keys));
      const rows = (summary && summary.daily_trend) || [];
      const need = keys.includes("real_profit") || keys.includes("real_profit_rate");
      const missing = need && rows.some((row) => row.real_profit === undefined);
      if (missing) fillDailyReal();
      else renderDailyTrend();
    });
  }

  function trendTip(value, axis) {
    if (value == null || value === "") return "-";
    if (axis === "rate") return `${(Number(value) * 100).toFixed(2)}%`;
    return "¥" + money(value);
  }

  function renderDailyTrend() {
    const chart = charts["chart-daily"];
    if (!chart) return;
    const rows = (summary && summary.daily_trend) || [];
    const picked = new Set(loadTrendPicks());
    const selected = TREND_SERIES.filter((item) => picked.has(item.key));
    const hasMoney = selected.some((item) => item.axis === "money");
    const hasRate = selected.some((item) => item.axis === "rate");
    const yAxis = [];
    let moneyIndex = 0;
    let rateIndex = 0;
    if (hasMoney || !hasRate) {
      yAxis.push({
        type: "value",
        name: "元",
        axisLabel: { formatter: (value) => money(value) },
      });
    }
    if (hasRate) {
      rateIndex = yAxis.length;
      if (!hasMoney) moneyIndex = rateIndex;
      yAxis.push({
        type: "value",
        name: selected.some((item) => item.key === "real_profit_rate") ? "利润率" : "预估利润率",
        axisLabel: { formatter: (value) => `${(Number(value) * 100).toFixed(0)}%` },
        splitLine: { show: !hasMoney },
      });
    }
    const series = selected.map((item) => ({
      name: item.name,
      type: "line",
      smooth: true,
      showSymbol: rows.length <= 14,
      connectNulls: false,
      yAxisIndex: item.axis === "rate" ? rateIndex : moneyIndex,
      data: rows.map((row) => (row[item.key] == null ? null : Number(row[item.key]))),
      itemStyle: { color: item.color },
      lineStyle: { width: 2, color: item.color },
    }));
    chart.setOption(
      {
        tooltip: {
          trigger: "axis",
          formatter(params) {
            const list = Array.isArray(params) ? params : [params];
            if (!list.length) return "";
            const index = list[0].dataIndex;
            const head = (rows[index] && rows[index].date) || list[0].axisValue;
            const lines = list.map((point) => {
              const spec = selected[point.seriesIndex] || { axis: "money" };
              return `${point.marker}${point.seriesName} ${trendTip(point.value, spec.axis)}`;
            });
            return [head, ...lines].join("<br>");
          },
        },
        legend: { top: 0, selectedMode: false, data: selected.map((item) => item.name) },
        grid: { left: 72, right: hasMoney && hasRate ? 64 : 24, top: 36, bottom: 40 },
        xAxis: {
          type: "category",
          data: rows.map((row) => String(row.date || "").slice(5)),
        },
        yAxis,
        series: series.length ? series : [{ type: "line", data: [] }],
      },
      true
    );
  }

  function initCharts() {
    ["chart-daily", "chart-cost", "chart-profit", "chart-product-cost", "chart-product-sales"].forEach((id) => {
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
      `商品「${item.name}」佣金百分比：`,
      item.commission || ""
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
    const profitNode = el("kpi-profit");
    profitNode.textContent = "¥" + money(k.total_profit);
    profitNode.classList.remove("num-gain", "num-loss");
    profitNode.classList.add(Number(k.total_profit) < 0 ? "num-loss" : "num-gain");
    el("kpi-profit-rate").textContent = board.profit_rate == null ? "-" : `${(Number(board.profit_rate) * 100).toFixed(2)}%`;
    el("kpi-pay").textContent = "¥" + money(board.pay_amount);
    el("kpi-pay-roi").textContent = board.pay_roi == null ? "-" : Number(board.pay_roi).toFixed(2);
    el("kpi-settle").textContent = "¥" + money(board.settle_amount);
    el("kpi-settle-roi").textContent = board.settle_roi == null ? "-" : Number(board.settle_roi).toFixed(2);
    el("kpi-refund").textContent = fmtRefund(board.refund_rate_1h);
    el("kpi-people").textContent = k.people_count ?? 0;
    renderAccounts();
    renderDailyTrend();
    const trend = summary.trend;
    if (trend && trend.length > 1) {
      const who = summary.trend_person || "";
      el("chart-cost-title").textContent = who ? `${who}消耗趋势` : "消耗趋势";
      el("chart-profit-title").textContent = who ? `${who}预估利润趋势` : "预估利润趋势";
      charts["chart-cost"].setOption(lineOption(trend, "cost", COLORS.cost), true);
      charts["chart-profit"].setOption(lineOption(trend, "profit", COLORS.profit), true);
    } else {
      el("chart-cost-title").textContent = "各运营消耗对比";
      el("chart-profit-title").textContent = "各运营预估利润对比";
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
    const names = new Set();
    accountRows.forEach((item) => {
      const who = ownerOf(item.advertiser_name);
      if (who) names.add(who);
    });
    (summary.people || []).forEach((item) => {
      if (item.name) names.add(item.name);
    });
    const ordered = [...names].sort((a, b) => a.localeCompare(b, "zh"));
    fillingPeople = true;
    sel.innerHTML =
      '<option value="">全部</option>' +
      ordered
        .map((name) => `<option value="${esc(name)}"${name === current ? " selected" : ""}>${esc(name)}</option>`)
        .join("");
    if (current && [...sel.options].some((opt) => opt.value === current)) sel.value = current;
    fillingPeople = false;
    renderPersonChips();
  }

  function renderPersonChips() {
    const box = el("person-chips");
    const sel = el("ov-person");
    if (!box || !sel) return;
    const current = sel.value;
    box.innerHTML = [...sel.options]
      .filter((opt) => opt.value)
      .map((opt) => {
        const name = opt.value
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
        const count = accountRows.filter((item) => ownerOf(item.advertiser_name) === opt.value).length;
        const suffix = count ? ` (${count})` : "";
        return `<button type="button" class="chip${opt.value === current ? " active" : ""}" data-person="${name}">${name}${suffix}</button>`;
      })
      .join("");
    const all = document.querySelector('#person-field .chip[data-person=""]');
    if (all) all.classList.toggle("active", !current);
  }

  function syncRangePills() {
    const key = el("ov-range").value;
    document.querySelectorAll("#range-pills .pill[data-range]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.range === key);
    });
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
    const maskTimer = setTimeout(() => {
      el("loading-mask").classList.remove("hidden");
    }, 800);
    startProgress(`${start}~${end}`);
    try {
      const qs = new URLSearchParams({ start_date: start, end_date: end });
      if (isAdmin && el("ov-person").value) qs.set("person", el("ov-person").value);
      if (refresh) qs.set("refresh", "true");
      const boardTask = api(`/api/advertiser/dashboard?${dashboardQuery(start, end).toString()}`).catch(() => null);
      const isToday = start === todayISO() && end === todayISO();
      const realTask = isToday
        ? loadRealProfits(start, end).catch(() => ({ real: new Map(), estimate: new Map() }))
        : Promise.resolve({ real: new Map(), estimate: new Map() });
      summary = await api(`/api/overview/summary?${qs.toString()}`);
      const board = await boardTask;
      const profits = await realTask;
      realByAccount = profits.real || new Map();
      accountsReady = !!(board && Array.isArray(board.accounts));
      accountRows = accountsReady ? board.accounts : [];
      accountRows.forEach((item) => {
        if (!isToday) return;
        const value = realByAccount.get(String(item.advertiser_id || ""));
        item.real_profit = value == null ? null : value;
      });
      applyEstimateOverlay(profits.estimate || new Map());
      focusId = "";
      if (isAdmin) fillPersonOptions();
      renderAll();
      if (isToday) fillDailyReal();
    } catch (err) {
      const msg = err.message || "加载失败";
      toast(msg, msg.includes("汇总") || msg.includes("稍后再") ? "warn" : "error");
    } finally {
      clearTimeout(maskTimer);
      stopProgress();
      el("loading-mask").classList.add("hidden");
    }
  }

  async function runQuery(button, refresh) {
    if (querying) return;
    querying = true;
    setBusy(button, true);
    try {
      await load(!!refresh);
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
    const lines = ["类型,名称,消耗(元),预估利润(元),销量"];
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

  el("btn-query").addEventListener("click", (e) => runQuery(e.currentTarget, true));
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
    syncRangePills();
  }
  document.querySelectorAll("#range-pills .pill[data-range]").forEach((btn) => {
    btn.addEventListener("click", () => {
      el("ov-range").value = btn.dataset.range;
      el("ov-range").dispatchEvent(new Event("change"));
    });
  });
  el("person-field").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip || fillingPeople) return;
    el("ov-person").value = chip.dataset.person || "";
    renderPersonChips();
    el("ov-person").dispatchEvent(new Event("change"));
  });
  el("ov-start").addEventListener("change", () => onCustomDate("start"));
  el("ov-end").addEventListener("change", () => onCustomDate("end"));
  el("btn-export").addEventListener("click", exportCsv);
  el("ov-search").addEventListener("input", () => renderAccounts());
  el("btn-clear-focus").addEventListener("click", () => {
    focusId = "";
    renderAccounts();
  });
  el("account-table").addEventListener("click", (event) => {
    const sort = event.target.closest("th.sortable");
    if (sort) {
      const key = sort.dataset.sort;
      if (sortKey === key) sortAsc = !sortAsc;
      else {
        sortKey = key;
        sortAsc = false;
      }
      renderAccounts();
      return;
    }
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const text = copy.dataset.copy || "";
      if (navigator.clipboard && text) navigator.clipboard.writeText(text).then(() => toast("已复制账户 ID")).catch(() => {});
      return;
    }
    const focus = event.target.closest("[data-focus]");
    if (!focus) return;
    focusId = focus.dataset.focus || "";
    renderAccounts();
  });

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
    initTrendPicks();
    await load(false);
  })().catch((err) => toast(err.message, "error"));
})();
