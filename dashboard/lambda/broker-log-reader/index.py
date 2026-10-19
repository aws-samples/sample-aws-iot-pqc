# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Broker Log Reader Lambda

Queries CloudWatch Logs for the Mosquitto broker log, optionally filtered
by device name. Returns the most recent log entries.

Environment variables:
  LOG_GROUP_NAME — CloudWatch log group (default: /pqc-workshop/broker-container,
                   the live ECS Fargate broker; the legacy Raspberry-Pi group
                   /pqc-workshop/mosquitto-broker is retired).
"""

import boto3
import json
import os
import time

logs_client = boto3.client('logs')

LOG_GROUP = os.environ.get('LOG_GROUP_NAME', '/pqc-workshop/broker-container')


def handler(event, context):
    """Query broker logs, optionally filtered by device name."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get('thingName', '')
    limit = event.get('limit', 50)
    if limit > 200:
        limit = 200

    # Build CloudWatch Logs Insights query
    if thing_name:
        query = f"fields @timestamp, @message | filter @message like /{thing_name}/ | sort @timestamp desc | limit {limit}"
    else:
        query = f"fields @timestamp, @message | filter @message not like /PINGREQ/ | filter @message not like /PINGRESP/ | sort @timestamp desc | limit {limit}"

    # Query last 1 hour of logs
    end_time = int(time.time())
    start_time = end_time - 3600

    try:
        response = logs_client.start_query(
            logGroupName=LOG_GROUP,
            startTime=start_time,
            endTime=end_time,
            queryString=query
        )
        query_id = response['queryId']

        # Poll for results (Logs Insights is async)
        results = None
        for _ in range(10):
            time.sleep(0.5)
            result_response = logs_client.get_query_results(queryId=query_id)
            if result_response['status'] == 'Complete':
                results = result_response['results']
                break

        if results is None:
            return {
                'entries': [],
                'message': 'Query timed out'
            }

        # Format results
        entries = []
        for row in results:
            entry = {}
            for field in row:
                if field['field'] == '@timestamp':
                    entry['timestamp'] = field['value']
                elif field['field'] == '@message':
                    entry['message'] = field['value'].strip()
            if entry.get('message'):
                entries.append(entry)

        return {
            'entries': entries,
            'count': len(entries)
        }

    except Exception as e:
        print(f"Error querying logs: {e}")
        return {
            'entries': [],
            'message': str(e)
        }
