(() => {
  if (window.__qcFangzhouHook) return;
  window.__qcFangzhouHook = true;

  const known = new Map();

  function remember(pairs) {
    let changed = false;
    pairs.forEach((pair) => {
      if (!pair || !pair.adId || !pair.productId) return;
      if (known.get(pair.adId) === pair.productId) return;
      known.set(pair.adId, pair.productId);
      changed = true;
    });
    if (!changed) return;
    const payload = [...known.entries()].slice(-800).map(([adId, productId]) => ({ adId, productId }));
    const root = document.documentElement || document.head;
    if (!root) return;
    let node = document.getElementById("qc-fangzhou-products");
    if (!node) {
      node = document.createElement("div");
      node.id = "qc-fangzhou-products";
      node.hidden = true;
      root.appendChild(node);
    }
    node.textContent = JSON.stringify(payload);
    document.dispatchEvent(new CustomEvent("qc-fangzhou-products"));
  }

  function pairsFromText(text) {
    if (!text) return [];
    let source = String(text);
    if (source.length > 5000000) {
      const at = source.indexOf("adGoodsMap");
      if (at < 0) return [];
      source = source.slice(at, at + 2000000);
    }
    const hasNamed = source.indexOf("product_id") >= 0 || source.indexOf("productId") >= 0 || source.indexOf("product_info") >= 0 || source.indexOf("productInfo") >= 0 || source.indexOf("adGoodsMap") >= 0;
    const hasPlanObject = /"\d{15,17}"\s*:\s*\[?\s*\{/.test(source);
    if (!hasNamed && !hasPlanObject) return [];
    const pairs = [];
    const seen = new Set();
    const push = (adId, productId) => {
      if (!adId || !productId || adId === productId) return;
      if (adId.length < 15 || adId.length > 17) return;
      if (productId.length < 18 || productId.length > 20) return;
      const key = `${adId}:${productId}`;
      if (seen.has(key)) return;
      seen.add(key);
      pairs.push({ adId, productId });
    };
    if (hasPlanObject) {
      const mapAt = source.indexOf("adGoodsMap");
      const goodsText = mapAt >= 0 ? source.slice(mapAt, mapAt + 2000000) : source;
      const goodsMap = /"(\d{15,17})"\s*:\s*\[?\s*\{/g;
      let goods = goodsMap.exec(goodsText);
      while (goods) {
        const start = goods.index + goods[0].length;
        const next = goodsText.slice(start, start + 80000);
        const boundary = next.search(/"\d{15,17}"\s*:/);
        const chunk = boundary > 0 ? next.slice(0, boundary) : next;
        const product = chunk.match(/"(?:id|productId|product_id)"\s*:\s*"?(\d{18,20})"?/);
        if (product) push(goods[1], product[1]);
        goods = goodsMap.exec(goodsText);
      }
    }
    const adThenProduct = /"ad_info"\s*:\s*\{[\s\S]{0,1800}?"id"\s*:\s*"?(\d{15,17})"?[\s\S]{0,3000}?"(?:product_id|productId)"\s*:\s*"?(\d{18,20})"?/g;
    let match = adThenProduct.exec(source);
    while (match) {
      push(match[1], match[2]);
      match = adThenProduct.exec(source);
    }
    const productKey = /"(?:product_id|productId|goods_id|goodsId)"\s*:\s*"?(\d{18,20})"?/g;
    match = productKey.exec(source);
    while (match) {
      const start = Math.max(0, match.index - 2200);
      const windowText = source.slice(start, match.index);
      const ids = [...windowText.matchAll(/"(?:id|ad_id|adId)"\s*:\s*"?(\d{15,17})"?/g)];
      if (ids.length) push(ids[ids.length - 1][1], match[1]);
      match = productKey.exec(source);
    }
    return pairs;
  }

  function publish(text) {
    try {
      remember(pairsFromText(String(text || "")));
    } catch (_) {}
  }

  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function () {
      const result = origFetch.apply(this, arguments);
      if (result && typeof result.then === "function") {
        result.then((response) => {
          try {
            response.clone().text().then(publish).catch(() => {});
          } catch (_) {}
        }).catch(() => {});
      }
      return result;
    };
  }

  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener("load", function () {
      let text = "";
      try {
        if (typeof this.responseText === "string") text = this.responseText;
      } catch (_) {}
      if (!text && this.response && typeof this.response !== "string") {
        try { text = JSON.stringify(this.response); } catch (_) {}
      } else if (!text && typeof this.response === "string") {
        text = this.response;
      }
      if (text) publish(text);
    });
    return origSend.apply(this, arguments);
  };

  function pageDay(offset) {
    const date = new Date();
    date.setDate(date.getDate() + offset);
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${date.getFullYear()}-${month}-${day}`;
  }

  function pageRange() {
    const params = new URLSearchParams(location.search);
    const parts = String(params.get("dr") || "").split(",");
    const start = /^\d{4}-\d{2}-\d{2}$/.test(parts[0] || "") ? parts[0] : pageDay(0);
    const end = /^\d{4}-\d{2}-\d{2}$/.test(parts[1] || "") ? parts[1] : start;
    return { aavid: params.get("aavid") || "", start, end };
  }

  function csrfToken() {
    const matched = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
    return matched ? decodeURIComponent(matched[1]) : "";
  }

  function listBody(start, end, page) {
    return {
      UseNewChain: true,
      Params: {
        AdFilter: {
          AdlabScene: 0,
          MarGoal: 1,
          UniPromShopFilter: { UniPromShopView: 1 },
          NotInEcpAdStatuses: ["delete", "frozen"],
          DataTimeRange: {
            StartTime: `${start} 00:00:00`,
            EndTime: `${end} 23:59:59`,
          },
        },
        Metrics: [
          "stat_cost_for_roi2",
          "total_pay_order_count_for_roi2",
          "total_pay_order_gmv_for_roi2",
          "total_prepay_and_pay_order_roi2",
          "total_order_settle_amount_for_roi2_1h",
          "total_prepay_and_pay_settle_roi2_1h",
          "total_refund_order_gmv_for_roi2_1h_rate",
        ],
        OrderBy: { Type: 2, Field: "stat_cost_for_roi2" },
        NeedRequestOptional: true,
        ListAdsModules: [10, 4, 20, 36, 7, 8],
        SophonxDataSetKey: "product_roi2_promotion",
        PageParams: { Page: page, PageSize: 100 },
      },
    };
  }

  let loadingGoods = false;
  let loadedKey = "";

  async function loadPlanGoods() {
    if (!location.pathname.startsWith("/uni-prom")) return;
    const range = pageRange();
    if (!range.aavid || loadingGoods) return;
    const key = `${range.aavid}:${range.start}:${range.end}`;
    if (loadedKey === key) return;
    loadingGoods = true;
    try {
      const headers = { accept: "application/json, text/plain, */*", "content-type": "application/json" };
      const csrf = csrfToken();
      if (csrf) headers["x-csrftoken"] = csrf;
      let page = 1;
      let pages = 1;
      let found = false;
      while (page <= pages && page <= 5) {
        const response = await window.fetch(
          `https://qianchuan.jinritemai.com/ad/api/pmc/v1/uni-promotion/ad/list-required?aavid=${encodeURIComponent(range.aavid)}`,
          { method: "POST", credentials: "include", headers, body: JSON.stringify(listBody(range.start, range.end, page)) }
        );
        const text = await response.text();
        const status = text.match(/"status_code"\s*:\s*"?(\d+)"?/);
        if (!response.ok || (status && status[1] !== "0")) break;
        const pairs = pairsFromText(text);
        if (pairs.length) {
          remember(pairs);
          found = true;
        }
        const total = text.match(/"totalPage"\s*:\s*"?(\d+)"?/);
        pages = total ? Math.min(Number(total[1]) || 1, 5) : 1;
        page += 1;
      }
      if (found) loadedKey = key;
    } catch (_) {}
    loadingGoods = false;
  }

  document.addEventListener("qc-fangzhou-want-products", () => {
    loadPlanGoods();
  });
})();
