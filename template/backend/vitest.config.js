const { defineConfig } = require('vitest/config');

module.exports = defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.{test,spec}.{js,jsx,ts,tsx}'],
    // tests/_example.test.js 是给模型看的**写法示例**，不是真实测试。
    // 它被 include 的 glob 命中，所以必须显式排除，否则裸跑 `vitest run` 会执行它。
    exclude: ['test-e2e/**/*', 'tests/_example.test.js'],
  },
});
