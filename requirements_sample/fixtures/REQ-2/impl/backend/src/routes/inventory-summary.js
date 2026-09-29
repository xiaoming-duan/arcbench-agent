'use strict';

// REQ-2.API.Summary —— GET /api/inventory/summary

const express = require('express');
const { summarizeItems } = require('../services/summary');

const router = express.Router();

router.get('/summary', (req, res) => {
  res.json({ code: 200, data: summarizeItems() });
});

module.exports = router;
