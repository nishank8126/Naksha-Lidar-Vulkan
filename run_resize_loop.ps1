# run_resize_loop.ps1 - run the resize regression test N times back to back,
# each with its own log tag, and print the exit code of every run.
# The desktop-pixel check has been seen to come back black in some runs and not
# others, so a single green run is not evidence; this makes the flake rate
# observable in one go.
param([int]$Runs = 2)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
for ($i = 1; $i -le $Runs; $i++) {
    $tag = "_r$i"
    Write-Output "=== run $i/$Runs (tag $tag) ==="
    & (Join-Path $root 'run_resize.ps1') -Tag $tag
    Write-Output "run $i exit: $(Get-Content (Join-Path $root "resize_exit$tag.txt") -Raw)"
}
Write-Output "=== loop done ==="