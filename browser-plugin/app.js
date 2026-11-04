(() => {
  const P = globalThis.QcProfit;
  const A = globalThis.QcApi;
  const $ = (id) => document.getElementById(id);

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

  let me = null;
  let accounts = [];
  let overview = {};
  let sortKey = "stat_cost";
  let sortAsc = false;
  let keyword = "";
  let plans = [];
  let planAdvertiserId = "";
  let plansByAccount = {};
  let picked = new Set();
  let opPlan = null;
  let reminders = [];
  let busy = false;

  function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function setStatus(text) {
    $("status").textContent = text || "";
  }

  function statusText(label) {
    return STATUS_LABELS[label] || label || "-";
  }

  function rangeQuery() {
    const key = $("dash-range").value || "today";
    if (key === "custom") {
      return new URLSearchParams({ start_date: $("dash-start").value, end_date: $("dash-end").value });
    }
    return new URLSearchParams({ range: key });
  }

  function planDates() {
    const key = $("plan-range").value || "today";
    const end = P.todayText();
    if (key === "7") return { start: P.shiftDate(6), end };
    if (key === "30") return { start: P.shiftDate(29), end };
    return { start: end, end };
  }

  function rateOf(plan) {
    if (!plan || plan.actual_refund_rate == null || plan.actual_refund_rate === "") return "";
    return String(plan.actual_refund_rate);
  }

  function accountEstimate(item) {
    const list = plansByAccount[String(item.advertiser_id)];
    if (!list || !list.length) return null;
    return P.sumPlanLines(list, rateOf);
  }

  function visibleAccounts() {
    const q = keyword.trim().toLowerCase();
    const list = accounts.filter((item) => {
      if (!q) return true;
      return `${item.advertiser_name || ""} ${item.advertiser_id || ""}`.toLowerCase().includes(q);
    });
    list.sort((a, b) => {
      const value = (row) => {
        if (sortKey === "estimate") {
          const num = accountEstimate(row);
          return num == null ? -Infinity : num;
        }
        const num = Number(row[sortKey]);
        return Number.isFinite(num) ? num : -Infinity;
      };
      const av = value(a);
      const bv = value(b);
      return sortAsc ? av - bv : bv - av;
    });
    return list;
  }

  function paintOverview() {
    const cards = [
      ["整体消耗", P.moneyText(overview.stat_cost)],
      ["用户实际支付金额", P.moneyText(overview.pay_amount)],
      ["整体支付 ROI", P.roiText(overview.pay_roi)],
      ["净成交金额", P.moneyText(overview.settle_amount)],
      ["净成交 ROI", P.roiText(overview.settle_roi)],
    ];
    $("overview").innerHTML = cards.map(([label, value]) => `<div class="stat"><b>${value}</b>${label}</div>`).join("");
  }

  function paintAccounts() {
    const list = visibleAccounts();
    $("account-count").textContent = `显示 ${list.length} / 共 ${accounts.length} 个`;
    if (!list.length) {
      $("account-body").innerHTML = `<tr><td colspan="10">暂无子账户</td></tr>`;
      return;
    }
    $("account-body").innerHTML = list.map((item) => {
      const estimate = accountEstimate(item);
      const name = esc(item.advertiser_name || "未命名");
      return `<tr data-id="${item.advertiser_id}">
        <td class="name">${name}<div class="sub">ID ${item.advertiser_id}</div></td>
        <td>${esc(item.status || "-")}</td>
        <td>${P.moneyText(item.stat_cost)}</td>
        <td>${P.roiText(item.pay_roi)}</td>
        <td>${P.moneyText(item.pay_amount)}</td>
        <td>${P.moneyText(item.settle_amount)}</td>
        <td>${P.roiText(item.settle_roi)}</td>
        <td>${P.rateText(item.refund_rate_1h)}</td>
        <td class="${estimate != null && estimate < 0 ? "neg" : ""}">${P.moneyText(estimate)}</td>
        <td><button type="button" data-open-plans="${item.advertiser_id}">看计划</button></td>
      </tr>`;
    }).join("");
  }

  function fillAdvertisers() {
    const select = $("plan-advertiser");
    const current = planAdvertiserId || select.value;
    select.innerHTML = accounts.map((item) => {
      const name = esc(item.advertiser_name || item.advertiser_id);
      return `<option value="${item.advertiser_id}">${name}</option>`;
    }).join("");
    if (current) select.value = String(current);
  }

  function remainText(adId) {
    const hit = reminders.find((item) => String(item.ad_id) === String(adId) && item.status === "counting");
    if (!hit) return "-";
    const seconds = Number(hit.remain_seconds) || 0;
    const min = Math.floor(seconds / 60);
    const sec = String(seconds % 60).padStart(2, "0");
    return `${min}:${sec}`;
  }

  function paintPlans() {
    if (!plans.length) {
      $("plan-body").innerHTML = `<tr><td colspan="14">暂无计划</td></tr>`;
      $("plan-hint").textContent = "";
      return;
    }
    $("plan-body").innerHTML = plans.map((item) => {
      const checked = item.enabled ? "checked" : "";
      const pickedAttr = picked.has(String(item.ad_id)) ? "checked" : "";
      const pct = item.commission_pct == null ? "" : item.commission_pct;
      const estimate = P.planEstimate(item, rateOf(item));
      const productId = item.product_id || "";
      return `<tr>
        <td><input type="checkbox" data-pick="${item.ad_id}" ${pickedAttr}></td>
        <td><input type="checkbox" data-toggle="${item.ad_id}" ${checked}></td>
        <td class="name">${esc(item.ad_name || "-")}<div class="sub">商品 ${esc(item.product_name || "未挂商品")} ${productId ? "ID " + productId : ""}</div></td>
        <td>${esc(statusText(item.status_label))}</td>
        <td>预算 ${P.moneyText(item.budget)}<div class="sub">ROI ${P.roiText(item.roi2_goal)}</div></td>
        <td>${P.moneyText(item.stat_cost)}</td>
        <td>${P.moneyText(item.settle_amount)}</td>
        <td>${P.roiText(item.pay_roi)}</td>
        <td>${P.rateText(item.refund_rate_1h)}</td>
        <td><input data-commission="${item.ad_id}" value="${esc(pct)}" placeholder="佣金%" inputmode="decimal"></td>
        <td><input data-product-rate="${item.ad_id}" value="${esc(rateOf(item))}" placeholder="未填" inputmode="decimal"></td>
        <td class="${estimate != null && estimate < 0 ? "neg" : ""}">${P.moneyText(estimate)}</td>
        <td>${remainText(item.ad_id)}</td>
        <td><button type="button" data-op="${item.ad_id}">操作</button></td>
      </tr>`;
    }).join("");
    const summed = P.sumPlanLines(plans, rateOf);
    $("plan-hint").textContent = `已加载 ${plans.length} 条。预估利润合计 ${P.moneyText(summed)}。同一商品的实际退款率只填一次，记在商品上。`;
  }

  function showView(name) {
    $("accounts-view").classList.toggle("hidden", name !== "accounts");
    $("plans-view").classList.toggle("hidden", name !== "plans");
    $("tab-accounts").classList.toggle("active", name === "accounts");
    $("tab-plans").classList.toggle("active", name === "plans");
    if (name === "plans") {
      fillAdvertisers();
      if (!planAdvertiserId && accounts[0]) planAdvertiserId = String(accounts[0].advertiser_id);
      if (planAdvertiserId) $("plan-advertiser").value = String(planAdvertiserId);
    }
  }

  async function loadDashboard(refresh) {
    const qs = rangeQuery();
    if (refresh && $("dash-range").value !== "custom") qs.set("refresh", "true");
    if ($("dash-range").value === "custom" && (!$("dash-start").value || !$("dash-end").value)) {
      setStatus("请选择开始和结束日期。");
      return;
    }
    setStatus("正在从线上读取子账户…");
    const data = await A.request(`/api/advertiser/dashboard?${qs.toString()}`);
    accounts = data.accounts || [];
    overview = data.overview || {};
    plansByAccount = {};
    plans = [];
    paintOverview();
    paintAccounts();
    fillAdvertisers();
    const cache = data.from_cache ? "缓存" : "实时";
    setStatus(`${cache} · ${data.start_date || ""} ~ ${data.end_date || ""} · ${data.viewer_scope || me.username}`);
  }

  async function loadPlans() {
    planAdvertiserId = $("plan-advertiser").value;
    if (!planAdvertiserId) {
      setStatus("没有子账户。");
      return;
    }
    const dates = planDates();
    setStatus("正在从线上拉取计划…");
    const qs = new URLSearchParams({
      advertiser_id: planAdvertiserId,
      start_date: dates.start,
      end_date: dates.end,
    });
    const data = await A.request(`/api/plan/uni/list?${qs.toString()}`);
    plans = data.plans || [];
    plansByAccount[String(planAdvertiserId)] = plans;
    picked = new Set();
    paintPlans();
    paintAccounts();
    setStatus(data.note || `计划已更新 ${dates.start} ~ ${dates.end}`);
  }

  async function loadReminders() {
    try {
      reminders = await A.request("/api/reminders");
      if (!$("plans-view").classList.contains("hidden")) paintPlans();
    } catch (_) {}
  }

  async function saveRate(plan, value) {
    if (!plan.product_id) {
      setStatus("这条计划没有商品，填不了实际退款率。");
      paintPlans();
      return;
    }
    const data = await A.request("/api/product/refund-rate", {
      method: "POST",
      body: JSON.stringify({
        advertiser_id: Number(planAdvertiserId),
        product_id: Number(plan.product_id),
        product_name: plan.product_name || "",
        actual_refund_rate: value.trim(),
      }),
    });
    const shown = data.actual_refund_rate == null ? "" : String(data.actual_refund_rate);
    plans.forEach((item) => {
      if (String(item.product_id) === String(plan.product_id)) item.actual_refund_rate = shown;
    });
    paintPlans();
    paintAccounts();
    setStatus(shown === "" ? "已清空这个商品的实际退款率" : `实际退款率已记为 ${shown}%`);
  }

  function focusSelf() {
    if (!me) return Promise.resolve();
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "focus-self", username: me.username }, (resp) => {
        if (resp && resp.loggedIn) setStatus(`当前页只看「${me.username}」的账户。数字来自线上。`);
        else setStatus("浏览器还没登录巨量，正在打开登录页。");
        resolve(resp);
      });
    });
  }

  function showLoginError(message) {
    const node = $("form-error");
    node.textContent = message || "";
    node.classList.toggle("hidden", !message);
  }

  function panelMode(mode) {
    chrome.runtime.sendMessage({ type: "panel-mode", mode });
  }

  function showApp(user) {
    me = user;
    document.body.classList.remove("is-login");
    panelMode("app");
    $("who").textContent = user.username;
    $("who-avatar").textContent = String(user.username || "").slice(0, 1);
    $("login-box").classList.add("hidden");
    $("accounts-view").classList.remove("hidden");
    showLoginError("");
  }

  function showLogin(message) {
    me = null;
    document.body.classList.add("is-login");
    panelMode("login");
    $("who").textContent = "未登录";
    $("login-box").classList.remove("hidden");
    $("accounts-view").classList.add("hidden");
    $("plans-view").classList.add("hidden");
    setStatus("");
    showLoginError(message && message !== "用你自己的账号登录。" ? message : "");
  }

  $("pwd-toggle").addEventListener("click", () => {
    const input = $("password");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    $("pwd-toggle").textContent = show ? "隐藏" : "显示";
  });

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const submitBtn = $("submit-btn");
    busy = true;
    showLoginError("");
    submitBtn.disabled = true;
    submitBtn.textContent = "登录中…";
    try {
      const user = await A.login($("username").value.trim(), $("password").value);
      $("password").value = "";
      showApp(user);
      await loadDashboard(false);
      await loadReminders();
    } catch (err) {
      showLoginError(err.message || "登录失败");
    } finally {
      busy = false;
      submitBtn.disabled = false;
      submitBtn.textContent = "进入工作台";
    }
  });

  $("tab-accounts").addEventListener("click", () => showView("accounts"));
  $("tab-plans").addEventListener("click", () => showView("plans"));
  $("btn-logout").addEventListener("click", async () => {
    try {
      await A.logout();
    } catch (_) {}
    plans = [];
    accounts = [];
    plansByAccount = {};
    showLogin("");
  });
  $("dash-range").addEventListener("change", () => {
    const custom = $("dash-range").value === "custom";
    document.querySelectorAll(".custom").forEach((node) => node.classList.toggle("hidden", !custom));
    if (custom) {
      $("dash-start").value = $("dash-start").value || P.todayText();
      $("dash-end").value = $("dash-end").value || P.todayText();
      return;
    }
    loadDashboard(false).catch((err) => setStatus(err.message));
  });
  $("btn-load-dash").addEventListener("click", () => {
    loadDashboard(true).catch((err) => setStatus(err.message));
  });
  $("btn-sync").addEventListener("click", async () => {
    setStatus("正在同步子账户…");
    try {
      const data = await A.request("/api/advertiser/sync-all");
      setStatus(`同步完成：新增 ${data.created}，更新 ${data.updated}`);
      await loadDashboard(true);
    } catch (err) {
      setStatus(err.message || "同步失败");
    }
  });
  $("account-search").addEventListener("input", () => {
    keyword = $("account-search").value;
    paintAccounts();
  });
  $("account-table").addEventListener("click", (event) => {
    const th = event.target.closest("th[data-sort]");
    if (th) {
      const key = th.dataset.sort;
      if (sortKey === key) sortAsc = !sortAsc;
      else { sortKey = key; sortAsc = false; }
      paintAccounts();
      return;
    }
    const open = event.target.closest("[data-open-plans]");
    if (!open) return;
    planAdvertiserId = open.dataset.openPlans;
    showView("plans");
    loadPlans().catch((err) => setStatus(err.message));
  });

  $("btn-load-plans").addEventListener("click", () => loadPlans().catch((err) => setStatus(err.message)));
  $("plan-advertiser").addEventListener("change", () => loadPlans().catch((err) => setStatus(err.message)));
  $("plan-range").addEventListener("change", () => loadPlans().catch((err) => setStatus(err.message)));

  $("plan-body").addEventListener("change", async (event) => {
    const toggle = event.target.closest("[data-toggle]");
    if (toggle) {
      const adId = Number(toggle.dataset.toggle);
      const optStatus = toggle.checked ? "ENABLE" : "DISABLE";
      toggle.disabled = true;
      try {
        await A.request("/api/plan/update-status", {
          method: "POST",
          body: JSON.stringify({ advertiser_id: Number(planAdvertiserId), ad_id: adId, opt_status: optStatus }),
        });
        setStatus(optStatus === "ENABLE" ? "计划已开启" : "计划已暂停");
        await loadPlans();
      } catch (err) {
        toggle.checked = !toggle.checked;
        setStatus(err.message || "启停失败");
      } finally {
        toggle.disabled = false;
      }
      return;
    }
    const pick = event.target.closest("[data-pick]");
    if (pick) {
      if (pick.checked) picked.add(pick.dataset.pick);
      else picked.delete(pick.dataset.pick);
      return;
    }
    const rateInput = event.target.closest("[data-product-rate]");
    if (rateInput) {
      const plan = plans.find((row) => String(row.ad_id) === rateInput.dataset.productRate);
      if (plan) saveRate(plan, rateInput.value).catch((err) => setStatus(err.message));
      return;
    }
    const commission = event.target.closest("[data-commission]");
    if (!commission) return;
    const item = plans.find((row) => String(row.ad_id) === commission.dataset.commission);
    if (!item) return;
    const text = commission.value.trim();
    const pct = text === "" ? null : Number(text);
    item.commission_pct = pct;
    paintPlans();
    paintAccounts();
    try {
      await A.request("/api/plan/commission", {
        method: "POST",
        body: JSON.stringify({
          advertiser_id: Number(planAdvertiserId),
          ad_id: Number(item.ad_id),
          commission_pct: pct,
        }),
      });
    } catch (err) {
      setStatus(err.message || "佣金保存失败");
    }
  });

  async function deletePlans(items) {
    if (!items.length) {
      setStatus("请选择要删除的计划。");
      return;
    }
    if (items.length > 10) {
      setStatus("一次最多删除 10 个计划。");
      return;
    }
    const names = items.map((item) => item.ad_name || item.ad_id).join("、");
    if (!window.confirm(`确定删除这 ${items.length} 个计划？${names}`)) return;
    await A.request("/api/plan/uni/delete", {
      method: "POST",
      body: JSON.stringify({
        advertiser_id: Number(planAdvertiserId),
        plans: items.map((item) => ({
          ad_id: String(item.ad_id),
          status: item.status || "",
          opt_status: item.opt_status || "",
          status_label: item.status_label || "",
          stat_cost: item.stat_cost,
        })),
      }),
    });
    if (opPlan && items.some((item) => String(item.ad_id) === String(opPlan.ad_id))) closeDrawer();
    setStatus("计划已删除");
    await loadPlans();
  }

  function closeDrawer() {
    opPlan = null;
    $("drawer").classList.add("hidden");
  }

  async function openDrawer(item) {
    opPlan = item;
    $("drawer").classList.remove("hidden");
    $("op-title").textContent = item.ad_name || "计划操作";
    $("op-sub").textContent = `ID ${item.ad_id} · ${statusText(item.status_label)} · ROI目标 ${P.roiText(item.roi2_goal)}`;
    $("roi-value").value = item.roi2_goal == null ? "" : item.roi2_goal;
    $("log-body").innerHTML = `<tr><td colspan="5">正在查询…</td></tr>`;
    const dates = planDates();
    try {
      const qs = new URLSearchParams({
        advertiser_id: planAdvertiserId,
        ad_id: String(item.ad_id),
        start_date: dates.start,
        end_date: dates.end,
        page: "1",
      });
      const data = await A.request(`/api/plan/uni/logs?${qs.toString()}`);
      const rows = [];
      (data.logs || []).forEach((entry) => {
        const changes = entry.changes && entry.changes.length
          ? entry.changes
          : [{ field: entry.content_title || "-", before: "-", after: "-" }];
        changes.forEach((change) => {
          rows.push(`<tr>
            <td>${esc(entry.create_time || "-")}</td>
            <td>${esc(entry.operator_name || "-")}</td>
            <td>${esc(change.field || entry.content_title || "-")}</td>
            <td>${esc(change.before || "-")}</td>
            <td>${esc(change.after || "-")}</td>
          </tr>`);
        });
      });
      $("log-body").innerHTML = rows.length ? rows.join("") : `<tr><td colspan="5">这个时间段没有操作日志</td></tr>`;
    } catch (err) {
      $("log-body").innerHTML = `<tr><td colspan="5">${esc(err.message)}</td></tr>`;
    }
  }

  $("plan-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-op]");
    if (!button) return;
    const item = plans.find((row) => String(row.ad_id) === button.dataset.op);
    if (item) openDrawer(item);
  });
  $("op-close").addEventListener("click", closeDrawer);
  $("drawer").addEventListener("click", (event) => {
    if (event.target === $("drawer")) closeDrawer();
  });
  $("op-delete").addEventListener("click", () => {
    if (!opPlan) return;
    deletePlans([opPlan]).catch((err) => setStatus(err.message));
  });
  $("btn-delete-plans").addEventListener("click", () => {
    const items = plans.filter((row) => picked.has(String(row.ad_id)));
    deletePlans(items).catch((err) => setStatus(err.message));
  });
  $("btn-roi-save").addEventListener("click", async () => {
    if (!opPlan) return;
    const roi2Goal = Number($("roi-value").value);
    if (!roi2Goal) {
      setStatus("请填写 ROI。");
      return;
    }
    const remind = Number($("roi-remind").value) || null;
    try {
      const data = await A.request("/api/plan/update-roi", {
        method: "POST",
        body: JSON.stringify({
          advertiser_id: Number(planAdvertiserId),
          ad_id: Number(opPlan.ad_id),
          ad_name: opPlan.ad_name || "",
          roi2_goal: roi2Goal,
          remind_after_seconds: remind,
        }),
      });
      setStatus(data.reminder ? "ROI 已更新，倒计时已开始" : "ROI 已更新");
      await loadPlans();
      await loadReminders();
    } catch (err) {
      setStatus(err.message || "ROI 保存失败");
    }
  });

  async function boot() {
    const today = P.todayText();
    $("dash-start").value = today;
    $("dash-end").value = today;
    try {
      const user = await A.request("/api/auth/me");
      showApp(user);
      await loadDashboard(false);
      await loadReminders();
      setInterval(loadReminders, 15000);
    } catch (err) {
      showLogin(err.code === 40100 ? "用你自己的账号登录。" : (err.message || "还没连上线上"));
    }
  }

  boot();
})();
