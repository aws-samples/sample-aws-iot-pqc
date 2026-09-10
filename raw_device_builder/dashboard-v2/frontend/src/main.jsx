import React from 'react';
import { createRoot } from 'react-dom/client';
import { loadConfig } from './config.js';
import './styles.css';

// Bootstrap: load runtime config BEFORE importing App. App.jsx reads config
// values at module load (e.g. the Cognito client uses config.region), so the
// config object must be populated before App is evaluated. We therefore fetch
// /config.json first, then dynamically import App only once config is ready.
const root = createRoot(document.getElementById('root'));

async function bootstrap() {
  try {
    await loadConfig();
  } catch (err) {
    console.error('Failed to load runtime config:', err);
    root.render(
      <div style={{ padding: '2rem', fontFamily: 'sans-serif', color: '#b00' }}>
        <h1>Dashboard configuration error</h1>
        <p>The dashboard could not load its runtime configuration (config.json).</p>
        <pre style={{ whiteSpace: 'pre-wrap' }}>{String(err && err.message ? err.message : err)}</pre>
      </div>
    );
    return;
  }
  // Config is loaded; now import App (its module-load reads see populated config).
  const { default: App } = await import('./App.jsx');
  root.render(<App />);
}

bootstrap();
