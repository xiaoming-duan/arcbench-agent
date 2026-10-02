const app = require('./app');
// ★平台基准测试的 baseURL 是 http://127.0.0.1:3301（见 arc-bench
// 的 playwright.config.ts：TARGET_URL || PLAYWRIGHT_BASE_URL || :3301）。
// 默认 3000 会让就绪探测永远打不到 —— 平台报「application server
// exited before becoming ready」。
const defaultPort = Number(process.env.ARC_RUNTIME_PORT || 3301);
const port = Number(process.env.PORT || defaultPort);

app.listen(port, () => {
  console.log(`Backend listening at http://127.0.0.1:${port}`);
});
