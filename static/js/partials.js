/** 注入统一侧栏，避免每个页面复制一整段导航 HTML。 */
(() => {
  const mount = document.getElementById("sidebar-mount");
  if (!mount) return;

  const active = mount.dataset.active || "";
  const items = [
    {
      key: "auth",
      href: "/",
      label: "授权账号信息",
      icon: '<path d="M4 19V5M4 19h16M8 17V9M12 17V7M16 17v-4"/>',
    },
    {
      key: "advertisers",
      href: "/advertisers",
      label: "子账户",
      icon: '<path d="M3 21h18M5 21V8l7-4 7 4v13M9 21v-6h6v6"/>',
    },
    {
      key: "plans",
      href: "/plans",
      label: "计划与 ROI",
      icon: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    },
    {
      key: "reports",
      href: "/reports",
      label: "投放报表",
      icon: '<path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/>',
    },
  ];

  mount.innerHTML = `
    <div class="logo">
      <img class="logo-img" src="/static/img/logo.png" alt="千川方舟">
      <div class="logo-title">千川方舟 ERP</div>
      <div class="logo-sub">投放数据 · ROI 调整</div>
    </div>
    <nav class="nav">
      <div class="nav-group-title">工作台</div>
      ${items
        .map(
          (item) => `
        <a class="nav-item ${item.key === active ? "active" : ""}" href="${item.href}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${item.icon}</svg>
          <span>${item.label}</span>
        </a>`
        )
        .join("")}
    </nav>
    <div class="sidebar-footer">
      <button type="button" class="nav-item logout-btn" id="btn-logout">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
          <polyline points="16 17 21 12 16 7"/>
          <line x1="21" y1="12" x2="9" y2="12"/>
        </svg>
        <span>退出登录</span>
      </button>
    </div>
  `;

  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      try {
        await fetch("/api/auth/logout", { method: "POST" });
      } catch (_) {}
      location.href = "/login";
    });
  }
})();
