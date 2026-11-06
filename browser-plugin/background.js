const API_ORIGIN = "http://120.76.58.132:8088";

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "sync-profit") return;
  pushProfit(message);
});

async function pushProfit(message) {
  const advertiserId = String(message.advertiserId || "").trim();
  const adId = String(message.adId || "").trim();
  if (!/^\d+$/.test(advertiserId) || !/^\d+$/.test(adId)) return;
  let cookie;
  try {
    cookie = await chrome.cookies.get({ url: `${API_ORIGIN}/`, name: "ad_helper_session" });
  } catch (_) {
    return;
  }
  if (!cookie || !cookie.value) return;
  try {
    await fetch(`${API_ORIGIN}/api/plugin/profit-input`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Ad-Helper-Session": cookie.value,
      },
      body: JSON.stringify({
        advertiser_id: Number(advertiserId),
        ad_id: Number(adId),
        product_name: String(message.productName || ""),
        commission: String(message.commission || ""),
        refund: String(message.refund || ""),
      }),
    });
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
