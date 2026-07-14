import React, { useState, useEffect, useCallback } from 'react';
import config from './config.js';

/**
 * Dashboard v2 — Firmware Management
 *
 * Shows all registered devices from the device registry,
 * lists available firmware versions per device from S3,
 * and allows pushing any version to a device via OTA.
 */

async function apiCall(path, method = 'GET', body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(`${config.apiEndpoint}${path}`, opts);
    return await res.json();
  } catch (err) {
    console.error(`API error: ${err}`);
    return null;
  }
}

function DeviceRow({ device, onPushFirmware }) {
  const [versions, setVersions] = useState([]);
  const [selectedVersion, setSelectedVersion] = useState('');
  const [pushing, setPushing] = useState(false);
  const [loadingVersions, setLoadingVersions] = useState(true);

  useEffect(() => {
    async function loadVersions() {
      const data = await apiCall(`/firmware?serialNumber=${device.serialNumber}`);
      if (data && data.versions) {
        setVersions(data.versions);
        if (data.versions.length > 0) setSelectedVersion(data.versions[0]);
      }
      setLoadingVersions(false);
    }
    loadVersions();
  }, [device.serialNumber]);

  const handlePush = async () => {
    if (!selectedVersion) return;
    setPushing(true);
    await onPushFirmware(device.serialNumber, device.thingName, selectedVersion);
    setPushing(false);
  };

  return (
    <tr className={`device-row ${device.status}`}>
      <td className="device-name">{device.thingName}</td>
      <td>{device.serialNumber}</td>
      <td><span className={`status ${device.status}`}>{device.status}</span></td>
      <td>{device.firmwareVersion || '—'}</td>
      <td>{device.certType || 'unknown'}</td>
      <td>{device.registeredAt ? new Date(device.registeredAt).toLocaleDateString() : '—'}</td>
      <td className="firmware-actions">
        {loadingVersions ? (
          <span className="loading-text">Loading...</span>
        ) : versions.length === 0 ? (
          <span className="no-versions">No builds</span>
        ) : (
          <>
            <select
              value={selectedVersion}
              onChange={(e) => setSelectedVersion(e.target.value)}
              disabled={pushing}
            >
              {versions.map(v => (
                <option key={v} value={v}>
                  {v} {v === device.firmwareVersion ? '(current)' : ''}
                </option>
              ))}
            </select>
            <button
              className="push-btn"
              onClick={handlePush}
              disabled={pushing || selectedVersion === device.firmwareVersion}
            >
              {pushing ? '⏳ Pushing...' : '🚀 Push OTA'}
            </button>
          </>
        )}
      </td>
    </tr>
  );
}

function App() {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState('');

  const fetchDevices = useCallback(async () => {
    const data = await apiCall('/firmware');
    if (data && data.devices) {
      setDevices(data.devices);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchDevices();
    const interval = setInterval(fetchDevices, config.pollInterval);
    return () => clearInterval(interval);
  }, [fetchDevices]);

  const handlePushFirmware = async (serialNumber, thingName, version) => {
    const confirmed = window.confirm(
      `Push firmware v${version} to ${thingName}?\n\n` +
      `The device will download the new firmware and reboot.`
    );
    if (!confirmed) return;

    setMessage(`Pushing v${version} to ${thingName}...`);
    const result = await apiCall('/firmware', 'POST', {
      serialNumber,
      thingName,
      firmwareVersion: version
    });

    if (result && result.success) {
      setMessage(`✅ OTA v${version} sent to ${thingName}. Device will reboot shortly.`);
    } else {
      setMessage(`❌ Push failed: ${result?.error || 'Unknown error'}`);
    }
    setTimeout(() => setMessage(''), 8000);
  };

  return (
    <div className="app">
      <header>
        <div className="header-content">
          <h1>🔧 PQC Firmware Manager</h1>
          <p>Production Device Registry &amp; OTA Updates</p>
        </div>
        <div className="header-stats">
          <div className="stat">
            <span className="stat-value">{devices.length}</span>
            <span className="stat-label">Devices</span>
          </div>
          <div className="stat">
            <span className="stat-value">{devices.filter(d => d.status === 'registered').length}</span>
            <span className="stat-label">Registered</span>
          </div>
        </div>
      </header>

      {message && <div className="message-banner">{message}</div>}

      <main>
        {loading ? (
          <div className="loading-state">Loading device registry...</div>
        ) : devices.length === 0 ? (
          <div className="empty-state">
            <h2>No devices registered</h2>
            <p>Use the Builder-Pi GUI to register new ESP32 devices.</p>
          </div>
        ) : (
          <table className="device-table">
            <thead>
              <tr>
                <th>Device Name</th>
                <th>Serial</th>
                <th>Status</th>
                <th>Current FW</th>
                <th>Cert Type</th>
                <th>Registered</th>
                <th>Firmware Update</th>
              </tr>
            </thead>
            <tbody>
              {devices.map(device => (
                <DeviceRow
                  key={device.serialNumber}
                  device={device}
                  onPushFirmware={handlePushFirmware}
                />
              ))}
            </tbody>
          </table>
        )}
      </main>

      <footer>
        <p>PQC Workshop — Production Device Pipeline</p>
      </footer>
    </div>
  );
}

export default App;
