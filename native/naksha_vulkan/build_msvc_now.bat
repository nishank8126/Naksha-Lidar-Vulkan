@echo off
rem MSVC + Ninja build for build_msvc (wraps vcvars64 so INCLUDE/LIB are set).
rem Usage: build_msvc_now.bat [ninja args, default: -j 8]
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo [build] vcvars64 FAILED
  exit /b 1
)
cd /d "H:\naksha-lidar 2\native\naksha_vulkan\build_msvc"
if "%~1"=="" ( H:\tools\Ninja\ninja.exe -j 8 ) else ( H:\tools\Ninja\ninja.exe %* )
exit /b %ERRORLEVEL%
