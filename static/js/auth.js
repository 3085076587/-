(() => {
  const { el, toast, api, setBusy, renderTokenPill } = window.AdHelper;

  async function loadPage() {
    const [oauth, advertisers] = await Promise.all([
      api("/api/oauth/status"),
      api("/api/advertiser/list"),
    ]);
    renderTokenPill(oauth);
    if (!oauth.has_token) {
      el("stat-token").textContent = "无";
      el("stat-agent").textContent = "-";
      el("stat-agent-name").textContent = "管理账户名称 / 角色：-";
      el("stat-expire").textContent = "-";
    } else {
      el("stat-token").textContent = oauth.access_expired ? "过期" : "有效";
      el("stat-agent").textContent = oauth.agent_id || "-";
      el("stat-agent-name").textContent = oauth.agent_name
        ? `${oauth.agent_name}（${oauth.account_role || "-"}）`
        : oauth.account_role || "-";
      el("stat-expire").textContent = oauth.expires_at || "-";
    }
    el("stat-adv").textContent = String((advertisers || []).length);
    const accounts = await api("/api/oauth/accounts");
    renderAccounts(accounts);
  }

  function renderAccounts(data) {
    const box = el("account-switch");
    const hint = el("account-hint");
    box.innerHTML = "";
    const rows = (data && data.accounts) || [];
    if (!rows.length) {
      hint.textContent = "还没有授权账号。点下面的「打开巨量授权」。";
      return;
    }
    const current = rows.find((item) => item.selected) || rows[0];
    rows.forEach((item) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = item.selected ? "btn btn-primary" : "btn";
      const kind = item.account_kind === "shop" ? "单店" : "组织";
      btn.textContent = `${item.agent_name || item.agent_id}（${kind}）`;
      btn.addEventListener("click", () => selectAccount(item.agent_id));
      box.appendChild(btn);
    });
    if (current.account_kind === "shop" && !current.has_workbench) {
      hint.textContent = "当前是单店。子账户可以同步，数据总览还要等这个账号自己的工作台登录。豆娱传媒的数不会写到这里。";
      return;
    }
    hint.textContent = current.account_kind === "shop"
      ? "当前是单店。总览、店铺和定时入库只看这一家，并重新拉取。"
      : "当前是组织账号。总览、店铺和定时入库只看这个组织下面的店。";
  }

  async function selectAccount(agentId) {
    try {
      const data = await api("/api/oauth/select", {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId }),
      });
      toast("已切换授权账号");
      renderAccounts(data);
      await loadPage();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  el("btn-authorize").addEventListener("click", () => {
    const url = el("authorize-url").value;
    if (!url) {
      toast("未配置授权链接", "error");
      return;
    }
    window.open(url, "_blank");
    toast("请在新窗口完成授权，完成后回到本页会自动刷新状态", "warn");
  });

  el("btn-sync-advertisers").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    setBusy(btn, true);
    try {
      const data = await api("/api/advertiser/sync-all");
      toast(`同步完成：新增 ${data.created}，更新 ${data.updated}`);
      await loadPage();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadPage().catch(() => {});
  });
  window.addEventListener("focus", () => loadPage().catch(() => {}));

  loadPage().catch((err) => toast(err.message, "error"));
})();
