// Embedded satellite manager.
//
// Sunday.app spawns the device satellite as a *child process* so macOS
// attributes its TCC grants (Screen Recording, etc.) to "Sunday" — not to
// a standalone "Python" LaunchAgent, which is the grant-to-Python UX we're
// trying to kill. A child of a signed, hardened app is covered by the
// app's permission entries.
//
// Lifecycle: spawn on app ready, restart on crash with backoff, kill on
// quit. The command is resolved from prefs/env with a sensible default so
// dev machines Just Work; packaged builds can point at a bundled runtime.

const { spawn } = require('node:child_process');
const fs   = require('node:fs');
const os   = require('node:os');
const path = require('node:path');

let child = null;
let stopping = false;
let restartTimer = null;
let backoffMs = 1000;

// Resolve {command, args, cwd} for the satellite. Priority:
//   1. prefs.satelliteCmd (array) — explicit override from settings
//   2. SUNDAY_SATELLITE_CMD env (space-separated)
//   3. a sunday-satellite console script next to a known venv python
// The server URL is derived from the daemon HTTP/WS prefs; device id
// defaults to the machine hostname.
function resolveSatellite(prefs) {
  const home = os.homedir();
  const deviceId = prefs.deviceId || os.hostname().replace(/\.local$/, '');
  const server = deviceWsFromPrefs(prefs);

  // Candidate launchers, first existing wins.
  const candidates = [];
  if (Array.isArray(prefs.satelliteCmd) && prefs.satelliteCmd.length) {
    candidates.push(prefs.satelliteCmd);
  }
  if (process.env.SUNDAY_SATELLITE_CMD) {
    candidates.push(process.env.SUNDAY_SATELLITE_CMD.split(/\s+/));
  }
  // Known dev venv console script + a couple of common spots.
  for (const base of [
    path.join(home, 'Development', 'Repos', 'sunday', '.venv', 'bin'),
    path.join(home, '.sunday', 'venv', 'bin'),
    '/opt/sunday/.venv/bin',
  ]) {
    candidates.push([path.join(base, 'sunday-satellite')]);
  }

  for (const c of candidates) {
    if (c[0] && (c[0].includes('/') ? fs.existsSync(c[0]) : true)) {
      return {
        command: c[0],
        args: c.slice(1).concat(['--server', server, '--device-id', deviceId]),
        cwd: home,
      };
    }
  }
  return null;
}

function deviceWsFromPrefs(prefs) {
  // Prefer an explicit devices WS; else derive from daemonHttp/daemonWs.
  if (prefs.devicesWs) return prefs.devicesWs;
  const http = prefs.daemonHttp || 'http://127.0.0.1:8765';
  const wsBase = http.replace(/^http/, 'ws').replace(/\/+$/, '');
  return `${wsBase}/v1/devices/ws`;
}

function logLine(logPath, line) {
  try { fs.appendFileSync(logPath, line.endsWith('\n') ? line : line + '\n'); }
  catch { /* best effort */ }
}

function start(prefs) {
  stopping = false;
  const resolved = resolveSatellite(prefs);
  const logPath = path.join(os.homedir(), '.sunday', 'logs', 'satellite-embedded.log');
  fs.mkdirSync(path.dirname(logPath), { recursive: true });

  if (!resolved) {
    logLine(logPath, `[satellite] could not resolve a satellite command — set prefs.satelliteCmd or SUNDAY_SATELLITE_CMD`);
    return false;
  }
  logLine(logPath, `[satellite] spawning ${resolved.command} ${resolved.args.join(' ')}`);
  child = spawn(resolved.command, resolved.args, {
    cwd: resolved.cwd,
    env: { ...process.env, PATH: `/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:${process.env.PATH || ''}` },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (d) => logLine(logPath, `[out] ${d.toString().trimEnd()}`));
  child.stderr.on('data', (d) => logLine(logPath, `[err] ${d.toString().trimEnd()}`));
  child.on('exit', (code, signal) => {
    logLine(logPath, `[satellite] exited code=${code} signal=${signal}`);
    child = null;
    if (stopping) return;
    // Restart with capped backoff.
    restartTimer = setTimeout(() => { backoffMs = Math.min(backoffMs * 2, 30000); start(prefs); }, backoffMs);
  });
  // Reset backoff once it's lived a little.
  setTimeout(() => { if (child) backoffMs = 1000; }, 15000);
  return true;
}

function stop() {
  stopping = true;
  if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
  if (child) {
    try { child.kill('SIGTERM'); } catch { /* ignore */ }
    child = null;
  }
}

function isRunning() {
  return !!child;
}

module.exports = { start, stop, isRunning, resolveSatellite };
