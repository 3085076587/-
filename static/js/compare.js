/** 两个授权账号的数据总览卡片，以及一块合计。 */
(() => {
  const { el, api, toast, setBusy } = window.AdHelper;
  const FIELDS = [
    { key: "stat_cost", label: "总消耗", money: true, cls: "ov-purple" },
    { key: "total_profit", label: "预估总利润", money: true, cls: "ov-purple" },
    { key: "profit_rate", label: "预估总利润率", rate: true },
    { key: "pay_amount", label: "用户实际支付金额", money: true },
    { key: "pay_roi", label: "整体支付 ROI", roi: true },
    { key: "settle_amount", label: "净成交金额", money: true },
    { key: "settle_roi", label: "净成交 ROI", roi: true },
    { key: "refund_rate_1h", label: "1小时内退款率", refund: true },
    { key: "people_count", label: "运营数", count: true, cls: "ov-blue" },
  ];

  function todayISO() {
    const d = new Date();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${m}-${day}`;
  }

  function shiftISO(days) {
    const d = new Date();
    d.setDate(d.getDate() + days);
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${m}-${day}`;
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

  function fmtRefund(value) {
    if (value == null || value === "") return "-";
    let n = Number(value);
    if (!Number.isFinite(n) || n < 0) return "-";
    if (n > 1 && n <= 100) n /= 100;
    if (n > 1) return "-";
    return `${(n * 100).toFixed(2)}%`;
  }

  function fmtValue(field, board) {
    if (!board) return "-";
    const value = board[field.key];
    if (field.money) return "¥" + money(value);
    if (field.rate) return value == null ? "-" : `${(Number(value) * 100).toFixed(2)}%`;
    if (field.roi) return value == null ? "-" : Number(value).toFixed(2);
    if (field.refund) return fmtRefund(value);
    if (field.count) return String(value == null ? 0 : value);
    return "-";
  }

  function esc(text) {
    return String(text || "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function cards(board) {
    return FIELDS.map(
      (field) =>
        `<div class="stat-card"><div class="stat-num ${field.cls || ""}">${fmtValue(field, board)}</div><div class="stat-label">${field.label}</div></div>`
    ).join("");
  }

  function section(title, board, extraClass, meta) {
    return `
      <div class="panel ${extraClass || ""}">
        <div class="panel-title">${esc(title)}</div>
        ${meta ? `<p class="hint">${esc(meta)}</p>` : ""}
        <div class="stats">${cards(board)}</div>
      </div>`;
  }

  function accountMeta(item) {
    if (item.error) return item.error;
    const parts = [];
    if (item.start_date && item.end_date) parts.push(`${item.start_date} ~ ${item.end_date}`);
    parts.push(item.from_cache ? "来自缓存" : "实时拉取");
    if (item.cached_at) parts.push(`缓存时间 ${item.cached_at}`);
    if (item.note) parts.push(item.note);
    return parts.join(" · ");
  }

  function render(data) {
    const accounts = (data && data.accounts) || [];
    const box = el("compare-boards");
    const blocks = [
      section("合计", data.total || {}, "compare-total", "两个账号里已经读出来的数相加"),
    ];
    accounts.forEach((item) => {
      blocks.push(section(item.agent_name || String(item.agent_id), item.error ? null : item, "", accountMeta(item)));
    });
    box.innerHTML = blocks.join("");
    const meta = el("ov-meta");
    if (meta) meta.textContent = `${data.start_date} ~ ${data.end_date} · ${accounts.length} 个授权账号`;
  }

  let querying = false;

  async function load() {
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
    try {
      const qs = new URLSearchParams({ start_date: start, end_date: end });
      render(await api(`/api/overview/compare?${qs.toString()}`));
    } catch (err) {
      toast(err.message || "加载失败", "error");
    } finally {
      el("loading-mask").classList.add("hidden");
    }
  }

  async function runQuery(button) {
    if (querying) return;
    querying = true;
    setBusy(button, true);
    try {
      await load();
    } finally {
      querying = false;
      setBusy(button, false);
    }
  }

  el("ov-range").addEventListener("change", () => {
    applyRange(el("ov-range").value);
    if (el("ov-range").value !== "custom") runQuery(el("btn-query"));
  });
  el("btn-query").addEventListener("click", () => runQuery(el("btn-query")));
  applyRange("today");
  runQuery(el("btn-query"));
})();
