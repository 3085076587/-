(() => {
  const form = document.getElementById("login-form");
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  const err = document.getElementById("form-error");
  const btn = document.getElementById("submit-btn");
  const toggle = document.getElementById("pwd-toggle");

  function showError(msg) {
    err.textContent = msg || "";
    err.classList.toggle("hidden", !msg);
  }

  function nextUrl() {
    const raw = new URLSearchParams(location.search).get("next") || "/";
    if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/login")) {
      return "/";
    }
    return raw;
  }

  toggle.addEventListener("click", () => {
    const show = password.type === "password";
    password.type = show ? "text" : "password";
    toggle.textContent = show ? "隐藏" : "显示";
    toggle.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showError("");
    const u = username.value.trim();
    const p = password.value;
    if (!u || !p) {
      showError("请输入账号和密码");
      return;
    }
    btn.disabled = true;
    btn.textContent = "登录中…";
    try {
      const resp = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: u, password: p }),
      });
      const body = await resp.json();
      if (!body || body.code !== 0) {
        throw new Error((body && body.msg) || "登录失败");
      }
      location.href = nextUrl();
    } catch (ex) {
      showError(ex.message || "登录失败");
      btn.disabled = false;
      btn.textContent = "进入工作台";
    }
  });

  username.focus();
})();
