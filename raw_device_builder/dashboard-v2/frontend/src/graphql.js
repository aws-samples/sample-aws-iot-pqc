/**
 * GraphQL queries, mutations, and subscriptions for the device dashboard.
 */

export const LIST_DEVICES = `
  query ListDevices {
    listDevices {
      thingName
      status
      firmwareVersion
      pqcAlgorithm
      tlsVersion
      wifiRssi
      freeHeap
      minHeap
      chipTempC
      ipAddress
      uptimeSec
      messageId
      lastSeen
    }
  }
`;

export const SEND_COMMAND = `
  mutation SendCommand($thingName: String!, $action: String!, $topicSuffix: String) {
    sendCommand(thingName: $thingName, action: $action, topicSuffix: $topicSuffix) {
      success
      message
      thingName
    }
  }
`;

export const MIGRATE_FIRMWARE = `
  mutation MigrateFirmware($thingName: String!) {
    migrateFirmware(thingName: $thingName) {
      success
      message
      thingName
    }
  }
`;

export const GET_DEVICE_LOG = `
  query GetDeviceLog($thingName: String!, $limit: Int) {
    getDeviceLog(thingName: $thingName, limit: $limit) {
      thingName
      timestamp
      topic
      direction
      payload
    }
  }
`;

export const GET_BROKER_LOG = `
  query GetBrokerLog($thingName: String, $limit: Int) {
    getBrokerLog(thingName: $thingName, limit: $limit) {
      timestamp
      message
    }
  }
`;
