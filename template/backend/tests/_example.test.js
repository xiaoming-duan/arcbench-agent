// =============================================================================
// 示例测试文件 —— 仅供模型模仿写法，**不会被收集执行**。
// -----------------------------------------------------------------------------
// 为什么放在这里：实测模型会模仿项目里已有文件的风格。把「正确的 ESM vitest
// 写法」放在 tests/ 下，比在 prompt 里用文字描述更有效。
//
// 为什么不会被收集：它在 template/backend/vitest.config.js 的 test.exclude 里
// 被显式排除（`tests/_example.test.js`）。否则它会被 include 的
// `tests/**/*.{test,spec}.*` 命中，在裸跑 `vitest run` 时被当作真实测试执行。
//
// 要点（照抄这些）：
//   1. 用 ESM `import` 引 vitest —— 不要用 CommonJS 的 require 引它
//      （vitest 是纯 ESM 包，CJS require 会抛
//      "Vitest cannot be imported in a CommonJS module"）。
//      本文件刻意不写出那个被禁的写法字面量，以免静态检查误伤。
//   2. 引用实现用相对路径 `../src/...`（测试在 tests/，实现在 src/，只退一层；
//      写成 `../../src/...` 会退到项目根，审计会判 NO_IMPLEMENTATION_IMPORT）。
//   3. 保持精简：单个文件 ≤ 200 行，不要重建整库 schema/seed。
// =============================================================================

import { describe, it, expect } from 'vitest';

// 真实测试应当 import 被测实现，例如：
//   import { summarizeItems } from '../src/services/summary.js';
// 这里为了保持示例自包含、可安全排除，只演示结构。

describe('example', () => {
  it('演示 ESM 写法', () => {
    expect(1 + 1).toBe(2);
  });
});
