$activateScript = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"

if (-not (Test-Path -LiteralPath $activateScript -PathType Leaf)) {
    throw "Virtual environment activation script not found: $activateScript"
}

. $activateScript
