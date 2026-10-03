const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');
const app = express();

// route modules imports

// middleware imports
app.use(cors());
app.use(bodyParser.json());

// initialize database
const { initializeDatabase } = require('./database/init_db');
initializeDatabase().catch((error) => {
  console.error('Database initialization failed:', error);
});

// register routes
// ★就绪探测面要够宽：平台的模板服务在启动后会等「就绪」，
//   实测报错是「template application server did not become ready within
//   120 seconds」。探测路径我们**无法预知**，所以把常见几个都提供出来：
//   少一个 200，就可能换来一次 120 秒超时（而且看不出是路径不对）。
const readyPayload = { code: 200, message: 'Backend Ready' };
app.get('/api/health', (req, res) => res.json(readyPayload));
app.get('/health', (req, res) => res.json(readyPayload));
app.get('/healthz', (req, res) => res.json(readyPayload));

const frontendDistPath = path.resolve(__dirname, '../../frontend/dist');

if (fs.existsSync(frontendDistPath)) {
  app.use(express.static(frontendDistPath));

  // Keep API routes on the backend and serve the SPA for all other GET requests.
  app.get(/^(?!\/api(?:\/|$)).*/, (req, res) => {
    res.sendFile(path.join(frontendDistPath, 'index.html'));
  });
} else {
  // ★这里**曾经是 503**。问题在于「就绪」探测通常只认 2xx：
  //   一旦 frontend/dist 没构建出来，`/` 会**永远**回 503，
  //   探测就永远不就绪 -> 120 秒超时，而真正的原因（前端没构建）
  //   被完全掩盖。改成 200 后，探测能通过，问题会暴露在**基准测试**里
  //   （那才是该暴露它的地方）；页面内容仍然如实说明构建缺失。
  app.get('/', (req, res) => {
    res
      .status(200)
      .type('html')
      .send(`<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Frontend Build Missing</title>
    <style>
      body {
        margin: 0;
        font-family: ui-sans-serif, system-ui, sans-serif;
        background: #f6f7f9;
        color: #1f2937;
      }
      main {
        max-width: 720px;
        margin: 12vh auto 0;
        padding: 24px;
      }
      section {
        background: #fff;
        border: 1px solid #d1d5db;
        border-radius: 12px;
        padding: 24px;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
      }
      h1 {
        margin-top: 0;
      }
      code {
        background: #f3f4f6;
        padding: 2px 6px;
        border-radius: 6px;
      }
    </style>
  </head>
  <body>
    <main>
      <section>
        <h1>Frontend build missing</h1>
        <p>The backend is running, but <code>frontend/dist</code> is not available yet.</p>
        <p>Build the frontend first, then start the backend so it can host the compiled site on the same port.</p>
      </section>
    </main>
  </body>
</html>`);
  });
}

module.exports = app;
