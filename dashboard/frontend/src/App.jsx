import React, { useState, useEffect, useCallback } from 'react';
import config from './config.js';
import { LIST_DEVICES, MIGRATE_FIRMWARE } from './graphql.js';

/**
 * Minimal AppSync client using fetch (avoids heavy Amplify bundle).
 * Uses Cognito Identity Pool for unauthenticated IAM access.
 */
async function appSyncQuery(query, variables = {}) {
  const response = await fetch(config.appSyncEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, variables }),
  });
  const result = await response.json();
  if (result.errors) {
    console.error('GraphQL errors:', result.errors);
  }
  return result.data;
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
