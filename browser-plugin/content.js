(() => {
  const HOST_ID = "qc-fangzhou-host";
  const OPEN_KEY = "qc_panel_open";

  function remember(open) {
    chrome.storage.session.set({ [OPEN_KEY]: open });
  }

  function hidePanel() {
    const host = document.getElementById(HOST_ID);
    if (host) host.remove();
    remember(false);
  }

  function showPanel() {
    let host = document.getElementById(HOST_ID);
    if (host) return host;
    host = document.createElement("aside");
    host.id = HOST_ID;
    host.innerHTML = `<header>
      <strong>千川方舟</strong>
      <button type="button" id="qc-fangzhou-close">关闭</button>
    </header>
    <iframe src="${chrome.runtime.getURL("app.html")}" title="千川方舟"></iframe>`;
    document.documentElement.appendChild(host);
    host.querySelector("#qc-fangzhou-close").addEventListener("click", hidePanel);
    remember(true);
    return host;
  }

  function setPanelMode(mode) {
    const host = document.getElementById(HOST_ID);
    if (!host) return;
    host.classList.toggle("qc-login", mode === "login");
  }

  function togglePanel() {
    if (document.getElementById(HOST_ID)) hidePanel();
    else showPanel();
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message) return;
    if (message.type === "panel-mode") {
      setPanelMode(message.mode);
      sendResponse({ ok: true });
      return;
    }
    if (message.type !== "toggle-panel") return;
    togglePanel();
    sendResponse({ ok: true });
  });

  chrome.storage.session.get(OPEN_KEY, (data) => {
    if (data && data[OPEN_KEY]) showPanel();
  });
})();
