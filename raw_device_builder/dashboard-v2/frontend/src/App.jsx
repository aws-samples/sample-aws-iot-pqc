import React, { useState, useEffect, useCallback } from 'react';
import { CognitoIdentityClient, GetIdCommand, GetCredentialsForIdentityCommand } from '@aws-sdk/client-cognito-identity';
import config from './config.js';
import { LIST_DEVICES, SEND_COMMAND, MIGRATE_FIRMWARE, GET_DEVICE_LOG, GET_BROKER_LOG } from './graphql.js';

// ─── Auth (Cognito unauthenticated → IAM SigV4 for AppSync) ─────────────────

const cognitoClient = new CognitoIdentityClient({ region: config.region });
let cachedCredentials = null;
let credentialsExpiry = 0;

async function getCredentials() {
  if (cachedCredentials && Date.now() < credentialsExpiry) return cachedCredentials;
  const idResp = await cognitoClient.send(new GetIdCommand({ IdentityPoolId: config.identityPoolId }));
  const credsResp = await cognitoClient.send(new GetCredentialsForIdentityCommand({ IdentityId: idResp.IdentityId }));
  cachedCredentials = credsResp.Credentials;
  credentialsExpiry = Date.now() + 50 * 60 * 1000;
  return cachedCredentials;
}

async function appSyncQuery(query, variables = {}) {
  try {
    const creds = await getCredentials();
    const url = new URL(config.appSyncEndpoint);
    const body = JSON.stringify({ query, variables });
    const datetime = new Date().toISOString().replace(/[:-]|\.\d{3}/g, '');
    const date = datetime.substring(0, 8);
    const region = config.region;
    const service = 'appsync';
    const encoder = new TextEncoder();

    async function sha256(data) {
      const buf = await crypto.subtle.digest('SHA-256', typeof data === 'string' ? encoder.encode(data) : data);
      return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
    }
    async function hmacSha256(key, data) {
      const k = await crypto.subtle.importKey('raw', typeof key === 'string' ? encoder.encode(key) : key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
      return new Uint8Array(await crypto.subtle.sign('HMAC', k, encoder.encode(data)));
    }

    const payloadHash = await sha256(body);
    const headers = { 'content-type': 'application/json', 'host': url.host, 'x-amz-date': datetime, 'x-amz-security-token': creds.SessionToken };
    const signedHeaders = Object.keys(headers).sort().join(';');
    const canonicalHeaders = Object.keys(headers).sort().map(k => `${k}:${headers[k]}\n`).join('');
    const canonicalRequest = `POST\n${url.pathname}\n\n${canonicalHeaders}\n${signedHeaders}\n${payloadHash}`;
    const credentialScope = `${date}/${region}/${service}/aws4_request`;
    const stringToSign = `AWS4-HMAC-SHA256\n${datetime}\n${credentialScope}\n${await sha256(canonicalRequest)}`;

    let signingKey = await hmacSha256(`AWS4${creds.SecretKey}`, date);
    signingKey = await hmacSha256(signingKey, region);
    signingKey = await hmacSha256(signingKey, service);
    signingKey = await hmacSha256(signingKey, 'aws4_request');
    const signature = Array.from(await hmacSha256(signingKey, stringToSign)).map(b => b.toString(16).padStart(2, '0')).join('');
    const authorization = `AWS4-HMAC-SHA256 Credential=${creds.AccessKeyId}/${credentialScope}, SignedHeaders=${signedHeaders}, Signature=${signature}`;

    const response = await fetch(config.appSyncEndpoint, { method: 'POST', headers: { ...headers, authorization }, body });
    const result = await response.json();
    if (result.errors) console.error('GraphQL errors:', result.errors);
    return result.data;
  } catch (err) {
    console.error('AppSync query failed:', err);
    return null;
  }
}

// ─── REST API helper (for firmware builds/OTA) ───────────────────────────────

async function apiCall(path, method = 'GET', body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(`${config.apiEndpoint}${path}`, opts);
    return await res.json();
  } catch (err) {
    console.error(`API error: ${err}`);
    return null;
  }
}

// ─── Device Card ─────────────────────────────────────────────────────────────

// Human-readable labels for the live migration status the Step Functions
// workflow writes to the device record (surfaced via the 5s poll).
const MIGRATION_LABELS = {
  building: 'Building PQC firmware…',
  'pushing-ota': 'Pushing OTA…',
  rebooting: 'Rebooting into PQC…',
  failed: 'Migration failed',
  done: 'Migration complete',
};
const MIGRATION_IN_PROGRESS = ['building', 'pushing-ota', 'rebooting'];

// The three firmware variants the operator can flash, in migration order. The
// value is what start-migration expects; the label is what the picker shows.
const VARIANTS = ['classical', 'pqc', 'pqc-mldsa', 'signer', 'verified'];
const VARIANT_LABELS = {
  classical: 'Classical (ECDSA)',
  pqc: 'PQC key exchange (ML-KEM)',
  'pqc-mldsa': 'PQC + ML-DSA-44 cert',
  signer: 'Signed OTA (ML-DSA-44)',
  verified: 'Verified OTA (proof)',
};

// Best-effort "what is the device running now" from live telemetry. The device
// reports firmwareVersion via telemetry. The version is the single canonical
// per-variant string (matches the S3 artifact dir + OTA job): the variant is
// the suffix — 1.0.0-classical / 2.0.0-pqc / 3.0.0-mldsa. Match on the suffix
// (robust to future major-number changes). Used only to pre-select the picker's
// next logical step — the operator can always override, so an unknown/missing
// reading just defaults to "pqc".
function detectVariant(device) {
  const v = (device.firmwareVersion || '').trim().toLowerCase();
  // ML-DSA signer-era variants (Workstream 4). Check these first: they are the
  // highest steps on the path. Suffixes are distinct so order is not critical,
  // but keeping them ahead of the base checks documents the migration order.
  if (v.endsWith('-verified')) return 'verified';
  if (v.endsWith('-signer')) return 'signer';
  if (v.endsWith('-mldsa')) return 'pqc-mldsa';
  if (v.endsWith('-pqc')) return 'pqc';
  if (v.endsWith('-classical')) return 'classical';
  // No firmwareVersion reading yet: fall back to the key-exchange signal.
  const alg = device.pqcAlgorithm || '';
  if (alg.includes('MLKEM') || alg.includes('Kyber') || alg.includes('PQC')) return 'pqc';
  return null;
}

// Pre-select the next step up the migration path from the current variant, so
// the common "advance this device" action is one click.
function nextVariant(current) {
  const idx = VARIANTS.indexOf(current);
  if (idx === -1) return 'pqc';
  return VARIANTS[Math.min(idx + 1, VARIANTS.length - 1)];
}

function DeviceCard({ device, onCommand, onShowLog, onFlash }) {
  const [ledOn, setLedOn] = useState(true);
  const isPqc = device.pqcAlgorithm &&
    (device.pqcAlgorithm.includes('MLKEM') || device.pqcAlgorithm.includes('Kyber') || device.pqcAlgorithm.includes('PQC'));
  // The certificate signer is post-quantum only once it is an ML-DSA cert
  // (v3). PQC key exchange (v2) still uses a classical ECDSA certificate.
  const isPqcCert = device.certSigAlg &&
    (device.certSigAlg.includes('ML-DSA') || device.certSigAlg.includes('MLDSA') || device.certSigAlg.includes('Dilithium'));

  // Firmware integrity badge (OTA firmware-signing). otaSigAlg is the signature
  // era that protected the RUNNING image — distinct from certSigAlg (the TLS
  // *certificate* signature). firmwareSha256 is the device-computed hash of its
  // own running app partition, so the badge ties to the exact image bytes.
  const otaSig = (device.otaSigAlg || '').trim();
  const otaSigIsPqc = otaSig.includes('ML-DSA') || otaSig.includes('MLDSA') || otaSig.includes('Dilithium');
  const otaSigIsClassical = /ecdsa|rsa/i.test(otaSig);
  const otaSigned = otaSigIsPqc || otaSigIsClassical;
  const fwSha = (device.firmwareSha256 || '').trim();
  const fwShaShort = fwSha ? fwSha.slice(0, 12) : '';
  const sigClass = otaSigIsPqc ? 'sig-pqc' : (otaSigIsClassical ? 'sig-classical' : 'sig-unsigned');
  const sigLabel = otaSig
    ? (otaSigIsPqc ? `${otaSig} ✓` : (otaSigIsClassical ? `${otaSig} ✓` : 'unsigned (factory)'))
    : '—';
  const lastSeenDate = device.lastSeen ? new Date(device.lastSeen) : null;
  const isStale = lastSeenDate ? (Date.now() - lastSeenDate.getTime() > 90000) : true;
  const isOnline = device.status === 'online' && !isStale;
  const migrating = MIGRATION_IN_PROGRESS.includes(device.migrationStatus);

  const currentVariant = detectVariant(device);
  const [selectedVariant, setSelectedVariant] = useState(() => nextVariant(currentVariant));

  return (
    <div className={`device-card ${isPqc ? 'pqc' : 'classical'} ${isOnline ? 'online' : 'offline'}`}>
      <div className="device-header">
        <h3>{device.thingName}</h3>
        <span className={`status-dot ${isOnline ? 'online' : 'offline'}`}></span>
        {isPqc && <span className="pqc-badge">PQC</span>}
        {!isPqc && isOnline && <span className="classical-badge">Classical</span>}
      </div>

      <div className="device-metrics">
        <div className="metric">
          <label>Key Exchange</label>
          <span className={isPqc ? 'pqc-text' : 'classical-text'}>{device.pqcAlgorithm || '—'}</span>
        </div>
        <div className="metric">
          <label>Cert Signature</label>
          <span className={isPqcCert ? 'pqc-text' : 'classical-text'}>{device.certSigAlg || '—'}</span>
        </div>
        <div className="metric">
          <label>TLS Version</label>
          <span>{device.tlsVersion || '—'}</span>
        </div>
        <div className="metric">
          <label>Firmware</label>
          <span>{device.firmwareVersion || '—'}</span>
        </div>
        <div className="metric metric-wide">
          <label>Firmware Signature</label>
          <span className={`sig-badge ${sigClass}`} title={fwSha ? `Running image SHA-256: ${fwSha}` : 'No image hash reported'}>
            🔐 {sigLabel}{otaSigned && fwShaShort ? <span className="sig-sha"> · {fwShaShort}…</span> : null}
          </span>
        </div>
        <div className="metric">
          <label>WiFi</label>
          <span>{device.wifiRssi ? `${device.wifiRssi} dBm` : '—'}</span>
        </div>
        <div className="metric">
          <label>Heap free / used / total</label>
          <span>
            {device.freeHeap != null && device.heapTotal != null
              ? `${formatKB(device.freeHeap)} / ${formatKB(device.heapTotal - device.freeHeap)} / ${formatKB(device.heapTotal)}`
              : device.freeHeap != null
                ? `${formatKB(device.freeHeap)} free`
                : '—'}
          </span>
        </div>
        <div className="metric">
          <label>Min heap (low-water)</label>
          <span>{device.minHeap != null ? formatKB(device.minHeap) : '—'}</span>
        </div>
        <div className="metric">
          <label>Max contiguous block</label>
          <span>{device.maxAllocHeap != null ? formatKB(device.maxAllocHeap) : '—'}</span>
        </div>
        <div className="metric">
          <label>Flash free (OTA headroom)</label>
          <span>{device.freeSketchSpace != null ? formatKB(device.freeSketchSpace) : '—'}</span>
        </div>
        <div className="metric">
          <label>App size</label>
          <span>{device.sketchSize != null ? formatKB(device.sketchSize) : '—'}</span>
        </div>
        <div className="metric">
          <label>PSRAM free / total</label>
          <span>
            {device.psramSize != null
              ? (device.psramSize === 0
                  ? 'disabled'
                  : `${formatKB(device.psramFree)} / ${formatKB(device.psramSize)}`)
              : '—'}
          </span>
        </div>
        <div className="metric">
          <label>Temp</label>
          <span>{device.chipTempC ? `${device.chipTempC}°C` : '—'}</span>
        </div>
        <div className="metric">
          <label>Uptime</label>
          <span>{device.uptimeSec ? formatUptime(device.uptimeSec) : '—'}</span>
        </div>
        <div className="metric">
          <label>Messages</label>
          <span>{device.messageId || '0'}</span>
        </div>
      </div>

      <div className="device-actions">
        <button onClick={() => onShowLog(device.thingName)} title="View message log">📋 Logs</button>
        <button onClick={() => { const cmd = ledOn ? 'led-off' : 'led-on'; onCommand(device.thingName, cmd, 'control'); setLedOn(!ledOn); }} title="Toggle LED">
          💡 {ledOn ? 'LED Off' : 'LED On'}
        </button>
      </div>

      <div className="device-flash">
        <div className="flash-current">
          Running: <strong>{currentVariant ? VARIANT_LABELS[currentVariant] : 'unknown'}</strong>
        </div>
        {isOnline && migrating ? (
          <button className="migrate-btn migrating" disabled title="Migration in progress">
            <span className="spinner" aria-hidden="true"></span> {MIGRATION_LABELS[device.migrationStatus] || 'Working…'}
          </button>
        ) : (
          <div className="flash-controls">
            <select
              className="variant-picker"
              value={selectedVariant}
              onChange={(e) => setSelectedVariant(e.target.value)}
              disabled={!isOnline}
              title="Select firmware variant to flash"
            >
              {VARIANTS.map(v => (
                <option key={v} value={v}>
                  {VARIANT_LABELS[v]}{v === currentVariant ? ' (current)' : ''}
                </option>
              ))}
            </select>
            <button
              className="migrate-btn"
              onClick={() => onFlash(device.thingName, selectedVariant)}
              disabled={!isOnline}
              title="Build (if needed) and push the selected firmware via OTA"
            >
              🚀 Flash
            </button>
          </div>
        )}
      </div>
      {device.migrationStatus === 'failed' && device.migrationError && (
        <div className="migration-error" title={device.migrationError}>
          ⚠️ {device.migrationError.length > 120 ? device.migrationError.slice(0, 120) + '…' : device.migrationError}
        </div>
      )}
    </div>
  );
}

// ─── Log Panel ───────────────────────────────────────────────────────────────

function LogPanel({ thingName, onClose }) {
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    async function fetchLogs() {
      const data = await appSyncQuery(GET_DEVICE_LOG, { thingName, limit: 50 });
      if (active && data && data.getDeviceLog) {
        setLogs(data.getDeviceLog);
      }
      setLoading(false);
    }
    fetchLogs();
    const interval = setInterval(fetchLogs, 5000);
    return () => { active = false; clearInterval(interval); };
  }, [thingName]);

  return (
    <div className="log-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="log-panel">
        <div className="log-header">
          <h3>Messages: {thingName}</h3>
          <button onClick={onClose}>✕</button>
        </div>
        <div className="log-body">
          {loading ? <p>Loading...</p> : logs.length === 0 ? <p>No messages yet</p> : (
            <table className="log-table">
              <thead>
                <tr><th>Time</th><th>Dir</th><th>Topic</th><th>Payload</th></tr>
              </thead>
              <tbody>
                {logs.map((log, i) => (
                  <tr key={i} className={log.direction}>
                    <td>{new Date(log.timestamp).toLocaleTimeString()}</td>
                    <td>{log.direction === 'inbound' ? '⬆️' : '⬇️'}</td>
                    <td className="topic">{log.topic}</td>
                    <td className="payload">{truncate(log.payload, 80)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Main App ────────────────────────────────────────────────────────────────

function App() {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [logDevice, setLogDevice] = useState(null);
  const [message, setMessage] = useState('');

  const fetchDevices = useCallback(async () => {
    const data = await appSyncQuery(LIST_DEVICES);
    if (data && data.listDevices) {
      setDevices(data.listDevices);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchDevices();
    const interval = setInterval(fetchDevices, config.pollInterval);
    return () => clearInterval(interval);
  }, [fetchDevices]);

  const handleCommand = async (thingName, action, topicSuffix) => {
    const data = await appSyncQuery(SEND_COMMAND, { thingName, action, topicSuffix });
    if (data && data.sendCommand && data.sendCommand.success) {
      showMessage(`Command '${action}' sent to ${thingName}`);
    } else {
      showMessage(`Failed to send command to ${thingName}`);
    }
  };

  const handleFlash = async (thingName, variant) => {
    const label = VARIANT_LABELS[variant] || variant;
    const confirmed = window.confirm(
      `Build "${label}" firmware for ${thingName} and push via OTA?\n\n` +
      `If it isn't already built it will be built first, then the device ` +
      `downloads it and reboots into the selected variant.`
    );
    if (!confirmed) return;

    // Starts the async Step Functions migration for the chosen variant; returns
    // immediately. Live progress then appears on the device card
    // (migrationStatus) via the poll.
    const data = await appSyncQuery(MIGRATE_FIRMWARE, { thingName, variant });
    if (data && data.migrateFirmware && data.migrateFirmware.success) {
      showMessage(`Flash to "${label}" started for ${thingName}. Watch the card for progress.`);
    } else {
      const msg = data && data.migrateFirmware && data.migrateFirmware.message;
      showMessage(msg || `Failed to start flash for ${thingName}`);
    }
  };

  const showMessage = (msg) => {
    setMessage(msg);
    setTimeout(() => setMessage(''), 8000);
  };

  const onlineCount = devices.filter(d => {
    const lastSeen = d.lastSeen ? new Date(d.lastSeen) : null;
    return d.status === 'online' && lastSeen && (Date.now() - lastSeen.getTime() < 90000);
  }).length;

  const pqcCount = devices.filter(d =>
    d.pqcAlgorithm && (d.pqcAlgorithm.includes('MLKEM') || d.pqcAlgorithm.includes('Kyber') || d.pqcAlgorithm.includes('PQC'))
  ).length;

  return (
    <div className="app">
      <header>
        <div className="header-content">
          <h1>PQC Workshop — Device Dashboard</h1>
          <p>Post-Quantum Cryptography Migration Demo</p>
        </div>
        <div className="header-stats">
          <div className="stat">
            <span className="stat-value">{onlineCount}</span>
            <span className="stat-label">Online</span>
          </div>
          <div className="stat">
            <span className="stat-value">{pqcCount}</span>
            <span className="stat-label">PQC Active</span>
          </div>
          <div className="stat">
            <span className="stat-value">{devices.length}</span>
            <span className="stat-label">Total</span>
          </div>
        </div>
      </header>

      {message && <div className="message-banner">{message}</div>}

      <main>
        {loading ? (
          <div className="loading-state">Loading devices...</div>
        ) : devices.length === 0 ? (
          <div className="empty-state">
            <h2>No devices connected</h2>
            <p>Plug in your ESP32 device. It will appear here once connected to the broker.</p>
          </div>
        ) : (
          <div className="device-grid">
            {devices.map(device => (
              <DeviceCard
                key={device.thingName}
                device={device}
                onCommand={handleCommand}
                onShowLog={setLogDevice}
                onFlash={handleFlash}
              />
            ))}
          </div>
        )}
      </main>

      {logDevice && <LogPanel thingName={logDevice} onClose={() => setLogDevice(null)} />}

      <footer>
        <p>AWS re:Invent Builders Session — Post-Quantum Cryptography for IoT</p>
      </footer>
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatUptime(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

// Render a byte count in KB (device memory/flash numbers), or an em dash when
// the value is missing. 0 is a real value (e.g. PSRAM disabled) so it is kept.
function formatKB(bytes) {
  if (bytes === undefined || bytes === null) return '—';
  return `${Math.round(bytes / 1024)}KB`;
}

function truncate(str, maxLen) {
  if (!str) return '';
  return str.length > maxLen ? str.substring(0, maxLen) + '...' : str;
}

export default App;
