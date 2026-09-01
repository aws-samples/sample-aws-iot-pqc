This is being built for a workshop at re:Invent.  The time limit is 60 minutes, the attendees work on tables of 8 people with 1 AWS staff member as the facilitator.

The user will walk into the room and the single AWS account will already be created by the facilitator.  The AWS account will have all the AWS cloud side configuration created to facilitate the ESP-32 IoT devices connecting to AWS IOT Core.  The AWS Account will also have a dashboard app that shows the state of the IoT devices connected to it, so the attendees can see the state of their device without needing to login.  

The single account will have a builder-pi, that is a raspberry pi, that will have the tool chain for deploying the fireware v0.1.0, and it will be running the gui app for detecting and flashing devices.  The single account will have the mosquitto broker container running, that is ready for PQC and classical coinnections from the IoT devices.  The certificates will be provisioned by the Private CA, and devices and the broker will have the CA in common.

Each attendee will receive an esp-32 device.  I think I will provide the device with fireware 0.1.0 already configured, rather than trying to run the deploy using the builder-pi on the day.  The case for the device will have the name of the device labled on it so the attendee knows which device is theirs.

The AWS facilitator will do a presentation for 5 minutes on powerpoint.  the presentation will contain information about PQC, the threat and the mitigation. The talk will also talk about different techniques to accomplish the firmware update to PqC firmware at scale.

The attendee will now plug their ESP-32 device into a usb port in their laptop.  The device will use the firmware to conenct to the broker container using TLS classical.  The device will already be in the dashboard, and the attendee will be able to confirm that their devixce is is sending its data packets, including CPU temperature, memory usage, firmware version, TLS encryption algortym, and other stats for the ESP-32 devcice.  They will also view the logs, and issue sample commands to the device such as stopping the blink and restarting it.  

The AWS facilitator will now do a code walkthrough of the firmware updates.  They will explain how to implement the PQC on the device.  the builders session attendees can load Arduino on their laptop and view the code for themselves.

The attendees will no trigger a pipeline build for their specific device.  This should use a button on the dashboard.  The build will complete and be stored in S3.  The build will then trigger the device to pull the new fireware using the OTA channel and by giving it the S3 signed url to the fireware.

The ESP-32 device will download the new image to the OTA partition, reboot using the new firmware, and then  connect to greengrass with PQC.  The device will continue to send its data, and this will show the device is using PQC now.  

The attendee will leave with the ESP-32 device and the source code for the project so they can do this themselves at home.
