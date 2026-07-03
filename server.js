'use strict';

const express = require('express');
const pinoHttp = require('pino-http');
const { v4: uuidv4 } = require('uuid');

const config = require('./config');
const logger = require('./logger');
const { authenticate, authorizeModel } = require('./auth');
const { makeProxyHandler } = require('./proxyHandler');
const { listModels } = require('./modelRouter');

const app = express();

app.use(express.json({ limit: '25mb' }));
app.use(
  pinoHttp({
    logger,
    genReqId: () => uuidv4(),
    autoLogging: { ignore: (req) => req.url === '/healthz' },
  })
);

app.get('/healthz', (req, res) => res.json({ status: 'ok' }));

// OpenAI-compatible model listing (aggregated from config/models.json).
app.get('/v1/models', authenticate, (req, res) => {
  res.json({ object: 'list', data: listModels() });
});

app.post('/v1/chat/completions', authenticate, authorizeModel, makeProxyHandler('/chat/completions'));
app.post('/v1/completions', authenticate, authorizeModel, makeProxyHandler('/completions'));
app.post('/v1/embeddings', authenticate, authorizeModel, makeProxyHandler('/embeddings'));

// Fallback 404 in OpenAI's error shape.
app.use((req, res) => {
  res.status(404).json({
    error: {
      message: `Unknown route: ${req.method} ${req.originalUrl}`,
      type: 'invalid_request_error',
      param: null,
      code: 'not_found',
    },
  });
});

// Central error handler.
app.use((err, req, res, next) => { // eslint-disable-line no-unused-vars
  logger.error({ err: err.message, stack: err.stack }, 'Unhandled error');
  res.status(500).json({
    error: {
      message: 'Internal proxy error.',
      type: 'internal_error',
      param: null,
      code: 'internal_error',
    },
  });
});

app.listen(config.port, () => {
  logger.info(
    {
      port: config.port,
      backends: Object.keys(config.backends),
      models: Object.keys(config.models),
      clients: config.clients.length,
    },
    `NIM-to-OpenAI proxy listening on port ${config.port}`
  );
});
