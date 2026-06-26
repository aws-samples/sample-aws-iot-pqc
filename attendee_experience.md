This is being built for a builders session at re:Invent.  The time limit is 60 minutes, the attendees work on tables of 8 people with 1 AWS staff member as the facilitator.

The user will walk into the room and the single AWS account will already be created by the facilitator.  The AWS account will have all the AWS cloud side configuration created to facilitate the ESP-32 IoT devices connecting to AWS IOT Core.  The AWS Account will also have a dashboard app that shows the state of the IoT devices connected to it, so the attendees can see the state of their device without needing to login.  The single account will have a single greengrass device, that is a raspberry pi, that will have the TLS 1.3 with PQC support installed.  The Greengrass device will also have Mosquitto installed and configured for PQC capability.  The ESP devices will be pre packaged with two images, both requiring registration, one with classical TLS and one with PQC TLS.  

The AWS facilitator will do a presentation for 5 minutes on powerpoint.  the presentation will contain information about PQC, the threat and the mitigation. The talk will also talk about different techniques to accomplish the firmware update to PqC firmware at scale.

The attendee will now plug their ESP-32 device into a usb port in their laptop.  The device will use the firmware in boot partiion 0 to conenct to the greengrass device using TLS classical.  The device will appear on the dashboard and start sending its data packets, including CPU temperature, memory usage, firmware version, TLS encryption algortym, and other stats for the ESP-32 devcice.

The AWS facilitator will now do a code walkthrough of the firmware updates.  They will explain how to implement the PQC on the device.  the builders session attendees can load Arduino on their laptop and view the code for themselves.

The Attendees will now trigger a firmware update on the device, to make it boot with partition 1 which contains the PQC configured firmware.  This should be done by clicking a button on the dashboard to update the firmware.

The ESP-32 device will reboot, start the PQC firmware, and then  connect to greengrass with PQC.  The device will continue to send its data, and this will show the device is using PQC now.  

The attendee will leave with the ESP-32 device and the source code for the project so they can do this themselves at home.
