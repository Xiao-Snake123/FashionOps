<#
.SYNOPSIS
    服装电商多 Agent 运营中台 —— 一键启动（数据层 → 后端 → 前端）

.DESCRIPTION
    按顺序把整个项目拉起来：
      1. 数据层：Redis + Qdrant（复用 services.ps1）
      2. 校验数据库迁移版本（没初始化就自动 init）
      3. 后端：uvicorn（后台进程，日志写在 D:\develop\logs）
      4. 前端：Vite 开发服务器

    每一步都是**幂等**的：已经在跑的就跳过，不会起第二份。
    所以可以放心重复执行 —— 这也是它存在的理由：
    "起项目"这件事不该要求人记住三条命令和它们的顺序。

.PARAMETER BackendPort
    后端端口，默认 8000。前端会用它拼 VITE_API_TARGET。

.PARAMETER FrontendPort
    前端端口，默认 3000。

.PARAMETER NoFrontend
    只起后端和依赖服务（比如你要用自己已经开好的前端）。

.PARAMETER SkipData
    跳过 Redis/Qdrant 的启动（数据层由外部管理，例如跑在 Docker 里）。

.EXAMPLE
    .\start.ps1
    .\start.ps1 -BackendPort 8010
    .\start.ps1 -NoFrontend
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000,
    [switch]$NoFrontend,
    [switch]$SkipData
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # platform/
$FrontendDir = Join-Path $RepoRoot 'frontend'
$VenvPython  = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$DbCli       = Join-Path $RepoRoot '.venv\Scripts\fashionops-db.exe'
$Services    = Join-Path $PSScriptRoot 'services.ps1'
$LogRoot     = 'D:\develop\logs'

$BackendLog  = Join-Path $LogRoot 'backend.out.log'
$BackendErr  = Join-Path $LogRoot 'backend.err.log'
$FrontendLog = Join-Path $LogRoot 'frontend.out.log'
$FrontendErr = Join-Path $LogRoot 'frontend.err.log'

foreach ($d in @($LogRoot)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

function Write-Head($text) {
    Write-Host ''
    Write-Host "===== $text =====" -ForegroundColor Cyan
}

function Test-PortOpen([int]$Port) {
    # 用 TcpClient 探测端口，比 Test-NetConnection 快得多。
    #
    # ⚠ 必须同时试 IPv4 / IPv6：Vite 默认只监听 ::1（所以前端**只能**用
    #   http://localhost:3000 打开，127.0.0.1 连不上）。只探 127.0.0.1 的话，
    #   明明起来了也会被判成"启动超时" —— 这个坑就是这么来的。
    foreach ($probe in @(
        @{ Host = '127.0.0.1'; Family = [System.Net.Sockets.AddressFamily]::InterNetwork },
        @{ Host = '::1';       Family = [System.Net.Sockets.AddressFamily]::InterNetworkV6 }
    )) {
        # 探 IPv6 必须显式指定地址族：默认构造出来的 TcpClient 是 IPv4 的，
        # 拿它连 "::1" 会直接报"地址族不支持"，于是又变成误判超时
        $client = New-Object System.Net.Sockets.TcpClient($probe.Family)
        $host_ = $probe.Host
        try {
            $async = $client.BeginConnect($host_, $Port, $null, $null)
            if ($async.AsyncWaitHandle.WaitOne(400)) {
                $client.EndConnect($async)
                return $true
            }
        } catch {
            # 这个地址连不上，试下一个
        } finally {
            $client.Close()
        }
    }
    return $false
}

function Wait-ForPort([int]$Port, [int]$Seconds, [string]$Name) {
    for ($i = 0; $i -lt ($Seconds * 4); $i++) {
        if (Test-PortOpen $Port) { return $true }
        Start-Sleep -Milliseconds 250
    }
    Write-Host "[$Name] 等待端口 $Port 超时（${Seconds}s），去看日志" -ForegroundColor Red
    return $false
}

# ---------------------------------------------------------------------------
# 0. 前置检查
# ---------------------------------------------------------------------------
Write-Head '一键启动 FashionOps'

if (-not (Test-Path $VenvPython)) {
    Write-Host "找不到 $VenvPython" -ForegroundColor Red
    Write-Host '先在 platform/ 下装依赖：uv sync --all-extras（或 .venv\Scripts\python.exe -m pip install -e ".[dev]"）'
    exit 1
}

if (-not $SkipData) {
    if (-not (Test-PortOpen 3306)) {
        Write-Host '[mysql ] 3306 不通 —— 先跑 init-mysql.ps1 建库' -ForegroundColor Red
        exit 1
    }
    Write-Host '[mysql ] 已就绪 (3306)' -ForegroundColor Green

    Write-Head '1/4 数据层（Redis + Qdrant）'
    & $Services start
} else {
    Write-Head '1/4 数据层：已跳过（-SkipData）'
}

# ---------------------------------------------------------------------------
# 2. 数据库迁移版本
# ---------------------------------------------------------------------------
Write-Head '2/4 数据库迁移'

$current = & $DbCli current 2>&1 | Out-String
if ($current -match 'Rev:\s+(\S+)') {
    $rev = $Matches[1]
    if ($current -match '\(head\)') {
        Write-Host "[db    ] 已是最新版本 $rev" -ForegroundColor Green
    } else {
        Write-Host "[db    ] 当前版本 $rev **不是** head，建议跑：.venv\Scripts\fashionops-db.exe upgrade" -ForegroundColor Yellow
    }
} else {
    Write-Host '[db    ] 库里还没有迁移记录，自动执行 init…' -ForegroundColor Yellow
    & $DbCli init
}

# ---------------------------------------------------------------------------
# 3. 后端
# ---------------------------------------------------------------------------
Write-Head "3/4 后端 (端口 $BackendPort)"

if (Test-PortOpen $BackendPort) {
    Write-Host "[back  ] 端口 $BackendPort 已被占用，认为后端已在运行（要重启请先 .\stop.ps1）" -ForegroundColor Yellow
} else {
    # ⚠ 必须用 Start-Process：前台跑的话，终端会话一结束进程就被带走，
    #    表现是"刚才还好好的，刷个页面就全 404"
    Start-Process -FilePath $VenvPython `
        -ArgumentList '-m','uvicorn','fashionops.api.main:app','--host','127.0.0.1','--port',$BackendPort `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden `
        -RedirectStandardOutput $BackendLog -RedirectStandardError $BackendErr | Out-Null

    if (Wait-ForPort $BackendPort 40 'back') {
        Write-Host "[back  ] 已启动 -> http://127.0.0.1:$BackendPort  (日志: $BackendLog)" -ForegroundColor Green
    }
}

# 健康检查：能拿到 /health 才算真的起来了（端口通不代表依赖都通）
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/health" -TimeoutSec 15
    $bad = @($health.dependencies | Where-Object { -not $_.ok })
    if ($bad.Count -eq 0) {
        Write-Host "[back  ] 健康检查: ok（三个依赖全通）" -ForegroundColor Green
    } else {
        Write-Host "[back  ] 健康检查: $($health.status) —— 未就绪: $($bad.name -join ', ')" -ForegroundColor Yellow
        Write-Host '         项目照样能跑；缺 Redis 时限流/熔断降级，缺 Qdrant 时知识库检索不可用'
    }
} catch {
    Write-Host "[back  ] 健康检查失败: $($_.Exception.Message)" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# 4. 前端
# ---------------------------------------------------------------------------
if ($NoFrontend) {
    Write-Head '4/4 前端：已跳过（-NoFrontend）'
} else {
    Write-Head "4/4 前端 (端口 $FrontendPort)"

    if (Test-PortOpen $FrontendPort) {
        Write-Host "[front ] 端口 $FrontendPort 已被占用，认为前端已在运行" -ForegroundColor Yellow
    } else {
        # npm 在本机解析到的是 npm.ps1，而 Start-Process 起不了 .ps1
        # （报 "%1 is not a valid Win32 application"），所以这里要找 npm.cmd
        $npmCmd = Join-Path (Split-Path (Get-Command npm).Source) 'npm.cmd'
        if (-not (Test-Path $npmCmd)) {
            Write-Host "[front ] 找不到 npm.cmd，请确认 Node.js 已安装" -ForegroundColor Red
        } else {
            $env:VITE_API_TARGET = "http://127.0.0.1:$BackendPort"
            Start-Process -FilePath $npmCmd -ArgumentList 'run','dev' `
                -WorkingDirectory $FrontendDir -WindowStyle Hidden `
                -RedirectStandardOutput $FrontendLog -RedirectStandardError $FrontendErr | Out-Null
            Remove-Item Env:\VITE_API_TARGET

            if (Wait-ForPort $FrontendPort 60 'front') {
                Write-Host "[front ] 已启动 -> http://localhost:$FrontendPort  (日志: $FrontendLog)" -ForegroundColor Green
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 总结
# ---------------------------------------------------------------------------
Write-Head '启动完成'
Write-Host "  前端页面   : http://localhost:$FrontendPort" -ForegroundColor White
Write-Host "  后端接口   : http://127.0.0.1:$BackendPort/docs" -ForegroundColor White
Write-Host "  健康检查   : http://127.0.0.1:$BackendPort/health" -ForegroundColor White
Write-Host ''
Write-Host '  演示账号   : demo / FashionOps@2026（admin）' -ForegroundColor White
Write-Host ''
Write-Host "  停止       : cd `"$PSScriptRoot`"; .\stop.ps1" -ForegroundColor DarkGray
Write-Host "  看数据层   : .\services.ps1 status" -ForegroundColor DarkGray
Write-Host ''
Write-Host '  ⚠ 前端只能用 localhost（IPv6 ::1），127.0.0.1 连不上' -ForegroundColor DarkGray
