$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms

$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$bun = Join-Path $repository "node_modules\.bin\bun.exe"
$probe = Join-Path $PSScriptRoot "windows_clipboard_gate.ts"
if (-not (Test-Path -LiteralPath $bun -PathType Leaf)) {
    throw "Bun executable is missing: $bun"
}

$snapshot = [System.Windows.Forms.Clipboard]::GetDataObject()
try {
    $env:ZYRA_CLIPBOARD_GATE_CUSTODY = "powershell-restore-v1"
    $json = & $bun $probe
    if ($LASTEXITCODE -ne 0) {
        throw "Clipboard writer probe failed with exit code $LASTEXITCODE"
    }
    $receipt = $json | ConvertFrom-Json
    $actual = [System.Windows.Forms.Clipboard]::GetText()
    if ($actual -cne $receipt.marker) {
        throw "Clipboard content did not match the exact Unicode assistant message."
    }
    Write-Output $json
}
finally {
    Remove-Item Env:ZYRA_CLIPBOARD_GATE_CUSTODY -ErrorAction SilentlyContinue
    if ($null -eq $snapshot) {
        [System.Windows.Forms.Clipboard]::Clear()
    }
    else {
        [System.Windows.Forms.Clipboard]::SetDataObject($snapshot, $true)
    }
}
