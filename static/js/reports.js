(() => {
  const { el, toast, api, setBusy, refreshTokenPill, advertiserOptions, queryParam } =
    window.AdHelper;

  function defaultDates() {
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - 6);
    const fmt = (d) => d.toISOString().slice(0, 10);
    el("report-start").value = fmt(start);
    el("report-end").value = fmt(end);
  }

  function renderReports(list) {
    const tbody = el("report-tbody");
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">暂无报表数据</td></tr>';
      return;
    }
    tbody.innerHTML = list
      .map(
        (item) => `<tr>
          <td>${item.stat_date || "-"}</td>
          <td class="mono">${item.ad_id}</td>
          <td>${Number(item.cost || 0).toFixed(2)}</td>
          <td>${item.pay_order_count ?? 0}</td>
          <td>${Number(item.pay_amount || 0).toFixed(2)}</td>
          <td>${item.roi == null ? "-" : Number(item.roi).toFixed(4)}</td>
        </tr>`
      )
      .join("");
  }

  async function loadAdvertisers(selected) {
    const list = await api("/api/advertiser/list");
    el("report-advertiser").innerHTML = advertiserOptions(list, selected);
  }

  async function loadReports(advertiserId, startDate, endDate) {
    if (!advertiserId) {
      renderReports([]);
      return;
    }
    const qs = new URLSearchParams({ advertiser_id: advertiserId });
    if (startDate) qs.set("start_date", startDate);
    if (endDate) qs.set("end_date", endDate);
    const list = await api(`/api/plan/report/list?${qs.toString()}`);
    renderReports(list || []);
  }

  el("btn-sync-reports").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const advertiserId = el("report-advertiser").value;
    const startDate = el("report-start").value;
    const endDate = el("report-end").value;
    if (!advertiserId || !startDate || !endDate) {
      toast("请选择子账户和日期范围", "warn");
      return;
    }
    setBusy(btn, true);
    try {
      const qs = new URLSearchParams({
        advertiser_id: advertiserId,
        start_date: startDate,
        end_date: endDate,
      });
      const data = await api(`/api/plan/report/sync?${qs.toString()}`);
      toast(`报表同步完成：新增 ${data.created}，更新 ${data.updated}`);
      await loadReports(advertiserId, startDate, endDate);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  });

  el("btn-load-reports").addEventListener("click", async () => {
    try {
      await loadReports(
        el("report-advertiser").value,
        el("report-start").value,
        el("report-end").value
      );
    } catch (err) {
      toast(err.message, "error");
    }
  });

  (async () => {
    defaultDates();
    const selected = queryParam("advertiser_id") || "";
    await refreshTokenPill();
    await loadAdvertisers(selected);
    if (selected) {
      await loadReports(selected, el("report-start").value, el("report-end").value);
    }
  })().catch((err) => toast(err.message, "error"));
})();
