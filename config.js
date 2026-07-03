'use strict';

require('dotenv').config();
const fs = require('fs');
const path = require('path');

const CONFIG_DIR = process.env.CONFIG_DIR || path.join(__dirname, '..', 'config');

function loadJson(fileName, fallback) {
  const filePath = path.join(CONFIG_DIR, fileName);
  try {
    const raw = fs.readFileSync(filePath, 'utf8');
    return JSON.parse(raw);
  } catch (err) {
    if (err.code === 'ENOENT' && fallback !== undefined) return fallback;
    throw new Error(`Failed to load config file ${filePath}: ${err.message}`);
  }
}

const backends = loadJson('backends.json', {});
const models = loadJson('models.json', {});
const clients = loadJson('clients.json', []);

// Resolve each backend's actual API key from the env var it references.
for (const [name, backend] of Object.entries(backends)) {
  backend.apiKey = backend.apiKeyEnv ? process.env[backend.apiKeyEnv] || '' : '';
  if (!backend.baseUrl) {
    throw new Error(`Backend "${name}" is missing a baseUrl in backends.json`);
  }
}

const config = {
  port: parseInt(process.env.PORT || '3000', 10),
  logLevel: process.env.LOG_LEVEL || 'info',
  logFormat: process.env.LOG_FORMAT || 'pretty',

  defaultBackendName: process.env.DEFAULT_BACKEND || null,
  defaultBaseUrl: process.env.DEFAULT_BASE_URL || null,

  retry: {
    maxAttempts: parseInt(process.env.RETRY_MAX_ATTEMPTS || '3', 10),
    baseDelayMs: parseInt(process.env.RETRY_BASE_DELAY_MS || '300', 10),
    maxDelayMs: parseInt(process.env.RETRY_MAX_DELAY_MS || '4000', 10),
  },

  upstreamTimeoutMs: parseInt(process.env.UPSTREAM_TIMEOUT_MS || '60000', 10),

  backends,
  models,
  clients,
};

// Build a quick lookup of client API keys -> client record.
config.clientsByKey = new Map(config.clients.map((c) => [c.apiKey, c]));

module.exports = config;
