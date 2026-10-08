# build_native.ps1 - rebuild the native Vulkan renderer exactly the way the app
# loads it: gui/render_backend.py's _DLL_CANDIDATES prefers
# native/naksha_vulkan/build_msvc/naksha_vulkan.dll, and
# naksha_vulkan_c_api.cpp sets RendererConfig::shaderDirectory to the DLL's own
# directory, so the matching surface/point .spv files must land there too (the
# CMake POST_BUILD copy step already does that).
#
# Why a script: the build_msvc tree is a Ninja + MSVC-tree build, so cl.exe needs
# the Visual Studio developer environment (INCLUDE/LIB/PATH) that cmake only
# captures at configure time. Calling vcvars64.bat and cmake in one cmd.exe
# invocation is the only reliable way to rebuild without a full VS shell.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File build_native.ps1
#   powershell -ExecutionPolicy Bypass -File build_native.ps1 -BuildDir build
#
# Only the naksha_vulkan target is built by default: the sibling
# naksha_vulkan_benchmark target has no main() in tests/benchmark_main.cpp and
# fails to link, which would abort a whole-tree build before the DLL is
# relinked. Pass -Target to build something else.
[CmdletBinding()]
param(
    [string]$BuildDir = 'build_msvc',
    [string]$Target   = 'naksha_vulkan'
)
$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$native  = Join-Path $root 'native\naksha_vulkan'
$build   = Join-Path $native $BuildDir
$cmake   = 'H:\tools\CMake_64\bin\cmake.exe'
$vcvars  = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat'

foreach ($tool in @($cmake, $vcvars)) {
    if (-not (Test-Path $tool)) { throw "missing build tool: $tool" }
}
if (-not (Test-Path $build)) { throw "missing build dir: $build (configure it first)" }

Write-Host "[build] dir    : $build" -ForegroundColor Cyan
Write-Host "[build] shaders: $(Join-Path $build 'shaders')" -ForegroundColor Cyan

$inner = 'call "{0}" >nul 2>&1 && "{1}" --build "{2}" --target {3}' -f $vcvars, $cmake, $build, $Target
$output = & cmd.exe /c $inner 2>&1 | Out-String
Write-Host $output

$failed = $LASTEXITCODE -ne 0 -or $output -match 'error [A-Z]+[0-9]+|FAILED:'
if ($failed) {
    Write-Host "[build] FAILED (exit=$LASTEXITCODE)" -ForegroundColor Red
    exit 1
}

$dll = Join-Path $build 'naksha_vulkan.dll'
if (Test-Path $dll) {
    $info = Get-Item $dll
    Write-Host ("[build] OK     : {0} {1:N0} bytes {2:yyyy-MM-dd HH:mm:ss}" -f $info.Name, $info.Length, $info.LastWriteTime) -ForegroundColor Green
} else {
    Write-Host "[build] WARNING: $dll not found after build" -ForegroundColor Yellow
}
foreach ($spv in 'surface.vert.spv', 'surface.frag.spv', 'point.vert.spv', 'point.frag.spv') {
    $p = Join-Path $build $spv
    if (Test-Path $p) {
        $i = Get-Item $p
        Write-Host ("[build] shader : {0} {1,6:N0} bytes {2:yyyy-MM-dd HH:mm:ss}" -f $i.Name, $i.Length, $i.LastWriteTime)
    } else {
        Write-Host "[build] WARNING: $spv missing next to the DLL (the app loads .spv from the DLL directory)" -ForegroundColor Yellow
    }
}
