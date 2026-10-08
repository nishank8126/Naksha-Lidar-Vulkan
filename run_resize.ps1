# run_resize.ps1 - launch the Vulkan resize regression test detached and record
# its real exit code. Start-Process -Wait would block the calling shell for the
# ~6 minutes the run takes, so the wait happens in this helper instead.
#
# -Tag appends a suffix to every log so repeated runs can be compared side by
# side (the black-region flake only shows up in some runs).
param([string]$Tag = "")
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:NKV_ERR_LOG = Join-Path $root "resize_err$Tag.log"
Remove-Item Env:\NAKSHA_VULKAN_PREVIEW -ErrorAction SilentlyContinue
$p = Start-Process -FilePath (Join-Path $root 'venv\Scripts\python.exe') `
    -ArgumentList 'vulkan_resize_test.py' `
    -WorkingDirectory $root `
    -RedirectStandardOutput (Join-Path $root "resize_run$Tag.log") `
    -RedirectStandardError (Join-Path $root "resize_err$Tag.log") `
    -Wait -PassThru
"$($p.ExitCode)" | Set-Content -NoNewline (Join-Path $root "resize_exit$Tag.txt")
