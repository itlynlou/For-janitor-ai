'use strict';

const config = require('./config');

function openAiError(message, type, code, status) {
  return {
    status,
    body: {
      error: {
        message,
        type,
        param: null,
        code: code || null,
      },
    },
  };
}

function authenticate(req, res, next) {
  const header = req.headers['authorization'] || '';
  const match = header.match(/^Bearer\s+(.+)$/i);

  if (!match) {
    const err = openAiError(
      'You didn\'t provide an API key. Provide one via the Authorization: Bearer <key> header.',
      'invalid_request_error',
      'missing_api_key',
      401
    );
    return res.status(err.status).json(err.body);
  }

  const apiKey = match[1].trim();
  const client = config.clientsByKey.get(apiKey);

  if (!client) {
    const err = openAiError('Incorrect API key provided.', 'invalid_request_error', 'invalid_api_key', 401);
    return res.status(err.status).json(err.body);
  }

  req.client = client;
  next();
}

// Confirms the authenticated client is allowed to use the requested model alias.
function authorizeModel(req, res, next) {
  const requestedModel = req.body && req.body.model;
  const client = req.client;

  if (!requestedModel) {
    const err = openAiError('You must provide a "model" field.', 'invalid_request_error', 'missing_model', 400);
    return res.status(err.status).json(err.body);
  }

  const allowed = client.allowedModels || [];
  const isAllowed = allowed.includes('*') || allowed.includes(requestedModel);

  if (!isAllowed) {
    const err = openAiError(
      `Client "${client.name}" is not permitted to use model "${requestedModel}".`,
      'invalid_request_error',
      'model_not_allowed',
      403
    );
    return res.status(err.status).json(err.body);
  }

  next();
}

module.exports = { authenticate, authorizeModel, openAiError };
