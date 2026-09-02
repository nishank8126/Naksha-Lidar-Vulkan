# NakshaAI LiDAR

Windows desktop application for LiDAR visualization, classification, flight-line filtering, cross sections, surface generation, and SNT/DXF overlays.

## Environment

- Windows x64
- Python 3.10

```powershell
py -3.10 -m venv .venv
.venv/Scripts/python -m pip install --upgrade pip
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pip install ./GDAL-3.10.1-cp310-cp310-win_amd64.whl
.venv/Scripts/python -m pip install ./snt_core-1.3.1-cp310-cp310-win_amd64.whl
.venv/Scripts/python main.py
```

Use `snt_core-1.3.0-cp310-cp310-win_amd64.whl` only when compatibility with the older SNT runtime is required.

The manually installed Naksha Converter plugin carries its own known-good DGN
backend. NakshaAI does not require `snt_v2` for plugin conversion.

## Optional crash reporting

Set `NAKSHA_SMTP_PASSWORD` in the deployment environment or create an untracked `crash_config.json`. Never commit credentials.

## Build

```powershell
.venv/Scripts/python -m PyInstaller --clean --noconfirm naksha.spec
```

Generated virtual environments, installers, builds, customer point clouds, and backup data are intentionally excluded. They are reproducible outputs or private datasets rather than source dependencies.
