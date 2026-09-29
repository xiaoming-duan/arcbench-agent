'use strict';

// REQ-1 / REQ-1-SCN-1
//   GIVEN 库存中存在若干条目
//   WHEN  调用 listItems()
//   THEN  返回非空数组且字段完整
//
// 说明：本文件使用 Node 内置测试运行器（node --test），零外部依赖。
//       vitest 方言由 LLMGenerator 在真实环境中产出。

const test = require('node:test');
const assert = require('node:assert');

test('REQ-1 listItems() 返回非空条目数组', () => {
  const { listItems } = require('../src/services/items');
  const items = listItems();
  assert.ok(Array.isArray(items), 'listItems() 必须返回数组');
  assert.ok(items.length > 0, '库存条目不应为空');
});

test('REQ-1 条目字段完整且 quantity 为非负整数', () => {
  const { listItems } = require('../src/services/items');
  for (const item of listItems()) {
    assert.strictEqual(typeof item.id, 'number', 'id 必须是数字');
    assert.strictEqual(typeof item.sku, 'string', 'sku 必须是字符串');
    assert.strictEqual(typeof item.name, 'string', 'name 必须是字符串');
    assert.ok(Number.isInteger(item.quantity), 'quantity 必须是整数');
    assert.ok(item.quantity >= 0, 'quantity 必须非负');
  }
});
