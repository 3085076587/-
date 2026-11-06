(() => {
  const { el, toast, api, setBusy, refreshTokenPill } = window.AdHelper;
  let members = [];
  let current = "";
  let shops = [];
  let ownerFilter = "";
  let accountOperate = true;
  let planOperate = true;

  function esc(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function permText(item) {
    if (item.account_operate && item.plan_operate) return "可操作";
    if (!item.account_operate && !item.plan_operate) return "仅查看";
    return "部分可操作";
  }

  function shopText(item) {
    return item.all_shops ? "全部账户" : `${item.shop_count} 个`;
  }

  function renderMembers() {
    const body = el("member-tbody");
    el("member-quota").textContent = `成员 ${members.length}`;
    if (!members.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">还没有其他登录账号</td></tr>';
      return;
    }
    body.innerHTML = members
      .map((item) => {
        const initial = esc(String(item.username || "?").slice(0, 1));
        return `<tr>
          <td><span class="member-cell"><span class="member-avatar">${initial}</span><b>${esc(item.username)}</b></span></td>
          <td class="mono">${item.user_id}</td>
          <td>${esc(shopText(item))}</td>
          <td><span class="tag-view">${esc(permText(item))}</span></td>
          <td><span class="tag-ok">已生效</span></td>
          <td><button type="button" class="link-btn" data-user="${esc(item.username)}">编辑</button></td>
        </tr>`;
      })
      .join("");
    body.querySelectorAll("button[data-user]").forEach((node) => {
      node.addEventListener("click", () => openMember(node.dataset.user));
    });
  }

  function filteredShops() {
    const keyword = el("shop-search").value.trim().toLowerCase();
    return shops.filter((item) => {
      if (ownerFilter && item.owner !== ownerFilter) return false;
      if (!keyword) return true;
      return (
        String(item.advertiser_name || "").toLowerCase().includes(keyword) ||
        String(item.advertiser_id).includes(keyword) ||
        String(item.owner || "").toLowerCase().includes(keyword)
      );
    });
  }

  function paintSeg(id, on) {
    el(id).querySelectorAll("button").forEach((node) => {
      node.classList.toggle("on", node.dataset.value === (on ? "1" : "0"));
    });
  }

  function renderOwners() {
    const counts = new Map();
    shops.forEach((item) => {
      const owner = item.owner || "未识别";
      counts.set(owner, (counts.get(owner) || 0) + 1);
    });
    const chips = [`<button type="button" class="owner-chip ${ownerFilter ? "" : "on"}" data-owner="">全部 (${shops.length})</button>`];
    [...counts.entries()].forEach(([owner, count]) => {
      chips.push(
        `<button type="button" class="owner-chip ${ownerFilter === owner ? "on" : ""}" data-owner="${esc(owner)}">${esc(owner)} (${count})</button>`
      );
    });
    const box = el("owner-chips");
    box.innerHTML = chips.join("");
    box.querySelectorAll("button").forEach((node) => {
      node.addEventListener("click", () => {
        ownerFilter = node.dataset.owner || "";
        renderOwners();
        renderShops();
      });
    });
  }

  function renderShops() {
    const rows = filteredShops();
    const box = el("shop-list");
    const picked = shops.filter((item) => item.visible).length;
    el("shop-picked").textContent = `已选 ${picked}/${shops.length} 家`;
    if (!rows.length) {
      box.innerHTML = '<div class="empty">没有匹配的账户</div>';
      return;
    }
    box.innerHTML = rows
      .map(
        (item) =>
          `<label class="team-check"><input type="checkbox" data-id="${item.advertiser_id}"${item.visible ? " checked" : ""}><span>${esc(item.advertiser_name)}</span></label>`
      )
      .join("");
    box.querySelectorAll("input").forEach((node) => {
      node.addEventListener("change", () => {
        const shop = shops.find((item) => String(item.advertiser_id) === node.dataset.id);
        if (shop) shop.visible = node.checked;
        el("shop-picked").textContent = `已选 ${shops.filter((item) => item.visible).length}/${shops.length} 家`;
      });
    });
  }

  function setFiltered(visible) {
    const ids = new Set(filteredShops().map((item) => item.advertiser_id));
    shops.forEach((item) => {
      if (ids.has(item.advertiser_id)) item.visible = visible;
    });
    renderShops();
  }

  function closeDialog() {
    el("member-mask").classList.add("hidden");
    current = "";
  }

  async function openMember(username) {
    current = username;
    ownerFilter = "";
    el("shop-search").value = "";
    el("member-dialog-title").textContent = `编辑成员 · ${username}`;
    el("member-mask").classList.remove("hidden");
    const data = await api(`/api/team/members/${encodeURIComponent(username)}`);
    shops = data.shops || [];
    accountOperate = !!data.account_operate;
    planOperate = !!data.plan_operate;
    paintSeg("seg-account", accountOperate);
    paintSeg("seg-plan", planOperate);
    renderOwners();
    renderShops();
  }

  async function save(button) {
    if (!current) return;
    setBusy(button, true);
    try {
      const data = await api(`/api/team/members/${encodeURIComponent(current)}`, {
        method: "PUT",
        body: JSON.stringify({
          advertiser_ids: shops.filter((item) => item.visible).map((item) => item.advertiser_id),
          account_operate: accountOperate,
          plan_operate: planOperate,
        }),
      });
      const member = members.find((item) => item.username === current);
      if (member) {
        const visible = (data.shops || []).filter((item) => item.visible).length;
        member.shop_count = visible;
        member.shop_total = (data.shops || []).length;
        member.all_shops = member.shop_total > 0 && visible === member.shop_total;
        member.account_operate = !!data.account_operate;
        member.plan_operate = !!data.plan_operate;
      }
      renderMembers();
      closeDialog();
      toast("已保存", "success");
    } catch (err) {
      toast(err.message || "保存失败", "error");
    } finally {
      setBusy(button, false);
    }
  }

  el("seg-account").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    accountOperate = button.dataset.value === "1";
    paintSeg("seg-account", accountOperate);
  });
  el("seg-plan").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    planOperate = button.dataset.value === "1";
    paintSeg("seg-plan", planOperate);
  });
  el("shop-search").addEventListener("input", renderShops);
  el("btn-all").addEventListener("click", () => setFiltered(true));
  el("btn-none").addEventListener("click", () => setFiltered(false));
  el("btn-own").addEventListener("click", () => {
    shops.forEach((item) => {
      item.visible = !!item.owned;
    });
    renderShops();
  });
  el("btn-cancel").addEventListener("click", closeDialog);
  el("btn-save").addEventListener("click", (event) => save(event.currentTarget));
  el("member-mask").addEventListener("click", (event) => {
    if (event.target === el("member-mask")) closeDialog();
  });

  (async () => {
    await refreshTokenPill();
    try {
      members = await api("/api/team/members");
      renderMembers();
    } catch (err) {
      el("member-tbody").innerHTML = `<tr><td colspan="6" class="empty">${esc(err.message || "只有管理员可以管理团队")}</td></tr>`;
    }
  })();
})();
