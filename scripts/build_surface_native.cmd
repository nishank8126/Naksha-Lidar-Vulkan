@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 exit /b %errorlevel%
"H:\tools\Ninja\ninja.exe" -C "H:\naksha-lidar 2\native\naksha_vulkan\build_msvc" -j4 naksha_vulkan
