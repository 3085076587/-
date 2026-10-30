/** 全站公共工具：请求封装、Toast、顶栏 token 状态。 */
window.AdHelper = (() => {
  function el(id) {
    return document.getElementById(id);
  }

  function toast(msg, type = "success") {
    const node = document.createElement("div");
    node.className = `toast ${type}`;
    node.textContent = msg;
    document.body.appendChild(node);
    setTimeout(() => node.remove(), 2600);
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    let body;
    try {
      body = await resp.json();
    } catch (_) {
      throw new Error(`接口返回非 JSON（HTTP ${resp.status}）`);
    }
    if (typeof body.code === "undefined") {
      throw new Error(
        body.detail
          ? `接口不可用：${body.detail}（HTTP ${resp.status}），请重启本地服务`
          : `接口异常 HTTP ${resp.status}`
      );
    }
    if (body.code === 40100) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = `/login?next=${next}`;
      throw new Error(body.msg || "未登录");
    }
    if (body.code !== 0) {
      throw new Error(body.msg || "请求失败");
    }
    return body.data;
  }

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) {
      if (!button.dataset.label) button.dataset.label = button.textContent;
      button.disabled = true;
      button.textContent = "处理中…";
      return;
    }
    button.disabled = false;
    if (button.dataset.label) {
      button.textContent = button.dataset.label;
      delete button.dataset.label;
    }
  }

  function renderTokenPill(oauth) {
    const pill = el("token-pill");
    if (!pill) return;
    const data = oauth || { has_token: false };
    if (!data.has_token) {
      pill.className = "status-pill bad";
      pill.innerHTML = '<span class="status-dot"></span>未授权';
      return;
    }
    pill.className = data.access_expired ? "status-pill bad" : "status-pill";
    pill.innerHTML = data.access_expired
      ? '<span class="status-dot"></span>token 已过期'
      : '<span class="status-dot"></span>已授权';
  }

  async function refreshTokenPill() {
    try {
      const oauth = await api("/api/oauth/status");
      renderTokenPill(oauth);
      return oauth;
    } catch (_) {
      return null;
    }
  }

  function advertiserOptions(list, selected) {
    const options = ['<option value="">请选择子账户</option>'].concat(
      (list || []).map((item) => {
        const label = item.advertiser_name
          ? `${item.advertiser_id} · ${item.advertiser_name}`
          : String(item.advertiser_id);
        const sel = String(selected || "") === String(item.advertiser_id) ? " selected" : "";
        return `<option value="${item.advertiser_id}"${sel}>${label}</option>`;
      })
    );
    return options.join("");
  }

  function queryParam(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  return {
    el,
    toast,
    api,
    setBusy,
    renderTokenPill,
    refreshTokenPill,
    advertiserOptions,
    queryParam,
  };
})();
