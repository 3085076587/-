/** 多提醒并存：倒计时刷新 + 到期弹窗互不覆盖。 */
(() => {
  const { api } = window.AdHelper || {};
  if (!api) return;

  const shownIds = new Set();
  let countdownCache = [];

  function ensureStack() {
    let stack = document.getElementById("reminder-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "reminder-stack";
      stack.className = "reminder-stack";
      document.body.appendChild(stack);
    }
    return stack;
  }

  function fmtRemain(sec) {
    const s = Math.max(0, Number(sec) || 0);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
    return `${m}:${String(r).padStart(2, "0")}`;
  }

  function showPopup(item) {
    if (!item || !item.id || shownIds.has(item.id)) return;
    shownIds.add(item.id);
    const stack = ensureStack();
    const card = document.createElement("div");
    card.className = "reminder-card";
    card.dataset.id = item.id;
    card.innerHTML = `
      <div class="reminder-title">ROI 提醒到期</div>
      <div class="reminder-body">
        计划「${item.ad_name || item.ad_id}」<br>
        净成交 ROI 目标：<b>${item.roi2_goal}</b>
      </div>
      <div class="reminder-actions">
        <button class="btn btn-primary" data-ack="${item.id}">知道了</button>
      </div>
    `;
    stack.appendChild(card);
    card.querySelector("[data-ack]").addEventListener("click", async () => {
      try {
        await api(`/api/reminders/${item.id}/ack`, { method: "POST", body: "{}" });
      } catch (_) {}
      card.remove();
    });
  }

  function paintCountdowns() {
    document.querySelectorAll("[data-remind-ad]").forEach((node) => {
      const adId = String(node.getAttribute("data-remind-ad"));
      const hit = countdownCache.find(
        (x) => String(x.ad_id) === adId && x.status === "counting"
      );
      if (!hit) {
        node.textContent = "-";
        node.classList.remove("countdown-on");
        return;
      }
      node.textContent = fmtRemain(hit.remain_seconds);
      node.classList.add("countdown-on");
      node.title = `ROI ${hit.roi2_goal} · 到期提醒`;
    });
  }

  async function tick() {
    try {
      const list = await api("/api/reminders");
      countdownCache = list || [];
      paintCountdowns();
      (list || [])
        .filter((x) => x.status === "fired")
        .forEach((x) => showPopup(x));
    } catch (_) {
      // Redis 未开时静默，避免刷屏
    }
  }

  window.AdReminders = {
    refresh: tick,
    getByAdId(adId) {
      return countdownCache.find(
        (x) => String(x.ad_id) === String(adId) && x.status === "counting"
      );
    },
  };

  tick();
  setInterval(tick, 2000);
})();
