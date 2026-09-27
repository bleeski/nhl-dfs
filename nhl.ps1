Push-Location $PSScriptRoot
try {
    $venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Error "No .venv found at $venvPython. Run 'uv sync' first."
        exit 1
    }
    & $venvPython -m nhl_dfs.cli @Args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
