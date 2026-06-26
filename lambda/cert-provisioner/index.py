"""
Certificate Provisioner Lambda (CloudFormation Custom Resource)

Creates an IoT certificate and private key pair, stores them in
AWS Secrets Manager, and returns the certificate ARN for attachment.

Used by CloudFormation to provision certificates for both the
Greengrass core device and the original IoT Core setup.

Environment variables:
  THING_NAME - IoT Thing name (used for Secrets Manager path)
"""

import boto3
import cfnresponse
import os
import json


def handler(event, context):
    iot = boto3.client('iot')
    sm = boto3.client('secretsmanager')
    thing_name = os.environ['THING_NAME']
    try:
        if event['RequestType'] == 'Create':
            resp = iot.create_keys_and_certificate(setAsActive=True)
            cert_arn = resp['certificateArn']
            cert_id = resp['certificateId']
            cert_pem = resp['certificatePem']
            key_pem = resp['keyPair']['PrivateKey']

            secret_value = json.dumps({
                'certificatePem': cert_pem,
                'privateKey': key_pem,
                'certificateArn': cert_arn,
                'certificateId': cert_id
            })
            sm.create_secret(
                Name=f'iot/{thing_name}/device-credentials',
                Description=f'IoT device certificate and private key for {thing_name}',
                SecretString=secret_value,
                Tags=[
                    {'Key': 'Thing', 'Value': thing_name},
                    {'Key': 'ManagedBy', 'Value': 'CloudFormation'},
                    {'Key': 'Workshop', 'Value': 'pqc-reinvent'}
                ]
            )

            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'CertificateArn': cert_arn,
                'CertificateId': cert_id,
                'SecretName': f'iot/{thing_name}/device-credentials'
            }, physicalResourceId=cert_id)

        elif event['RequestType'] == 'Delete':
            cert_id = event.get('PhysicalResourceId', '')
            if cert_id and len(cert_id) == 64:
                try:
                    iot.update_certificate(certificateId=cert_id, newStatus='INACTIVE')
                except Exception:
                    pass
                try:
                    iot.delete_certificate(certificateId=cert_id, forceDelete=True)
                except Exception:
                    pass
            try:
                sm.delete_secret(
                    SecretId=f'iot/{thing_name}/device-credentials',
                    ForceDeleteWithoutRecovery=True
                )
            except Exception:
                pass
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})

        else:
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {},
                             physicalResourceId=event.get('PhysicalResourceId', ''))
    except Exception as e:
        print(f"Error: {e}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
