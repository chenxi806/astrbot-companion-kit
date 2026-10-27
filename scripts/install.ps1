param(
    [Parameter(Mandatory = $true)]
    [string]$AstrBotData,

    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = "Stop"

$pluginsSrc = Join-Path $RepoRoot "plugins"
$pluginsDst = Join-Path $AstrBotData "plugins"
$configDst = Join-Path $AstrBotData "config"

if (-not (Test-Path $pluginsDst)) {
    throw "找不到插件目录：$pluginsDst（确认 -AstrBotData 指向 AstrBot 的 data 目录）"
}

Get-ChildItem $pluginsSrc -Directory | ForEach-Object {
    $target = Join-Path $pluginsDst $_.Name
    if (Test-Path $target) {
        Write-Host "已存在，跳过：$($_.Name)（要覆盖请先手动删除）" -ForegroundColor Yellow
    }
    else {
        Copy-Item $_.FullName $target -Recurse
        Write-Host "已安装：$($_.Name)" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "下一步：" -ForegroundColor Cyan
Write-Host "1. 初始化记忆目录：.\scripts\init-memory.ps1 -Target `"$AstrBotData\companion_memory`""
Write-Host "2. 重启 AstrBot，让它生成默认配置"
Write-Host "3. 改 data/config/astrbot_plugin_*_config.json 里的 memory_dir"
Write-Host "4. 再重启一次"
