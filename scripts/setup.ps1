#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $projectRoot
$archive = Join-Path $projectRoot 'downloads\FreeCAD_1.1.3-Windows-x86_64-py311.7z'
$expected = '9c6959dc9c4dba64dd818a62447e3dfedb4221d776fb044b239d462f150bcec4'
$runtime = Join-Path $projectRoot 'vendor\FreeCAD_1.1.3-Windows-x86_64-py311\bin\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    New-Item -ItemType Directory -Path (Split-Path $archive),(Join-Path $projectRoot 'vendor') -Force | Out-Null
    if (-not (Test-Path -LiteralPath $archive)) {
        Invoke-WebRequest 'https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-Windows-x86_64-py311.7z' -OutFile $archive
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw 'FreeCAD checksum mismatch' }
    $sevenZip = 'C:\Program Files\7-Zip\7z.exe'
    if (-not (Test-Path -LiteralPath $sevenZip)) { throw '7-Zip is required to extract the portable distribution' }
    & $sevenZip x $archive ('-o' + (Join-Path $projectRoot 'vendor')) -y -bso0 -bsp0
    if ($LASTEXITCODE -ne 0) { throw 'FreeCAD extraction failed' }
}
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' }
& (Join-Path $projectRoot '.venv\Scripts\python.exe') -m fcoh.cli capabilities
