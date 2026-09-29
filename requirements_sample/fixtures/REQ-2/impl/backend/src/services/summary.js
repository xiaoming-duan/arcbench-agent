'use strict';

// REQ-2.SVC.Summary —— 基于 REQ-1 的数据源做汇总

const { listItems } = require('./items');

const LOW_STOCK_THRESHOLD = 10;

function summarizeItems() {
  const items = listItems();
  return {
    total: items.length,
    totalQuantity: items.reduce((sum, item) => sum + item.quantity, 0),
    lowStock: items.filter((item) => item.quantity < LOW_STOCK_THRESHOLD).length,
  };
}

module.exports = { LOW_STOCK_THRESHOLD, summarizeItems };
