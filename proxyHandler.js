'use strict';

const { Readable } = require('stream');
const { v4: uuidv4 } = require('uuid');
const logger = require('./logger');
const { resolveModel, ModelResolutionError } = require('./modelRouter');
const { retryFetch } = require('./retryFetch');
const { openAiError } = require('./auth');

// endpointPath: e.g. '/chat/completions', '/completions', '/embeddings'
function makeProxyHandler(endpointPath) {
  return async function proxyHandler(req, res) {
    const reqId = uuidv4();
    const start = Date.now();
    const client = req.client;
    const requestedModel = req.body.model;
    const isStreaming = Boolean(req.body.stream);

    let resolved;
    try {
      resolved = resolveModel(requestedModel);
    } catch (err) {
      if (err instanceof ModelResolutionError) {
        const e = openAiError(err.message, 'invalid_request_error', 'model_not_found', 404);
        return res.status(e.status).json(e.body);
      }
      throw err;
    }

    const { baseUrl, apiKey, targetModel, backendName } = resolved;
    const upstreamUrl = `${baseUrl.replace(/\/+$/, '')}${endpointPath}`;

    const upstreamBody = { ...req.body, model: targetModel };

    const headers = {
      'Content-Type': 'application/json',
    };
    if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`;
    if (isStreaming) headers['Accept'] = 'text/event-stream';

    const baseLog = {
      reqId,
      client: client.name,
      requestedModel,
      targetModel,
      backendName,
      endpoint: endpointPath,
      streaming: isStreaming,
    };

    logger.info(baseLog, 'Forwarding request to upstream');

    let upstreamResponse;
    try {
      upstreamResponse = await retryFetch(
        upstreamUrl,
        { method: 'POST', headers, body: JSON.stringify(upstreamBody) },
        { reqId }
      );
    } catch (err) {
      logger.error({ ...baseLog, err: err.message, durationMs: Date.now() - start }, 'Upstream request failed');
      const e = openAiError(
        `Failed to reach upstream NIM backend: ${err.message}`,
        'upstream_error',
        'upstream_unreachable',
        502
      );
      return res.status(e.status).json(e.body);
    }

    // Non-OK responses: relay the upstream error body/status where possible.
    if (!upstreamResponse.ok && !isStreaming) {
      const durationMs = Date.now() - start;
      let bodyText;
      try {
        bodyText = await upstreamResponse.text();
      } catch (_) {
        bodyText = '';
      }
      logger.warn(
        { ...baseLog, status: upstreamResponse.status, durationMs, upstreamBody: bodyText.slice(0, 500) },
        'Upstream returned an error response'
      );
      res.status(upstreamResponse.status);
      try {
        return res.json(JSON.parse(bodyText));
      } catch (_) {
        return res.type('text/plain').send(bodyText);
      }
    }

    if (isStreaming) {
      if (!upstreamResponse.ok) {
        const durationMs = Date.now() - start;
        let bodyText;
        try {
          bodyText = await upstreamResponse.text();
        } catch (_) {
          bodyText = '';
        }
        logger.warn(
          { ...baseLog, status: upstreamResponse.status, durationMs },
          'Upstream returned an error response before streaming started'
        );
        res.status(upstreamResponse.status);
        try {
          return res.json(JSON.parse(bodyText));
        } catch (_) {
          return res.type('text/plain').send(bodyText);
        }
      }

      res.status(200);
      res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
      res.setHeader('Cache-Control', 'no-cache, no-transform');
      res.setHeader('Connection', 'keep-alive');
      res.flushHeaders && res.flushHeaders();

      let chunkCount = 0;
      let bytesStreamed = 0;
      const nodeStream = Readable.fromWeb(upstreamResponse.body);

      nodeStream.on('data', (chunk) => {
        chunkCount += 1;
        bytesStreamed += chunk.length;
      });

      nodeStream.on('error', (err) => {
        logger.error({ ...baseLog, err: err.message, durationMs: Date.now() - start }, 'Error while streaming from upstream');
        if (!res.writableEnded) res.end();
      });

      req.on('close', () => {
        if (!res.writableEnded) nodeStream.destroy();
      });

      nodeStream.on('end', () => {
        logger.info(
          { ...baseLog, durationMs: Date.now() - start, chunkCount, bytesStreamed },
          'Streaming response complete'
        );
      });

      return nodeStream.pipe(res);
    }

    // Non-streaming success path.
    let json;
    try {
      json = await upstreamResponse.json();
    } catch (err) {
      logger.error({ ...baseLog, err: err.message }, 'Failed to parse upstream JSON response');
      const e = openAiError('Upstream returned an invalid response.', 'upstream_error', 'invalid_upstream_response', 502);
      return res.status(e.status).json(e.body);
    }

    const durationMs = Date.now() - start;
    logger.info(
      { ...baseLog, durationMs, usage: json.usage || null, status: upstreamResponse.status },
      'Request complete'
    );

    // Report the alias the client asked for, not the internal upstream target name.
    if (json && typeof json === 'object' && 'model' in json) {
      json.model = requestedModel;
    }

    return res.status(upstreamResponse.status).json(json);
  };
}

module.exports = { makeProxyHandler };
