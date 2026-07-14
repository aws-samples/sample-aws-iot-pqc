/**
 * Dashboard v2 Configuration — Firmware Management
 *
 * Update these values after deploying the device-builder CloudFormation stack.
 */
export default {
  // API Gateway endpoint for device registration and firmware management
  apiEndpoint: 'https://683vdu3tii.execute-api.us-east-1.amazonaws.com/prod',

  // Polling interval for device status (ms)
  pollInterval: 10000,

  // AWS region
  region: 'us-east-1',
};
