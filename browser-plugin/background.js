const API_ORIGIN = "http://120.76.58.132:8088";
const TOKEN_KEY = "qc_plugin_token_v1";
const USER_KEY = "qc_plugin_user_v1";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.type) return;
  if (message.type === "plugin-login") {
    pluginLogin(message).then(sendResponse);
    return true;
  }
  if (message.type === "plugin-logout") {
    chrome.storage.local.remove([TOKEN_KEY, USER_KEY], () => sendResponse({ ok: true }));
    return true;
  }
  if (message.type === "sync-profit") pushProfit(message, sender);
});

async function pluginLogin(message) {
  try {
    const response = await fetch(`${API_ORIGIN}/api/plugin/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: String(message.username || "").trim(),
        password: String(message.password || ""),
      }),
    });
    const data = await response.json();
    const token = data && data.data && data.data.token;
    if (!data || data.code !== 0 || !token) {
      return { ok: false, msg: (data && data.msg) || "登录失败" };
    }
    const username = String(data.data.username || "");
    await chrome.storage.local.set({ [TOKEN_KEY]: token, [USER_KEY]: username });
    return { ok: true, username };
  } catch (_) {
    return { ok: false, msg: "连不上后台" };
  }
}

async function pushProfit(message, sender) {
  const advertiserId = String(message.advertiserId || "").trim();
  const adId = String(message.adId || "").trim();
  if (!/^\d+$/.test(advertiserId) || !/^\d+$/.test(adId)) return;
  const stored = await chrome.storage.local.get([TOKEN_KEY]);
  const token = stored && stored[TOKEN_KEY];
  if (!token) return;
  try {
    const response = await fetch(`${API_ORIGIN}/api/plugin/profit-input`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Ad-Helper-Plugin": token,
      },
      body: JSON.stringify({
        advertiser_id: Number(advertiserId),
        ad_id: Number(adId),
        product_name: String(message.productName || ""),
        commission: String(message.commission || ""),
        refund: String(message.refund || ""),
      }),
    });
    const data = await response.json();
    if (data && (data.code === 40100 || data.code === 40303)) {
      await chrome.storage.local.remove([TOKEN_KEY, USER_KEY]);
      const tabId = sender && sender.tab && sender.tab.id;
      if (tabId) chrome.tabs.sendMessage(tabId, { type: "plugin-auth-lost" });
    }
  } catch (_) {}
}

chrome.action.onClicked.addListener(async (tab) => {
  const url = String((tab && tab.url) || "");
  if (!tab || !tab.id || !url.startsWith("https://qianchuan.jinritemai.com/")) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "toggle-panel" });
  } catch (error) {
    const message = String((error && error.message) || error || "");
    if (/No tab with id/i.test(message)) return;
    try {
      await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ["content.css"] });
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    } catch (_) {}
  }
});
