const { defineConfig } = require('@playwright/test');

// 与平台基准测试对齐：上游为 TARGET_URL || PLAYWRIGHT_BASE_URL || :3301
const baseURL = process.env.TARGET_URL || process.env.PLAYWRIGHT_BASE_URL
  || process.env.ARC_WEB_BASE_URL || 'http://127.0.0.1:3301';

module.exports = defineConfig({
  testDir: './test-e2e',
  testMatch: /.*\.(js|jsx|ts|tsx)$/,
  timeout: 30000,
  use: {
    baseURL,
    trace: 'retain-on-failure',
  },
    // ★ webServer：E2E 需要一个**跑起来的应用**才有东西可测。
    //   初版配置缺这一段 —— 即使浏览器装好，用例也会因连不上 baseURL 而全红，
    //   且失败原因是「环境」而非「实现」，会污染归因。
    //   reuseExistingServer 让本地已有服务时直接复用，不重复起。
    webServer: {
      command: 'npm start',
      url: baseURL,
      reuseExistingServer: true,
      timeout: 60000,
    },
});
