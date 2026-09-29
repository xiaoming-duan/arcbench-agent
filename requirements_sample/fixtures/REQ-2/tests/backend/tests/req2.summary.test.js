'use strict';

// REQ-2 / REQ-2-SCN-1
//   GIVEN 库存中有 3 个条目，数量分别为 12 / 4 / 27
//   WHEN  调用 summarizeItems()
//   THEN  返回 total=3, totalQuantity=43, lowStock=1

const test = require('node:test');
const assert = require('node:assert');

test('REQ-2 summarizeItems() 汇总总数/总量/低库存数', () => {
  const { summarizeItems } = require('../src/services/summary');
  const summary = summarizeItems();

  assert.strictEqual(summary.total, 3, 'total 应等于条目数');
  assert.strictEqual(summary.totalQuantity, 43, 'totalQuantity 应为 12+4+27');
  assert.strictEqual(summary.lowStock, 1, 'lowStock 应只统计低于阈值的条目');
});

test('REQ-2 summarizeItems() 复用 REQ-1 的数据源', () => {
  const { listItems } = require('../src/services/items');
  const { summarizeItems } = require('../src/services/summary');

  assert.strictEqual(summarizeItems().total, listItems().length);
});
