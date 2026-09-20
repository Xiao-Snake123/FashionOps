<#
.SYNOPSIS
    服装电商多 Agent 运营中台 —— 停止前端 / 后端（可选连数据层一起停）

.DESCRIPTION
    按**端口**找到占用它的进程并停掉：
      - 3000 → 前端（Vite / node）
      - 8000 → 后端（uvicorn / python）

    ## 为什么按端口找，而不是记 PID

    后端是用 `Start-Process -WindowStyle Hidden` 起的，PID 没有落到任何文件里，
    而且机器重启/窗口关掉之后 PID 文件本身就是过期数据（还会误杀别的进程）。
    端口是唯一稳定的标识 —— 找到占用者再**校验进程名**，两者都对上才动手，
    避免把占着 8000 的别的项目的服务杀掉（这台机器上真发生过）。

.PARAMETER BackendPort
    后端端口，默认 8000。

.PARAMETER FrontendPort
    前端端口，默认 3000。

.PARAMETER All
    连 Redis / Qdrant 一起停（等于再调一次 services.ps1 stop）。

.EXAMPLE
    .\stop.ps1
    .\stop.ps1 -All
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$Services = Join-Path $PSScriptRoot 'services.ps1'

function Write-Head($text) {
    Write-Host ''
    Write-Host "===== $text =====" -ForegroundColor Cyan
}

function Stop-OnPort([int]$Port, [string]$Label, [string[]]$ExpectNames) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conn) {
        Write-Host "[$Label] 端口 $Port 没有在监听，跳过" -ForegroundColor Yellow
        return
    }

    foreach ($c in $conn) {
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if (-not $proc) { continue }

        # 校验进程名：名字对不上就不动它 —— 端口可能被别的程序占着
        $matched = $false
        foreach ($n in $ExpectNames) {
            if ($proc.ProcessName -like $n) { $matched = $true; break }
        }
        if (-not $matched) {
            Write-Host "[$Label] 端口 $Port 被 [$($proc.ProcessName)] 占用，不是本项目进程，不动它" -ForegroundColor Yellow
            continue
        }

        Write-Host "[$Label] 停止 $($proc.ProcessName) (PID $($proc.Id))，端口 $Port" -ForegroundColor Green
        Stop-Process -Id $proc.Id -Force
    }
}

Write-Head '停止 FashionOps'

Stop-OnPort -Port $FrontendPort -Label 'front' -ExpectNames @('node*')
Stop-OnPort -Port $BackendPort  -Label 'back ' -ExpectNames @('python*')

if ($All) {
    Write-Head '数据层（Redis + Qdrant）'
    & $Services stop
} else {
    Write-Host ''
    Write-Host '  数据层（Redis / Qdrant）保持运行；要一起停：.\stop.ps1 -All' -ForegroundColor DarkGray
}
