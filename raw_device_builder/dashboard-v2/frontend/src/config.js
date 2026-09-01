/**
 * Dashboard v2 Configuration — Device Telemetry + Firmware Management
 *
 * Combines AppSync (for real-time telemetry, logs, commands) with
 * the REST API (for firmware builds and OTA pushes).
 */
export default {
  // AppSync GraphQL endpoint (from v1 dashboard stack)
  appSyncEndpoint: 'https://mpalbkcb6za2hnwibxygktd7ya.appsync-api.us-east-1.amazonaws.com/graphql',

  // Cognito Identity Pool ID (unauthenticated access for dashboard)
  identityPoolId: 'us-east-1:589e1d0a-9a61-4021-a7e9-643cd1899375',

  // REST API endpoint for device registration and firmware management
  apiEndpoint: 'https://683vdu3tii.execute-api.us-east-1.amazonaws.com/prod',

  // AWS region
  region: 'us-east-1',

  // Polling interval for device telemetry (ms)
  pollInterval: 5000,
};
