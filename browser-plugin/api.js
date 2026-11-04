const QcApi = globalThis.QcApi = (() => {
  const API_BASE = "http://120.76.58.132:8081";
  const COOKIE_NAME = "ad_helper_session";

  function getSession() {
    return new Promise((resolve) => {
      chrome.cookies.get({ url: `${API_BASE}/`, name: COOKIE_NAME }, (cookie) => {
        resolve(cookie && cookie.value ? cookie.value : "");
      });
    });
  }

  function request(path, options = {}) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({
        type: "api",
        path,
        method: options.method || "GET",
        body: options.body || null,
      }, (resp) => {
        if (chrome.runtime.lastError) {
          reject(new Error("插件后台没有响应，请重新加载插件"));
          return;
        }
        if (!resp || !resp.ok) {
          const error = new Error((resp && resp.message) || "请求失败");
          error.code = resp && resp.code;
          reject(error);
          return;
        }
        resolve(resp.data);
      });
    });
  }

  function logout() {
    return request("/api/auth/logout", { method: "POST", body: "{}" }).finally(() => new Promise((resolve) => {
      chrome.cookies.remove({ url: `${API_BASE}/`, name: COOKIE_NAME }, () => resolve());
    }));
  }

  async function login(username, password) {
    await request("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    return request("/api/auth/me");
  }

  function oceanLoggedIn() {
    return new Promise((resolve) => {
      chrome.cookies.getAll({ domain: "oceanengine.com" }, (cookies) => {
        const list = cookies || [];
        const hit = list.some((cookie) => /sessionid|sid_tt|uid_tt|passport_auth/i.test(cookie.name) && cookie.value);
        resolve(hit);
      });
    });
  }

  function oceanAccountUrl(username) {
    const keyword = encodeURIComponent(JSON.stringify([username]));
    const searchType = encodeURIComponent("[0]");
    return `https://business.oceanengine.com/ebp/account-manage/ecp/bidding/universe/account?keyword=${keyword}&searchType=${searchType}`;
  }

  function findOceanTab() {
    return new Promise((resolve) => {
      chrome.tabs.query({ url: "https://business.oceanengine.com/*" }, (tabs) => {
        resolve(tabs && tabs.length ? tabs[0] : null);
      });
    });
  }

  async function openOcean(username) {
    const loggedIn = await oceanLoggedIn();
    const url = loggedIn
      ? oceanAccountUrl(username)
      : "https://business.oceanengine.com/login";
    const existing = await findOceanTab();
    if (existing) await chrome.tabs.update(existing.id, { url, active: true });
    else await chrome.tabs.create({ url });
    return loggedIn;
  }

  return { API_BASE, request, login, logout, oceanLoggedIn, openOcean };
})();
