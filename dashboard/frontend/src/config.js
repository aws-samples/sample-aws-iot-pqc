/**
 * Dashboard configuration.
 *
 * After deploying the CloudFormation stack, replace these values with
 * the stack outputs. You can retrieve them with:
 *
 *   aws cloudformation describe-stacks --stack-name pqc-workshop-dashboard \
 *     --query 'Stacks[0].Outputs' --output table
 */
const config = {
  // AppSync GraphQL endpoint
  appSyncEndpoint: 'REPLACE_WITH_APPSYNC_ENDPOINT',

  // AWS Region
  region: 'us-east-1',

  // Cognito Identity Pool ID (for unauthenticated access)
  identityPoolId: 'REPLACE_WITH_IDENTITY_POOL_ID',

  // Polling interval for device list (ms) — fallback if subscriptions fail
  pollInterval: 5000,
};

export default config;
