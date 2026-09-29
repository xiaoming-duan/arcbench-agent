'use strict';

// REQ-1.API.Items —— GET /api/items

const express = require('express');
const { listItems } = require('../services/items');

const router = express.Router();

router.get('/', (req, res) => {
  res.json({ code: 200, data: listItems() });
});

module.exports = router;
