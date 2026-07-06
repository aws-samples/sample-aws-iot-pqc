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

export const GET_DEVICE = `
  query GetDevice($thingName: String!) {
    getDevice(thingName: $thingName) {
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

export const MIGRATE_FIRMWARE = `
  mutation MigrateFirmware($thingName: String!) {
    migrateFirmware(thingName: $thingName) {
      success
      message
      thingName
    }
  }
`;

export const DELETE_DEVICE = `
  mutation DeleteDevice($thingName: String!) {
    deleteDevice(thingName: $thingName) {
      success
      thingName
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
      entries {
        timestamp
        message
      }
      count
      message
    }
  }
`;

export const ON_DEVICE_UPDATE = `
  subscription OnDeviceUpdate {
    onDeviceUpdate {
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
