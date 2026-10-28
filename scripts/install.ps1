<#
.SYNOPSIS
    安装 Companion Memory Kit 到 AstrBot。

.EXAMPLE
    .\install.ps1 -AstrBotData "C:\AstrBot\data" -MemoryDir "C:\companion-memory"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$AstrBotData,

    [Parameter(Mandatory = $true)]
    [string]$MemoryDir
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot

$plugins = @(
    "memory_card",
    "card_review",
    "voice_pool",
    "pool_review",
    "mode_switch"
)

New-Item -ItemType Directory -Force -Path "$AstrBotData\plugins", "$AstrBotData\config" | Out-Null

foreach ($name in $plugins) {
    $src = Join-Path $Repo "plugins\astrbot_plugin_$name"
    $dst = Join-Path $AstrBotData "plugins\astrbot_plugin_$name"
    if (Test-Path $dst) {
        Write-Host "跳过已存在：$name"
        continue
    }
    Copy-Item $src $dst -Recurse
    Write-Host "安装插件：$name"
}

foreach ($name in $plugins) {
    $src = Join-Path $Repo "config\astrbot_plugin_${name}_config.json"
    $dst = Join-Path $AstrBotData "config\astrbot_plugin_${name}_config.json"
    if (Test-Path $dst) {
        Write-Host "跳过已存在配置：$name"
        continue
    }
    $cfg = Get-Content $src -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg | Add-Member -NotePropertyName memory_dir -NotePropertyValue $MemoryDir -Force
    $cfg | ConvertTo-Json -Depth 10 | Set-Content $dst -Encoding UTF8
    Write-Host "写入配置：$name"
}

$memSrc = Join-Path $Repo "memory-template"
if (-not (Test-Path $MemoryDir)) {
    New-Item -ItemType Directory -Force -Path $MemoryDir | Out-Null
}
Copy-Item "$memSrc\*" $MemoryDir -Recurse -Force
Write-Host "记忆模板已复制到：$MemoryDir"

Write-Host ""
Write-Host "完成。重启 AstrBot 后发送 /card 验证。"
