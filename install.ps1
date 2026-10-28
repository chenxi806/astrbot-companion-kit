#requires -Version 5.1
<#
AstrBot Companion Kit · 一键安装
用法：
  .\install.ps1
  .\install.ps1 -DataDir "E:\bot\instances\xxx\core\data" -MemoryDir "D:\companion\memory"

它会做四件事：
  1. 把插件复制到 AstrBot 的 data\plugins
  2. 把记忆模板复制到 MemoryDir
  3. 按 MemoryDir 生成插件配置
  4. 把示例人格写进数据库（失败则提示在 WebUI 里粘贴）
#>

param(
    [string]$DataDir = "",
    [string]$MemoryDir = "D:\companion\memory",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path

function Say($msg) { Write-Host $msg }
function Ok($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "[X] $msg" -ForegroundColor Red }

Say "=== AstrBot Companion Kit 安装器 ==="
Say "仓库：$Repo"

# ---------- 1. 找 AstrBot 数据目录 ----------
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $candidates = @()
    foreach ($root in @("E:\bot\instances", "D:\bot\instances", "$env:USERPROFILE\AstrBot", "C:\AstrBot")) {
        if (Test-Path $root) {
            $candidates += Get-ChildItem $root -Recurse -Depth 4 -Filter "data_v4.db" -File -ErrorAction SilentlyContinue |
                ForEach-Object { $_.DirectoryName }
        }
    }
    $candidates = $candidates | Select-Object -Unique
    if ($candidates.Count -eq 1) {
        $DataDir = $candidates[0]
        Ok "自动找到数据目录：$DataDir"
    } elseif ($candidates.Count -gt 1) {
        Say "找到多个数据目录："
        for ($i = 0; $i -lt $candidates.Count; $i++) { Say "  [$($i+1)] $($candidates[$i])" }
        $pick = Read-Host "选一个序号"
        $DataDir = $candidates[[int]$pick - 1]
    } else {
        $DataDir = Read-Host "没自动找到，请输入 AstrBot 数据目录（含 data_v4.db 的那一层）"
    }
}

if (-not (Test-Path (Join-Path $DataDir "data_v4.db"))) {
    Fail "这个目录里没有 data_v4.db：$DataDir"
    Fail "请填 AstrBot 的数据目录（通常叫 data），不是安装目录。"
    exit 1
}
Ok "数据目录：$DataDir"

if ([string]::IsNullOrWhiteSpace($MemoryDir)) { $MemoryDir = Read-Host "记忆目录（回车用 D:\companion\memory）" }
if ([string]::IsNullOrWhiteSpace($MemoryDir)) { $MemoryDir = "D:\companion\memory" }
Say "记忆目录：$MemoryDir"

# ---------- 2. 建记忆目录 ----------
foreach ($sub in @("", "card", "voice_pool", "modes", "raw")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $MemoryDir $sub) | Out-Null
}
Ok "记忆目录已建好"

# ---------- 3. 复制记忆模板 ----------
$template = Join-Path $Repo "memory-template"
Copy-Item (Join-Path $template "core-persona.example.txt") (Join-Path $MemoryDir "core-persona.txt") -Force
Copy-Item (Join-Path $template "lorebook.example.json") (Join-Path $MemoryDir "lorebook.json") -Force
if (Test-Path (Join-Path $template "memes.example.json")) {
    Copy-Item (Join-Path $template "memes.example.json") (Join-Path $MemoryDir "memes.json") -Force
} else {
    "{}" | Set-Content (Join-Path $MemoryDir "memes.json") -Encoding UTF8
}
Copy-Item (Join-Path $template "card\store.json") (Join-Path $MemoryDir "card\store.json") -Force
Copy-Item (Join-Path $template "card\规范.md") (Join-Path $MemoryDir "card\规范.md") -Force
Copy-Item (Join-Path $template "voice_pool\examples.jsonl") (Join-Path $MemoryDir "voice_pool\examples.jsonl") -Force
Copy-Item (Join-Path $template "modes\alice_corpus.json") (Join-Path $MemoryDir "modes\alice_corpus.json") -Force
Copy-Item (Join-Path $template "modes\state.json") (Join-Path $MemoryDir "modes\state.json") -Force
Ok "记忆模板已复制"

# ---------- 4. 复制插件 ----------
$pluginSrc = Join-Path $Repo "plugins"
$pluginDst = Join-Path $DataDir "plugins"
New-Item -ItemType Directory -Force -Path $pluginDst | Out-Null
$count = 0
Get-ChildItem $pluginSrc -Directory | ForEach-Object {
    Copy-Item $_.FullName $pluginDst -Recurse -Force
    $count++
}
Ok "已复制 $count 个插件到 $pluginDst"

# ---------- 5. 写配置 ----------
$cfgSrc = Join-Path $Repo "config"
$cfgDst = Join-Path $DataDir "config"
New-Item -ItemType Directory -Force -Path $cfgDst | Out-Null
$escaped = $MemoryDir.Replace("\", "\\")
Get-ChildItem $cfgSrc -Filter "*.json" | ForEach-Object {
    $raw = Get-Content $_.FullName -Raw -Encoding UTF8
    $raw = $raw.Replace("__MEMORY_DIR__", $escaped)
    $out = Join-Path $cfgDst $_.Name
    [System.IO.File]::WriteAllText($out, $raw, (New-Object System.Text.UTF8Encoding($false)))
}
Ok "插件配置已写入（memory_dir = $MemoryDir）"

# ---------- 6. 写人格 ----------
$personaFile = Join-Path $MemoryDir "core-persona.txt"
$writer = Join-Path $Repo "tools\write_persona.py"
$python = ""
$candidates = @()
$p1 = Split-Path -Parent $DataDir
if (-not [string]::IsNullOrWhiteSpace($p1)) {
    $candidates += (Join-Path $p1 "venv\Scripts\python.exe")
    $p2 = Split-Path -Parent $p1
    if (-not [string]::IsNullOrWhiteSpace($p2)) {
        $candidates += (Join-Path $p2 "venv\Scripts\python.exe")
    }
}
foreach ($p in $candidates) {
    if (Test-Path $p) { $python = $p; break }
}
if (-not $python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}

if ($python -and (Test-Path $writer)) {
    Say "写入人格到数据库..."
    & $python $writer --db (Join-Path $DataDir "data_v4.db") --file $personaFile
    if ($LASTEXITCODE -eq 0) { Ok "人格已写入" }
    else { Warn "人格写入失败，请在 WebUI → 人格 里粘贴 $personaFile 的内容" }
} else {
    Warn "没找到 Python，跳过写库。请在 WebUI → 人格 里粘贴 $personaFile 的内容，或手动运行："
    Say "  python `"$writer`" --db `"$(Join-Path $DataDir 'data_v4.db')`" --file `"$personaFile`""
}

# ---------- 7. 提示后续 ----------
Say ""
Say "=== 安装完成 ==="
Say "还需要在 AstrBot WebUI 里做两件事（只做一次）："
Say "  1. 模型提供商 → 填 DeepSeek API Key（Base: https://api.deepseek.com/v1，模型 deepseek-flash）"
Say "  2. 消息平台 → QQ 官方机器人，填 AppID / AppSecret / Token，并把你的 QQ 加进沙箱"
Say ""
Say "然后重启 AstrBot，私聊发「你好」。"
Say "更多说明：README.md / docs/quickstart.md"
