import React, { useState, useEffect, useCallback } from 'react';
import { CognitoIdentityClient, GetIdCommand, GetCredentialsForIdentityCommand } from '@aws-sdk/client-cognito-identity';
import config from './config.js';
import { LIST_DEVICES, MIGRATE_FIRMWARE, DELETE_DEVICE, SEND_COMMAND, GET_DEVICE_LOG, GET_BROKER_LOG } from './graphql.js';

// Cognito client for fetching guest credentials
const cognitoClient = new CognitoIdentityClient({ region: config.region });

let cachedCredentials = null;
let credentialsExpiry = 0;

async function getCredentials() {
  if (cachedCredentials && Date.now() < credentialsExpiry) {
    return cachedCredentials;
  }
  const idResp = await cognitoClient.send(new GetIdCommand({ IdentityPoolId: config.identityPoolId }));
  const credsResp = await cognitoClient.send(new GetCredentialsForIdentityCommand({ IdentityId: idResp.IdentityId }));
  cachedCredentials = credsResp.Credentials;
  credentialsExpiry = Date.now() + 50 * 60 * 1000; // refresh after 50 min
  return cachedCredentials;
}

/**
 * AppSync query with IAM SigV4 signing.
 */
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
    const headers = {
      'content-type': 'application/json',
      'host': url.host,
      'x-amz-date': datetime,
      'x-amz-security-token': creds.SessionToken,
    };
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

    const response = await fetch(config.appSyncEndpoint, {
      method: 'POST',
      headers: { ...headers, authorization },
      body,
    });
    const result = await response.json();
    if (result.errors) console.error('GraphQL errors:', result.errors);
    return result.data;
  } catch (err) {
    console.error('AppSync query failed:', err);
    return null;
  }
}

function DeviceCard({ device, onMigrate, onRevert, onLedToggle, onShowLog }) {
  const isPqc = device.pqcAlgorithm && 
    (device.pqcAlgorithm.includes('MLKEM') || 
     device.pqcAlgorithm.includes('Kyber') || 
     device.pqcAlgorithm.includes('PQC'));
  // Device is online only if status says so AND lastSeen is within 90 seconds
  const lastSeenDate = device.lastSeen ? new Date(device.lastSeen) : null;
  const staleThreshold = 90 * 1000; // 90 seconds
  const isStale = lastSeenDate ? (Date.now() - lastSeenDate.getTime() > staleThreshold) : true;
  const isOnline = device.status === 'online' && !isStale;
  const lastSeen = lastSeenDate
    ? lastSeenDate.toLocaleTimeString()
    : 'Never';

  return (
    <div className={`device-card ${isPqc ? 'pqc' : 'classical'} ${isOnline ? 'online' : 'offline'}`}>
      <div className="device-header">
        <h3>{device.thingName}</h3>
        <div className="header-actions">
          <button className="log-button" onClick={() => onShowLog(device.thingName)} title="View message log">📋</button>
          <span className={`status-badge ${isOnline ? 'online' : 'offline'}`}>
            {isOnline ? '● Online' : '○ Offline'}
          </span>
        </div>
      </div>

      <div className="device-crypto">
        <span className={`crypto-badge ${isPqc ? 'pqc' : 'classical'}`}>
          {isPqc ? '🛡️ PQC' : '🔓 Classical'}
        </span>
        <span className="tls-version">{device.tlsVersion || '—'}</span>
      </div>

      <div className="device-stats">
        <div className="stat">
          <label>Algorithm</label>
          <value>{device.pqcAlgorithm || 'none'}</value>
        </div>
        <div className="stat">
          <label>Firmware</label>
          <value>{device.firmwareVersion || '—'}</value>
        </div>
        <div className="stat">
          <label>WiFi RSSI</label>
          <value>{device.wifiRssi ? `${device.wifiRssi} dBm` : '—'}</value>
        </div>
        <div className="stat">
          <label>Free Heap</label>
          <value>{device.freeHeap ? `${(device.freeHeap / 1024).toFixed(0)} KB` : '—'}</value>
        </div>
        <div className="stat">
          <label>Temperature</label>
          <value>{device.chipTempC ? `${device.chipTempC}°C` : '—'}</value>
        </div>
        <div className="stat">
          <label>Uptime</label>
          <value>{device.uptimeSec ? `${Math.floor(device.uptimeSec / 60)}m ${device.uptimeSec % 60}s` : '—'}</value>
        </div>
        <div className="stat">
          <label>Messages</label>
          <value>{device.messageId || 0}</value>
        </div>
        <div className="stat">
          <label>Last Seen</label>
          <value>{lastSeen}</value>
        </div>
      </div>

      {isOnline && (
        <div className="led-controls">
          <label className="toggle-switch">
            <input
              type="checkbox"
              defaultChecked={true}
              onChange={(e) => onLedToggle(device.thingName, e.target.checked)}
            />
            <span className="toggle-slider"></span>
          </label>
          <span className="toggle-label">Blink</span>
        </div>
      )}

      {!isPqc && isOnline && (
        <button
          className="migrate-button"
          onClick={() => onMigrate(device.thingName)}
        >
          ⬆️ Migrate to PQC
        </button>
      )}

      {isPqc && isOnline && (
        <div className="pqc-actions">
          <div className="migrated-banner">
            ✓ Quantum-resistant connection active
          </div>
          <button
            className="revert-button"
            onClick={() => onRevert(device.thingName)}
          >
            ⬇️ Revert to Classical
          </button>
        </div>
      )}

      {isPqc && !isOnline && (
        <div className="migrated-banner">
          ✓ Quantum-resistant connection active
        </div>
      )}
    </div>
  );
}

function MessageLogPanel({ thingName, onClose }) {
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let interval;
    const fetchLogs = async () => {
      const data = await appSyncQuery(GET_DEVICE_LOG, { thingName, limit: 50 });
      if (data && data.getDeviceLog) {
        setLogs(data.getDeviceLog);
      }
      setLoading(false);
    };
    fetchLogs();
    interval = setInterval(fetchLogs, 5000);
    return () => clearInterval(interval);
  }, [thingName]);

  return (
    <div className="log-panel-overlay" onClick={onClose}>
      <div className="log-panel" onClick={(e) => e.stopPropagation()}>
        <div className="log-panel-header">
          <h3>📋 Message Log — {thingName}</h3>
          <button className="log-close" onClick={onClose}>✕</button>
        </div>
        <div className="log-entries">
          {loading && <div className="log-loading">Loading messages...</div>}
          {!loading && logs.length === 0 && (
            <div className="log-empty">No messages yet. Messages appear here as the device communicates.</div>
          )}
          {logs.map((entry, i) => (
            <div key={i} className={`log-entry ${entry.direction}`}>
              <span className="log-time">{new Date(entry.timestamp).toLocaleTimeString()}</span>
              <span className={`log-direction ${entry.direction}`}>
                {entry.direction === 'inbound' ? '⬆️' : '⬇️'}
              </span>
              <span className="log-topic">{entry.topic}</span>
              <pre className="log-payload">{entry.payload}</pre>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function BrokerLogPanel({ thingName, onClose }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterText, setFilterText] = useState(thingName || '');

  useEffect(() => {
    let interval;
    const fetchLogs = async () => {
      const data = await appSyncQuery(GET_BROKER_LOG, { thingName: filterText || null, limit: 100 });
      if (data && data.getBrokerLog && data.getBrokerLog.entries) {
        setEntries(data.getBrokerLog.entries);
      }
      setLoading(false);
    };
    fetchLogs();
    interval = setInterval(fetchLogs, 5000);
    return () => clearInterval(interval);
  }, [filterText]);

  return (
    <div className="log-panel-overlay" onClick={onClose}>
      <div className="log-panel broker-log-panel" onClick={(e) => e.stopPropagation()}>
        <div className="log-panel-header">
          <h3>🦟 Broker Log</h3>
          <div className="broker-filter">
            <input
              type="text"
              placeholder="Filter by device..."
              value={filterText}
              onChange={(e) => setFilterText(e.target.value)}
              className="broker-filter-input"
            />
          </div>
          <button className="log-close" onClick={onClose}>✕</button>
        </div>
        <div className="log-entries">
          {loading && <div className="log-loading">Querying CloudWatch Logs...</div>}
          {!loading && entries.length === 0 && (
            <div className="log-empty">No broker log entries found for the last hour.</div>
          )}
          {entries.map((entry, i) => (
            <div key={i} className="log-entry broker">
              <span className="log-time">{entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : ''}</span>
              <span className="broker-message">{entry.message}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function App() {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [migrating, setMigrating] = useState(null);
  const [error, setError] = useState(null);
  const [showManage, setShowManage] = useState(false);
  const [filter, setFilter] = useState('');
  const [logDevice, setLogDevice] = useState(null);
  const [showBrokerLog, setShowBrokerLog] = useState(false);

  const fetchDevices = useCallback(async () => {
    try {
      const data = await appSyncQuery(LIST_DEVICES);
      if (data && data.listDevices) {
        setDevices(data.listDevices);
      }
      setError(null);
    } catch (err) {
      console.error('Failed to fetch devices:', err);
      setError('Failed to connect to API. Check configuration.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDevices();
    const interval = setInterval(fetchDevices, config.pollInterval);
    return () => clearInterval(interval);
  }, [fetchDevices]);

  const handleMigrate = async (thingName) => {
    if (!window.confirm(`Migrate ${thingName} to PQC firmware?\n\nThe device will reboot and reconnect with quantum-resistant encryption.`)) {
      return;
    }

    setMigrating(thingName);
    try {
      const data = await appSyncQuery(MIGRATE_FIRMWARE, { thingName });
      if (data && data.migrateFirmware && data.migrateFirmware.success) {
        console.log(`Migration command sent to ${thingName}`);
      } else {
        alert(`Migration failed: ${data?.migrateFirmware?.message || 'Unknown error'}`);
      }
    } catch (err) {
      console.error('Migration error:', err);
      alert(`Migration error: ${err.message}`);
    } finally {
      setTimeout(() => setMigrating(null), 3000);
    }
  };

  const handleDeleteDevice = async (thingName) => {
    if (!window.confirm(`Remove ${thingName} from the dashboard?`)) return;
    try {
      await appSyncQuery(DELETE_DEVICE, { thingName });
      setDevices(prev => prev.filter(d => d.thingName !== thingName));
    } catch (err) {
      alert(`Failed to remove: ${err.message}`);
    }
  };

  const handleDeleteAll = async () => {
    if (!window.confirm(`Remove ALL ${devices.length} devices from the dashboard?\n\nThis cannot be undone.`)) return;
    try {
      for (const device of devices) {
        await appSyncQuery(DELETE_DEVICE, { thingName: device.thingName });
      }
      setDevices([]);
      setShowManage(false);
    } catch (err) {
      alert(`Failed: ${err.message}`);
      fetchDevices();
    }
  };

  const handleLedToggle = async (thingName, turnOn) => {
    const action = turnOn ? 'led-on' : 'led-off';
    try {
      await appSyncQuery(SEND_COMMAND, { thingName, action, topicSuffix: 'control' });
    } catch (err) {
      console.error('LED toggle failed:', err);
    }
  };

  const handleRevert = async (thingName) => {
    if (!window.confirm(`Revert ${thingName} to classical firmware?\n\nThe device will reboot and reconnect with standard TLS encryption.`)) {
      return;
    }

    setMigrating(thingName);
    try {
      const data = await appSyncQuery(SEND_COMMAND, { thingName, action: 'migrate-classical', topicSuffix: 'ota' });
      if (data && data.sendCommand && data.sendCommand.success) {
        console.log(`Revert command sent to ${thingName}`);
      } else {
        alert(`Revert failed: ${data?.sendCommand?.message || 'Unknown error'}`);
      }
    } catch (err) {
      console.error('Revert error:', err);
      alert(`Revert error: ${err.message}`);
    } finally {
      setTimeout(() => setMigrating(null), 3000);
    }
  };

  const pqcCount = devices.filter(d => d.pqcAlgorithm && (d.pqcAlgorithm.includes('MLKEM') || d.pqcAlgorithm.includes('Kyber'))).length;
  const onlineCount = devices.filter(d => d.status === 'online').length;
  const filteredDevices = filter
    ? devices.filter(d => d.thingName.toLowerCase().includes(filter.toLowerCase()))
    : devices;

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-content">
          <h1>🔐 PQC Workshop Dashboard</h1>
          <p className="subtitle">ESP32 Post-Quantum Cryptography Migration</p>
        </div>
        <div className="header-stats">
          <div className="header-stat">
            <span className="stat-value">{onlineCount}</span>
            <span className="stat-label">Online</span>
          </div>
          <div className="header-stat">
            <span className="stat-value">{pqcCount}</span>
            <span className="stat-label">PQC Active</span>
          </div>
          <div className="header-stat">
            <span className="stat-value">{devices.length}</span>
            <span className="stat-label">Total</span>
          </div>
        </div>
        <div className="header-filter">
          <input
            type="text"
            placeholder="Filter by device name..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="filter-input"
          />
          {filter && <button className="filter-clear" onClick={() => setFilter('')}>✕</button>}
        </div>
      </header>

      {error && (
        <div className="error-banner">
          ⚠️ {error}
        </div>
      )}

      {loading ? (
        <div className="loading">Loading devices...</div>
      ) : devices.length === 0 ? (
        <div className="empty-state">
          <h2>No devices connected</h2>
          <p>Plug in your ESP32 device via USB. It will connect automatically.</p>
        </div>
      ) : (
        <div className="device-grid">
          {filteredDevices
            .sort((a, b) => a.thingName.localeCompare(b.thingName))
            .map(device => (
              <DeviceCard
                key={device.thingName}
                device={device}
                onMigrate={handleMigrate}
                onRevert={handleRevert}
                onLedToggle={handleLedToggle}
                onShowLog={(name) => setLogDevice(name)}
              />
            ))
          }
        </div>
      )}

      <footer className="app-footer">
        <p>re:Invent Builders Session — Post-Quantum Cryptography for IoT</p>
      </footer>

      {/* Manage Devices Panel */}
      <button
        className="manage-button"
        onClick={() => setShowManage(!showManage)}
      >
        ⚙️ Manage Devices
      </button>

      <button
        className="broker-log-button"
        onClick={() => setShowBrokerLog(true)}
      >
        🦟 Broker Log
      </button>

      {showManage && (
        <div className="manage-panel">
          <div className="manage-panel-content">
            <h3>Manage Devices</h3>
            <div className="manage-list">
              {devices.map(d => (
                <div key={d.thingName} className="manage-item">
                  <span>{d.thingName}</span>
                  <span className={`manage-status ${d.status === 'online' ? 'online' : 'offline'}`}>
                    {d.status || 'unknown'}
                  </span>
                  <button onClick={() => handleDeleteDevice(d.thingName)}>✕ Remove</button>
                </div>
              ))}
            </div>
            <div className="manage-actions">
              <button className="remove-all-button" onClick={handleDeleteAll}>
                🗑️ Remove All Devices
              </button>
              <button onClick={() => setShowManage(false)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {logDevice && (
        <MessageLogPanel thingName={logDevice} onClose={() => setLogDevice(null)} />
      )}

      {showBrokerLog && (
        <BrokerLogPanel thingName="" onClose={() => setShowBrokerLog(false)} />
      )}
    </div>
  );
}

export default App;
