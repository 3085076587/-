/** 预估利润：填了实际退款率就扣这个比例，没填按 0。结果始终叫预估利润。 */
const QcProfit = globalThis.QcProfit = (() => {
  const RATE_KEY = "qc_product_refund_v1";
  function isFilled(value) {
    return value !== null && value !== undefined && String(value).trim() !== "";
  }

  function rateRatio(value) {
    if (!isFilled(value)) return null;
    let num = Number(String(value).replace("%", "").trim());
    if (!Number.isFinite(num) || num < 0) return null;
    if (num > 1) num /= 100;
    if (num > 1) return null;
    return num;
  }

  function commissionPct(value) {
    const pct = Number(value);
    if (!Number.isFinite(pct) || pct <= 0 || pct > 100) return null;
    return pct;
  }

  function lineProfit(settle, cost, pct, rateRatioValue) {
    const settleNum = Number(settle);
    const costNum = Number(cost);
    const rate = rateRatioValue == null ? 0 : rateRatioValue;
    if (!Number.isFinite(settleNum) || !Number.isFinite(costNum) || pct == null) return null;
    if (rate < 0 || rate > 1) return null;
    return settleNum * (1 - rate) * (pct / 100) - costNum;
  }

  function planEstimate(plan, rateText) {
    const pct = commissionPct(plan.commission_pct);
    let rate = 0;
    if (isFilled(rateText)) {
      rate = rateRatio(rateText);
      if (rate == null) return null;
    }
    return lineProfit(plan.settle_amount, plan.stat_cost, pct, rate);
  }

  function sumPlanLines(plans, rateFor) {
    let estimate = 0;
    let hasEstimate = false;
    (plans || []).forEach((plan) => {
      const value = planEstimate(plan, rateFor(plan));
      if (value != null) {
        estimate += value;
        hasEstimate = true;
      }
    });
    return hasEstimate ? estimate : null;
  }

  function displayRefund(value) {
    const ratio = rateRatio(value);
    if (ratio == null && !isFilled(value)) return null;
    if (ratio == null) return null;
    return Math.min(1, ratio + REFUND_EXTRA);
  }

  function moneyText(value) {
    if (value == null || !Number.isFinite(Number(value))) return "-";
    return Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function roiText(value) {
    if (value == null || value === "" || !Number.isFinite(Number(value))) return "-";
    return Number(value).toFixed(2);
  }

  function rateText(value) {
    const shown = displayRefund(value);
    if (shown == null) return "-";
    return `${(shown * 100).toFixed(2)}%`;
  }

  function profitRateText(profit, settle) {
    const settleNum = Number(settle);
    if (profit == null || !Number.isFinite(settleNum) || settleNum === 0) return "-";
    return `${((profit / settleNum) * 100).toFixed(2)}%`;
  }

  function loadRates() {
    return new Promise((resolve) => {
      chrome.storage.local.get(RATE_KEY, (data) => {
        const rates = data && data[RATE_KEY];
        resolve(rates && typeof rates === "object" ? rates : {});
      });
    });
  }

  function saveRates(rates) {
    return new Promise((resolve) => {
      chrome.storage.local.set({ [RATE_KEY]: rates }, () => resolve(rates));
    });
  }

  function todayText() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${now.getFullYear()}-${month}-${day}`;
  }

  function shiftDate(days) {
    const now = new Date();
    now.setDate(now.getDate() - days);
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${now.getFullYear()}-${month}-${day}`;
  }

  return {
    isFilled,
    planEstimate,
    sumPlanLines,
    moneyText,
    roiText,
    rateText,
    profitRateText,
    loadRates,
    saveRates,
    todayText,
    shiftDate,
  };
})();
