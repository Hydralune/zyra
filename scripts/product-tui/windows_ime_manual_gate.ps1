$ErrorActionPreference = "Stop"
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$bun = Join-Path $repository "node_modules\.bin\bun.exe"
$gate = Join-Path $PSScriptRoot "manual_ime_process.ts"
if (-not (Test-Path -LiteralPath $bun -PathType Leaf)) {
    throw "Bun executable is missing: $bun"
}
& $bun $gate
exit $LASTEXITCODE
