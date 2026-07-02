import React, { useState, useEffect, useCallback } from 'react';
import { CognitoIdentityClient, GetIdCommand, GetCredentialsForIdentityCommand } from '@aws-sdk/client-cognito-identity';
import config from './config.js';
import { LIST_DEVICES, MIGRATE_FIRMWARE } from './graphql.js';

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

function DeviceCard({ device, onMigrate }) {
  const isPqc = device.pqcAlgorithm && device.pqcAlgorithm !== 'none';
  const isOnline = device.status === 'online';
  const lastSeen = device.lastSeen
    ? new Date(device.lastSeen).toLocaleTimeString()
    : 'Never';

  return (
    <div className={`device-card ${isPqc ? 'pqc' : 'classical'} ${isOnline ? 'online' : 'offline'}`}>
      <div className="device-header">
        <h3>{device.thingName}</h3>
        <span className={`status-badge ${isOnline ? 'online' : 'offline'}`}>
          {isOnline ? '● Online' : '○ Offline'}
        </span>
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

      {!isPqc && isOnline && (
        <button
          className="migrate-button"
          onClick={() => onMigrate(device.thingName)}
        >
          ⬆️ Migrate to PQC
        </button>
      )}

      {isPqc && (
        <div className="migrated-banner">
          ✓ Quantum-resistant connection active
        </div>
      )}
    </div>
  );
}

function App() {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [migrating, setMigrating] = useState(null);
  const [error, setError] = useState(null);

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

  const pqcCount = devices.filter(d => d.pqcAlgorithm && d.pqcAlgorithm !== 'none').length;
  const onlineCount = devices.filter(d => d.status === 'online').length;

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
          {devices
            .sort((a, b) => a.thingName.localeCompare(b.thingName))
            .map(device => (
              <DeviceCard
                key={device.thingName}
                device={device}
                onMigrate={handleMigrate}
              />
            ))
          }
        </div>
      )}

      <footer className="app-footer">
        <p>re:Invent Builders Session — Post-Quantum Cryptography for IoT</p>
      </footer>
    </div>
  );
}

export default App;
