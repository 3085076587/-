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
