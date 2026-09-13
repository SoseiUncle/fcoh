#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$source = Join-Path $projectRoot 'adapters\fusion\FcohBridge'
$target = Join-Path $env:APPDATA 'Autodesk\Autodesk Fusion 360\API\AddIns\FcohBridge'
if (Test-Path -LiteralPath $target) {
    $backup = Join-Path $projectRoot ('.runtime\backups\FcohBridge-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path (Split-Path $backup) -Force | Out-Null
    Copy-Item -LiteralPath $target -Destination $backup -Recurse
}
New-Item -ItemType Directory -Path $target -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'FcohBridge.py') -Destination $target
Copy-Item -LiteralPath (Join-Path $source 'FcohBridge.manifest') -Destination $target
@{root=$projectRoot} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $target 'project-root.json') -Encoding utf8
[pscustomobject]@{Installed=$target;Project=$projectRoot;Next='Start Fusion; if necessary enable FcohBridge in Scripts and Add-Ins.'} | ConvertTo-Json
