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
