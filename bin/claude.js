#!/usr/bin/env node
const { spawn } = require('child_process');
const path = require('path');

const scriptPath = path.join(__dirname, '../claude_attach.py');
const args = process.argv.slice(2);

const child = spawn('python', [scriptPath, ...args], { stdio: 'inherit' });
child.on('exit', (code) => {
  process.exit(code ?? 0);
});
