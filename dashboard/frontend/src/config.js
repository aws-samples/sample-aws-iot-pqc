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
  appSyncEndpoint: 'https://mpalbkcb6za2hnwibxygktd7ya.appsync-api.us-east-1.amazonaws.com/graphql',

  // AWS Region
  region: 'us-east-1',

  // Cognito Identity Pool ID (for unauthenticated access)
  identityPoolId: 'us-east-1:589e1d0a-9a61-4021-a7e9-643cd1899375',

  // Polling interval for device list (ms) — fallback if subscriptions fail
  pollInterval: 5000,
};

export default config;
