'use strict';

const config = require('./config');
const logger = require('./logger');

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function backoffDelay(attempt) {
  const { baseDelayMs, maxDelayMs } = config.retry;
  const exp = Math.min(maxDelayMs, baseDelayMs * 2 ** (attempt - 1));
  const jitter = Math.random() * exp * 0.3;
  return Math.floor(exp - exp * 0.15 + jitter);
}

// Retries idempotent-ish upstream calls on network errors, 429, and 5xx.
// NOTE: only call this for the initial (non-streamed) response. Once bytes
// have started flowing to the client we can't safely retry.
async function retryFetch(url, options, { maxAttempts, timeoutMs, reqId } = {}) {
  const attempts = maxAttempts || config.retry.maxAttempts;
  const timeout = timeoutMs || config.upstreamTimeoutMs;

  let lastErr;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);

    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      clearTimeout(timer);

      const shouldRetry = response.status === 429 || response.status >= 500;
      if (shouldRetry && attempt < attempts) {
        const delay = backoffDelay(attempt);
        logger.warn(
          { reqId, attempt, status: response.status, delay },
          'Upstream returned retryable status, backing off'
        );
        await sleep(delay);
        continue;
      }

      return response;
    } catch (err) {
      clearTimeout(timer);
      lastErr = err;
      const isAbort = err.name === 'AbortError';

      if (attempt < attempts) {
        const delay = backoffDelay(attempt);
        logger.warn(
          { reqId, attempt, err: err.message, isAbort, delay },
          'Upstream request failed, retrying'
        );
        await sleep(delay);
        continue;
      }
    }
  }

  throw lastErr || new Error('Upstream request failed after retries');
}

module.exports = { retryFetch };
