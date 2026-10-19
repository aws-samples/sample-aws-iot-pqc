// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Runtime configuration holder for the dashboard SPA.
 *
 * Values are NOT baked in at build time. Instead they are loaded at runtime
 * from /config.json (served alongside the built app) by loadConfig() in
 * main.jsx, BEFORE the React app is imported/rendered. This keeps the built
 * bundle environment-agnostic — the same artifact runs in any account, and no
 * account-specific endpoints are committed to source.
 *
 * The default export is a single mutable object that loadConfig() fills in.
 * App.jsx imports it after config has loaded, so its module-level reads (e.g.
 * the Cognito client constructed from config.region) see populated values.
 */

const config = {
  appSyncEndpoint: '',
  identityPoolId: '',
  apiEndpoint: '',
  region: 'us-east-1',
  pollInterval: 5000,
};

/**
 * Fetch /config.json and merge it into the shared config object.
 *
 * Guards against the SPA-routing fallback: CloudFront rewrites 403/404 to
 * index.html (HTTP 200), so a missing config.json returns HTML, not a 404.
 * We therefore validate that the response is JSON and contains the required
 * endpoints before accepting it, and throw a clear error otherwise.
 */
export async function loadConfig() {
  const resp = await fetch('/config.json', { cache: 'no-store' });
  if (!resp.ok) {
    throw new Error(`config.json fetch failed: HTTP ${resp.status}`);
  }
  const contentType = resp.headers.get('content-type') || '';
  const text = await resp.text();
  let loaded;
  try {
    loaded = JSON.parse(text);
  } catch {
    // Almost certainly the CloudFront 403/404 -> index.html rewrite: we got
    // HTML instead of JSON. Surface a clear, actionable error.
    throw new Error(
      'config.json is missing or not valid JSON (received ' +
        (contentType || 'unknown content-type') +
        '). The dashboard config was not deployed correctly.'
    );
  }
  if (!loaded.appSyncEndpoint || !loaded.apiEndpoint || !loaded.identityPoolId) {
    throw new Error('config.json is missing required endpoints');
  }
  Object.assign(config, loaded);
  return config;
}

export default config;
