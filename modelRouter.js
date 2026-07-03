'use strict';

const config = require('./config');

class ModelResolutionError extends Error {}

// Resolves a client-requested model name into:
//   { baseUrl, apiKey, targetModel, backendName }
// Priority:
//   1. Exact match in config/models.json  -> mapped backend + target model name
//   2. Fallback to DEFAULT_BACKEND / DEFAULT_BASE_URL with the model name passed through unchanged
function resolveModel(requestedModel) {
  const mapping = config.models[requestedModel];

  if (mapping) {
    const backend = config.backends[mapping.backend];
    if (!backend) {
      throw new ModelResolutionError(
        `Model "${requestedModel}" references unknown backend "${mapping.backend}" in models.json`
      );
    }
    return {
      backendName: mapping.backend,
      baseUrl: backend.baseUrl,
      apiKey: backend.apiKey,
      targetModel: mapping.target || requestedModel,
    };
  }

  // Fallback: transparent passthrough to the default backend, unmapped model name.
  if (config.defaultBaseUrl) {
    let apiKey = '';
    let backendName = config.defaultBackendName || 'default';

    // If the default backend name matches a configured backend, reuse its key.
    if (config.defaultBackendName && config.backends[config.defaultBackendName]) {
      apiKey = config.backends[config.defaultBackendName].apiKey;
    } else if (config.defaultBackendName === 'cloud') {
      apiKey = process.env.NVIDIA_API_KEY || '';
    }

    return {
      backendName,
      baseUrl: config.defaultBaseUrl,
      apiKey,
      targetModel: requestedModel,
    };
  }

  throw new ModelResolutionError(
    `Model "${requestedModel}" is not registered in config/models.json and no DEFAULT_BASE_URL is set.`
  );
}

function listModels() {
  return Object.keys(config.models).map((alias) => ({
    id: alias,
    object: 'model',
    owned_by: config.models[alias].backend,
  }));
}

module.exports = { resolveModel, listModels, ModelResolutionError };
