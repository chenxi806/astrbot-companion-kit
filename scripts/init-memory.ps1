param(
    [Parameter(Mandatory = $true)]
    [string]$Target,

    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = "Stop"

$template = Join-Path $RepoRoot "memory-template"
if (-not (Test-Path $template)) {
    throw "找不到 memory-template：$template"
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Target "events") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Target "proposals") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Target "raw") | Out-Null

Copy-Item (Join-Path $template "store.json") $Target -Force
Copy-Item (Join-Path $template "render.py") $Target -Force
Copy-Item (Join-Path $template "whims.json") $Target -Force

# 世界书：有 example 就复制成正式文件；已经存在则不覆盖
$lore = Join-Path $Target "lorebook.json"
if (-not (Test-Path $lore)) {
    Copy-Item (Join-Path $template "lorebook.example.json") $lore -Force
}

# 核心人格只作为参考，不直接覆盖线上人格
Copy-Item (Join-Path $template "core-persona.example.md") (Join-Path $Target "core-persona.example.md") -Force

Write-Host "记忆目录已初始化：$Target" -ForegroundColor Green
Write-Host ""
Write-Host "接下来把六个插件的 memory_dir 都指向这个目录，重启 AstrBot。" -ForegroundColor Cyan
