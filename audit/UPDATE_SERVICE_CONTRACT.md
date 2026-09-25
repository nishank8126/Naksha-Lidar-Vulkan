# NakshaAI-LiDAR update service contract

The desktop client is disabled until both `updates/api_url` and a unique
`updates/device_token` are set in the `NakshaAI/LidarApp` QSettings store.
They can alternatively be supplied through `NAKSHA_UPDATE_API_URL` and
`NAKSHA_UPDATE_DEVICE_TOKEN`. A system number can be provisioned through
`updates/system_number` or `NAKSHA_SYSTEM_NUMBER`. Production URLs must use
HTTPS.

## Check request

`POST /api/v1/updates/check`

The request includes `Authorization: Bearer <unique-device-token>`. The server
must store only a hash of this token and bind it to the authorized system.

```json
{
  "product": "NakshaAI-LiDAR",
  "current_version": "2.0.4",
  "device_id": "67ad09cc-fec7-493f-ab73-2fb13a936cff",
  "system_number": "NAKSHA-PC-0017",
  "hardware_fingerprint": "sha256-hex-value",
  "channel": "stable"
}
```

Return HTTP 204, or `{"update_available": false}`, when the device has no
assigned update. An available update uses HTTP 200:

```json
{
  "update_available": true,
  "version": "2.0.5",
  "download_url": "https://updates.example/releases/2.0.5/setup.exe",
  "sha256": "64-lowercase-hex-characters",
  "size": 483920100,
  "release_notes": "Release notes shown to the operator.",
  "mandatory": false
}
```

The service must authorize the device before returning a release. The download
URL should be short-lived. Every installer must have a valid Windows
Authenticode signature; set `updates/expected_publisher` or
`NAKSHA_UPDATE_PUBLISHER` to require an exact publisher substring.

The client stages the installer under Qt's per-user application-data directory,
checks its exact byte count and SHA-256, validates Authenticode, requests UAC,
installs silently, and passes `/RESTARTAFTERUPDATE` to the Inno installer.
