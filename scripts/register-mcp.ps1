#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup.ps1 first' }
codex mcp add fcoh -- $python -m fcoh.server
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed' }
codex mcp get fcoh
