const API_BASE = "http://120.76.58.132:8081";

function oceanLoggedIn() {
  return new Promise((resolve) => {
    chrome.cookies.getAll({ domain: "oceanengine.com" }, (cookies) => {
      const hit = (cookies || []).some((cookie) => /sessionid|sid_tt|uid_tt|passport_auth/i.test(cookie.name) && cookie.value);
      resolve(hit);
    });
  });
}

function alreadySelf(currentUrl, username) {
  try {
    const url = new URL(currentUrl);
    if (!url.pathname.includes("/universe/account")) return false;
    const list = JSON.parse(url.searchParams.get("keyword") || "");
    return Array.isArray(list) && list.length === 1 && list[0] === username;
  } catch (_) {
    return false;
  }
}

function accountUrl(currentUrl, username) {
  let current = null;
  try {
    current = new URL(currentUrl);
  } catch (_) {
    current = new URL("https://business.oceanengine.com/");
  }
  const next = new URL("https://business.oceanengine.com/ebp/account-manage/ecp/bidding/universe/account");
  ["ebpid", "uuid"].forEach((key) => {
    const value = current.searchParams.get(key);
    if (value) next.searchParams.set(key, value);
  });
  next.searchParams.set("keyword", JSON.stringify([username]));
  next.searchParams.set("searchType", "[0]");
  return next.toString();
}

async function apiFetch(path, method, body) {
  const headers = { "Content-Type": "application/json" };
  const cookie = await chrome.cookies.get({ url: `${API_BASE}/`, name: "ad_helper_session" });
  if (cookie && cookie.value) headers.Cookie = `ad_helper_session=${cookie.value}`;
  let resp;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      method: method || "GET",
      headers,
      body: body || undefined,
      credentials: "include",
    });
  } catch (_) {
    return { ok: false, message: "连不上线上接口，请确认 120.76.58.132:8081 能打开" };
  }
  let payload;
  try {
    payload = await resp.json();
  } catch (_) {
    return { ok: false, message: `线上接口没有返回 JSON（HTTP ${resp.status}）` };
  }
  if (typeof payload.code === "undefined") {
    return { ok: false, message: `线上接口异常 HTTP ${resp.status}` };
  }
  if (payload.code !== 0) return { ok: false, message: payload.msg || "请求失败", code: payload.code };
  return { ok: true, data: payload.data };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "api") {
    apiFetch(message.path, message.method, message.body).then(sendResponse);
    return true;
  }
  if (!message || message.type !== "focus-self" || !sender.tab) return;
  const tabId = sender.tab.id;
  oceanLoggedIn().then((loggedIn) => {
    if (!loggedIn) {
      if (!String(sender.tab.url || "").includes("/login")) {
        chrome.tabs.update(tabId, { url: "https://business.oceanengine.com/login" });
      }
      sendResponse({ loggedIn: false });
      return;
    }
    if (!alreadySelf(sender.tab.url, message.username)) {
      chrome.tabs.update(tabId, { url: accountUrl(sender.tab.url, message.username) });
    }
    sendResponse({ loggedIn: true });
  });
  return true;
});

async function togglePanel(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "toggle-panel" });
  } catch (_) {
    await chrome.storage.session.set({ qc_panel_open: true });
    await chrome.scripting.insertCSS({ target: { tabId }, files: ["content.css"] });
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  }
}

chrome.action.onClicked.addListener(async (tab) => {
  const url = String(tab.url || "");
  if (!url.startsWith("https://business.oceanengine.com/")) return;
  try {
    await togglePanel(tab.id);
  } catch (err) {
    console.error(err);
  }
});
