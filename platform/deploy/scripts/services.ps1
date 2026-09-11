<#
.SYNOPSIS
    服装电商多 Agent 运营中台 —— 本地数据层服务管理（启动 / 停止 / 状态）

.DESCRIPTION
    管理本项目依赖的本地服务：Redis（任务状态、限流计数）、Qdrant（向量检索）。
    所有服务均为免安装的 portable 版本，装在 D:\develop 下，不污染系统。

.PARAMETER Action
    start  = 启动服务（已在运行的会跳过）
    stop   = 停止服务
    status = 查看端口与进程状态
    restart= 先停后起

.PARAMETER Only
    只操作指定服务，可多选：redis, qdrant。不传则全部。

.EXAMPLE
    .\services.ps1 status
    .\services.ps1 start
    .\services.ps1 restart -Only redis
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action = 'status',

    [ValidateSet('redis', 'qdrant')]
    [string[]]$Only = @('redis', 'qdrant')
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 路径配置：改这里就能换安装位置
# ---------------------------------------------------------------------------
$DevRoot      = 'D:\develop'
$DataRoot     = Join-Path $DevRoot 'data'
$LogRoot      = Join-Path $DevRoot 'logs'
$RepoRoot     = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # platform/
$RedisHome    = Join-Path $DevRoot 'redis'
$RedisConf    = Join-Path $RepoRoot 'deploy\redis\redis.conf'
# 【踩坑】msys2 版的 redis-server 只认 "/" 开头的绝对路径，Windows 的 D:\... 会被当成
# 相对路径拼到 cwd 后面。所以数据/日志只能放在 redis 安装目录下的相对位置。
$RedisData    = Join-Path $RedisHome 'data'
$QdrantHome   = Join-Path $DevRoot 'qdrant'
$QdrantData   = Join-Path $QdrantHome 'storage'

$RedisPort  = 6379
$QdrantPort = 6333

foreach ($d in @($DataRoot, $LogRoot, $RedisData, $QdrantData)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

function Write-Head($text) {
    Write-Host ''
    Write-Host "===== $text =====" -ForegroundColor Cyan
}

function Test-PortOpen([int]$Port) {
    # 用 TcpClient 探测端口，比 Test-NetConnection 快得多
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(400)) {
            $client.EndConnect($async)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
function Start-Redis {
    if (Test-PortOpen $RedisPort) {
        Write-Host "[redis ] 已在运行 (端口 $RedisPort)，跳过" -ForegroundColor Yellow
        return
    }
    $exe = Join-Path $RedisHome 'redis-server.exe'
    if (-not (Test-Path $exe)) {
        Write-Host "[redis ] 未找到 $exe，请先安装 Redis" -ForegroundColor Red
        return
    }

    # 把仓库里的配置同步一份到 Redis 安装目录，然后用相对文件名启动。
    # 这样命令里完全不出现绝对路径，绕开 msys2 的路径解析问题。
    $localConf = Join-Path $RedisHome 'redis.local.conf'
    Copy-Item $RedisConf $localConf -Force
    if (-not (Test-Path $RedisData)) { New-Item -ItemType Directory -Force -Path $RedisData | Out-Null }

    $log  = Join-Path $LogRoot 'redis.log'
    $err  = Join-Path $LogRoot 'redis.err.log'
    Start-Process -FilePath $exe -ArgumentList 'redis.local.conf' `
        -WorkingDirectory $RedisHome -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError $err | Out-Null

    # 等待端口就绪（最多 10s）
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-PortOpen $RedisPort) { break }
    }
    if (Test-PortOpen $RedisPort) {
        Write-Host "[redis ] 启动成功 -> 127.0.0.1:$RedisPort  日志: $log" -ForegroundColor Green
    } else {
        Write-Host "[redis ] 启动失败，请查看 $log" -ForegroundColor Red
    }
}

function Stop-Redis {
    if (-not (Test-PortOpen $RedisPort)) {
        Write-Host '[redis ] 未在运行' -ForegroundColor Yellow
        return
    }
    $cli = Join-Path $RedisHome 'redis-cli.exe'
    if (Test-Path $cli) {
        # shutdown 会把 AOF 刷盘后优雅退出，保证任务状态不丢
        & $cli -p $RedisPort shutdown nosave 2>$null | Out-Null
    }
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 250
        if (-not (Test-PortOpen $RedisPort)) { break }
    }
    if (-not (Test-PortOpen $RedisPort)) {
        Write-Host '[redis ] 已停止' -ForegroundColor Green
    } else {
        Write-Host '[redis ] 停止超时' -ForegroundColor Red
    }
}

# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------
function Start-Qdrant {
    if (Test-PortOpen $QdrantPort) {
        Write-Host "[qdrant] 已在运行 (端口 $QdrantPort)，跳过" -ForegroundColor Yellow
        return
    }
    $exe = Join-Path $QdrantHome 'qdrant.exe'
    if (-not (Test-Path $exe)) {
        Write-Host "[qdrant] 未找到 $exe，请先安装 Qdrant" -ForegroundColor Red
        return
    }
    $out = Join-Path $LogRoot 'qdrant.out.log'
    $err = Join-Path $LogRoot 'qdrant.err.log'
    $env:QDRANT__STORAGE__STORAGE_PATH = $QdrantData
    Start-Process -FilePath $exe -WorkingDirectory $QdrantHome `
        -RedirectStandardOutput $out -RedirectStandardError $err `
        -WindowStyle Hidden | Out-Null

    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-PortOpen $QdrantPort) { break }
    }
    if (Test-PortOpen $QdrantPort) {
        Write-Host "[qdrant] 启动成功 -> http://127.0.0.1:$QdrantPort/dashboard  数据: $QdrantData" -ForegroundColor Green
    } else {
        Write-Host "[qdrant] 启动失败，请查看 $err" -ForegroundColor Red
    }
}

function Stop-Qdrant {
    $procs = Get-Process -Name 'qdrant' -ErrorAction SilentlyContinue
    if (-not $procs) {
        Write-Host '[qdrant] 未在运行' -ForegroundColor Yellow
        return
    }
    $procs | Stop-Process -Force
    Write-Host '[qdrant] 已停止' -ForegroundColor Green
}

function Show-Status {
    $rows = @(
        [pscustomobject]@{ 服务 = 'Redis';  端口 = $RedisPort;  运行 = (Test-PortOpen $RedisPort);  路径 = $RedisHome }
        [pscustomobject]@{ 服务 = 'Qdrant'; 端口 = $QdrantPort; 运行 = (Test-PortOpen $QdrantPort); 路径 = $QdrantHome }
        [pscustomobject]@{ 服务 = 'MySQL';  端口 = 3306;        运行 = (Test-PortOpen 3306);        路径 = 'D:\develop\mysql\mysql_8' }
    )
    $rows | Format-Table -AutoSize
}

# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------
Write-Head "本地数据层服务 [$Action]"

$targets = @()
if ($Only -contains 'redis')  { $targets += 'redis' }
if ($Only -contains 'qdrant') { $targets += 'qdrant' }

switch ($Action) {
    'start' {
        foreach ($t in $targets) {
            if ($t -eq 'redis')  { Start-Redis }
            if ($t -eq 'qdrant') { Start-Qdrant }
        }
        Show-Status
    }
    'stop' {
        foreach ($t in $targets) {
            if ($t -eq 'redis')  { Stop-Redis }
            if ($t -eq 'qdrant') { Stop-Qdrant }
        }
        Show-Status
    }
    'restart' {
        foreach ($t in $targets) {
            if ($t -eq 'redis')  { Stop-Redis;  Start-Redis }
            if ($t -eq 'qdrant') { Stop-Qdrant; Start-Qdrant }
        }
        Show-Status
    }
    'status' { Show-Status }
}
