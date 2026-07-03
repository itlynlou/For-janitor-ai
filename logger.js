'use strict';

const pino = require('pino');
const config = require('./config');

const options = { level: config.logLevel };

if (config.logFormat === 'pretty') {
  options.transport = {
    target: 'pino-pretty',
    options: { colorize: true, translateTime: 'HH:MM:ss', ignore: 'pid,hostname' },
  };
}

const logger = pino(options);

module.exports = logger;
