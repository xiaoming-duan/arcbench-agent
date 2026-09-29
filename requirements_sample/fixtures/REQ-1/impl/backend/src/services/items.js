'use strict';

// REQ-1.SVC.Items —— 库存条目只读数据源

const SEED_ITEMS = [
  { id: 1, sku: 'SKU-1001', name: '机械键盘', quantity: 12 },
  { id: 2, sku: 'SKU-1002', name: '人体工学椅', quantity: 4 },
  { id: 3, sku: 'SKU-1003', name: '显示器支架', quantity: 27 },
];

function listItems() {
  // 返回副本，避免调用方污染种子数据
  return SEED_ITEMS.map((item) => ({ ...item }));
}

module.exports = { SEED_ITEMS, listItems };
