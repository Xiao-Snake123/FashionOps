<#
.SYNOPSIS
    服装电商多 Agent 运营中台 —— 一键启动全栈（数据层 → 后端 → MCP → DeerFlow → 前端）

.DESCRIPTION
    按顺序把整个项目拉起来：
      1. 数据层：Redis + Qdrant（复用 services.ps1）
      2. 校验数据库迁移版本（没初始化就自动 init）
      3. 后端：uvicorn（后台进程，日志写在 D:\develop\logs）
      4. MCP 服务：把平台工具以 MCP/SSE 对外暴露
      5. DeerFlow sidecar（**可选**，默认不起，见 -WithDeerFlow）
      6. 前端：Vite 开发服务器

    每一步都是**幂等**的：已经在跑的就跳过，不会起第二份。
    所以可以放心重复执行 —— 这也是它存在的理由：
    "起项目"这件事不该要求人记住六条命令和它们的顺序。

.PARAMETER BackendPort
    后端端口，默认 **8010**。前端会用它拼 VITE_API_TARGET。
    ⚠ 刻意不用 8000：本机另一个股票系统的 FastAPI/WebUI 也默认占 8000。

.PARAMETER FrontendPort
    前端端口，默认 3000。（那个股票系统的前端用 5173，不冲突）

.PARAMETER McpPort
    MCP/SSE 服务端口，默认 8100。外部 Agent（DeerFlow、Claude Desktop 等）
    连 http://127.0.0.1:8100/mcp/sse 用平台工具。

.PARAMETER DeerFlowPort
    DeerFlow sidecar 端口，默认 **8011**。
    ⚠ 刻意不用 8001：本机那个股票系统的 Java 后端（dsa-server.jar）常驻 8001。

.PARAMETER NoFrontend
    只起后端和依赖服务（比如你要用自己已经开好的前端）。

.PARAMETER NoMcp
    不起 MCP/SSE 服务。只在确认没有外部 Agent 要连平台工具时才用。

.PARAMETER SkipData
    跳过 Redis/Qdrant 的启动（数据层由外部管理，例如跑在 Docker 里）。

.PARAMETER WithDeerFlow
    连 DeerFlow sidecar（:8011）一起起。默认**不起**，理由见脚本里 5/6 那段：
    它有自己的 venv 和配置，不是每台机器都有；而且默认 orchestrator_backend
    是 legacy，那时候起了也没人调它。

.PARAMETER DeerFlowRoot
    DeerFlow 的目录。不指定时按顺序找这几个位置，全找不到就跳过：
      ① platform/ 的同级目录
      ② 再往上一层（本机实际就在这一层 —— platform 在 Work agent\、deerflow 在 Desktop\）
      ③ 环境变量 FASHIONOPS_DEERFLOW_ROOT
    它不是 platform 的必需依赖，找不到不影响主流程起来。

.EXAMPLE
    .\start.ps1
    .\start.ps1 -BackendPort 8020
    .\start.ps1 -NoFrontend
    .\start.ps1 -WithDeerFlow
#>
[CmdletBinding()]
param(
    # ⚠ 默认端口刻意避开 8000 / 8001 —— 那两个是本机另一个股票系统的默认端口。
    #   端口表与避让理由见 platform/docs/14-端口清单.md。
    [int]$BackendPort = 8010,
    [int]$FrontendPort = 3000,
    [int]$McpPort = 8100,
    [int]$DeerFlowPort = 8011,
    [switch]$NoFrontend,
    [switch]$NoMcp,
    [switch]$SkipData,
    [switch]$WithDeerFlow,
    [string]$DeerFlowRoot = ''
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
$McpLog      = Join-Path $LogRoot 'mcp.out.log'
$McpErr      = Join-Path $LogRoot 'mcp.err.log'
$DeerFlowLog = Join-Path $LogRoot 'deerflow.out.log'
$DeerFlowErr = Join-Path $LogRoot 'deerflow.err.log'
$FrontendLog = Join-Path $LogRoot 'frontend.out.log'
$FrontendErr = Join-Path $LogRoot 'frontend.err.log'

# DeerFlow 的位置。**它不一定是 platform/ 的同级目录** ——
# 本机就是 platform 在 `Desktop\Work agent\`、而 deerflow 在 `Desktop\`，差了一层。
# 所以按候选依次找，而不是只赌一个位置。
# 全猜不到也**不报错** —— 它是可选组件，缺它不该让主流程起不来。
if (-not $DeerFlowRoot) {
    $platformParent = Split-Path -Parent $RepoRoot
    $candidates = @(
        (Join-Path $platformParent 'deerflow')                               # ① platform 的同级
        (Join-Path (Split-Path -Parent $platformParent) 'deerflow')          # ② 再上一层（本机实际位置）
        $env:FASHIONOPS_DEERFLOW_ROOT                                        # ③ 环境变量兜底
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            $DeerFlowRoot = $candidate
            break
        }
    }
}

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

function Get-PortOwner([int]$Port) {
    <#
    返回占用该端口的进程（含**命令行**）；没人占就返回 $null。

    ## 为什么必须查命令行，光看端口甚至光看进程名都不够

    这是实测撞出来的，而且撞了两次：

      1. 本机 `:8001` 常驻那个股票系统的 Java 后端（dsa-server.jar）——
         只看端口的话，"DeerFlow 起来了吗"的答案永远是"起来了"，
         脚本于是跳过启动，**什么都不做，还不报错**。
         （platform 侧因此把 DeerFlow 挪到了 8011。）
      2. 更隐蔽的一次：本机 `:8000` 被那个股票系统的 python 占着
         （`Anaconda\python.exe main.py --webui-only`）。
         于是脚本认为"后端已在运行"，而**平台后端其实根本没起**，
         后面那句"健康检查 ok"其实是打到了那个外部程序的 /health 上。
         （platform 侧因此把后端挪到了 8010。）

    第 2 条说明**光校验进程名也没用** —— 冒名者同样是 python.exe。
    唯一可靠的判据是命令行里有没有我们的模块名。

    `stop.ps1` 用进程名校验来避免误杀（目的：别杀错人）；
    这里是同一个判断的反向用法（目的：**别被别的进程骗过**）。
    #>
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $null }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    return [pscustomobject]@{
        Pid          = $conn.OwningProcess
        Name         = $p.Name
        CommandLine  = $p.CommandLine
    }
}

function Test-PortServedBy([int]$Port, [string]$MustContain) {
    <#
    端口上跑的是不是**我们的**服务 —— 用命令行里的一段特征串判断。

    $MustContain 取值见各步调用处（如 'fashionops.api.main'）。
    端口没人占也返回 $false（调用方据此决定要不要启动）。
    #>
    $owner = Get-PortOwner $Port
    if (-not $owner) { return $false }
    if (-not $owner.CommandLine) { return $false }
    return ($owner.CommandLine -like "*$MustContain*")
}

function Write-PortTakenWarning([int]$Port, [string]$Label, [string]$What) {
    <#
    端口被**别人的**服务占着时统一报警。

    刻意写成显眼的红字并给出两条出路：这种情况下脚本**什么都不会做**，
    如果不说清楚，用户会以为服务起来了，然后对着一个根本不存在的
    后端查半天"为什么接口 404"。
    #>
    $owner = Get-PortOwner $Port
    $who = if ($owner) { "$($owner.Name) (PID $($owner.Pid))" } else { '未知进程' }
    Write-Host "[$Label] ⚠ 端口 $Port 被 **$who** 占着，不是$What" -ForegroundColor Red
    if ($owner -and $owner.CommandLine) {
        Write-Host "         它的命令行: $($owner.CommandLine)" -ForegroundColor DarkGray
    }
    Write-Host "         所以这一步**跳过启动**（$What 并没有起来）。先停掉它，或换个端口重跑。" -ForegroundColor Yellow
}

function Get-OrchestratorBackend {
    <#
    读"当前用哪个编排后端"，用来判断 DeerFlow 该不该起。

    取值顺序刻意和 Pydantic Settings 一致：**进程环境变量优先于 .env**。
    反过来写的话，在终端里临时设了 FASHIONOPS_ORCHESTRATOR_BACKEND=deerflow
    想试一下，结果脚本读 .env 读回 legacy、DeerFlow 没起来 —— 这种
    "我明明配了却没生效"最难排查。
    #>
    if ($env:FASHIONOPS_ORCHESTRATOR_BACKEND) {
        return $env:FASHIONOPS_ORCHESTRATOR_BACKEND.Trim()
    }
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path $envFile) {
        $m = Select-String -Path $envFile -Pattern '^\s*FASHIONOPS_ORCHESTRATOR_BACKEND\s*=' |
            Select-Object -First 1
        # 用 -split 的第二个参数限制切 1 次：值里再出现 '=' 也不会被截断
        if ($m) { return ($m.Line -split '=', 2)[1].Trim() }
    }
    return 'legacy'
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

    Write-Head '1/6 数据层（Redis + Qdrant）'
    & $Services start
} else {
    Write-Head '1/6 数据层：已跳过（-SkipData）'
}

# ---------------------------------------------------------------------------
# 2. 数据库迁移版本
# ---------------------------------------------------------------------------
Write-Head '2/6 数据库迁移'

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
Write-Head "3/6 后端 (端口 $BackendPort)"

# ⚠ 判断"后端在不在"必须看**命令行**（Test-PortServedBy），不能只看端口。
#   本机 :8000 曾经被另一个 python 占着（Anaconda 的 `main.py --webui-only`），
#   于是旧版脚本认为"后端已在运行"、跳过启动，而平台后端其实**根本没起**；
#   后面那句"健康检查 ok"打的是那个外部程序的 /health。详见 Get-PortOwner。
$backendUs = Test-PortServedBy $BackendPort 'fashionops.api.main'

if ($backendUs) {
    Write-Host "[back  ] 已在运行（要重启请先 .\stop.ps1）" -ForegroundColor Yellow
} elseif (Test-PortOpen $BackendPort) {
    Write-PortTakenWarning $BackendPort 'back ' '平台后端'
} else {
    # ⚠ 必须用 Start-Process：前台跑的话，终端会话一结束进程就被带走，
    #    表现是"刚才还好好的，刷个页面就全 404"
    Start-Process -FilePath $VenvPython `
        -ArgumentList '-m','uvicorn','fashionops.api.main:app','--host','127.0.0.1','--port',$BackendPort `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden `
        -RedirectStandardOutput $BackendLog -RedirectStandardError $BackendErr | Out-Null

    if (Wait-ForPort $BackendPort 40 'back') {
        Write-Host "[back  ] 已启动 -> http://127.0.0.1:$BackendPort  (日志: $BackendLog)" -ForegroundColor Green
        $backendUs = $true
    }
}

# 健康检查：能拿到 /health 才算真的起来了（端口通不代表依赖都通）。
# ⚠ 只在"后端确实是我们的"时才查 —— 端口若被别的程序占着，这里打到的是
#   那个程序的 /health，字段结构完全不同，结果毫无意义。
if ($backendUs) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/health" -TimeoutSec 15

        # ⚠ 不能假设一定有 dependencies：只回 {status, timestamp} 的版本也存在。
        #   直接写 $health.dependencies | Where-Object 会把 $null 当一个元素
        #   送进管道（因为 -not $null 为真），$bad 于是凭空多一个元素，
        #   走进 else 分支打出"ok —— 未就绪:"这种自相矛盾的话。
        $deps = @()
        if ($health.PSObject.Properties['dependencies']) { $deps = @($health.dependencies) }
        $bad = @($deps | Where-Object { $_ -and -not $_.ok })

        if ($bad.Count -gt 0) {
            Write-Host "[back  ] 健康检查: $($health.status) —— 未就绪: $($bad.name -join ', ')" -ForegroundColor Yellow
            Write-Host '         项目照样能跑；缺 Redis 时限流/熔断降级，缺 Qdrant 时知识库检索不可用'
        } elseif ($deps.Count -gt 0) {
            Write-Host "[back  ] 健康检查: ok（$($deps.Count) 个依赖全通）" -ForegroundColor Green
        } else {
            Write-Host "[back  ] 健康检查: $($health.status)" -ForegroundColor Green
        }
    } catch {
        Write-Host "[back  ] 健康检查失败: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# ---------------------------------------------------------------------------
# 4. MCP 服务（把平台工具以 MCP/SSE 对外暴露）
# ---------------------------------------------------------------------------
if ($NoMcp) {
    Write-Head '4/6 MCP 服务：已跳过（-NoMcp）'
} else {
    Write-Head "4/6 MCP 服务 (端口 $McpPort)"

    if (Test-PortServedBy $McpPort 'fashionops.mcp.server') {
        Write-Host "[mcp   ] 已在运行" -ForegroundColor Yellow
    } elseif (Test-PortOpen $McpPort) {
        Write-PortTakenWarning $McpPort 'mcp  ' 'MCP 服务'
        Write-Host "         换端口重来：.\start.ps1 -McpPort 8101" -ForegroundColor DarkGray
    } else {
        # ⚠ `--factory` 不能省：create_sse_app 是"造一个 ASGI app"的**函数**，
        #   不写的话 uvicorn 会把函数对象本身当 app，报 "ASGI callable" 相关错误。
        #
        # ⚠ 刻意起成**独立进程**，而不是像早期那样 mount 进主应用的 /mcp：
        #   主应用的 timing_middleware 会把 SSE 响应再包一层，结果是同一个请求
        #   发了两次响应 → 客户端拿到 502，而且服务进程随后会**消失**
        #   （日志里只剩一句 Unexpected ASGI message，极难往 SSE 上联想）。
        Start-Process -FilePath $VenvPython `
            -ArgumentList '-m','uvicorn','fashionops.mcp.server:create_sse_app','--factory','--host','127.0.0.1','--port',$McpPort `
            -WorkingDirectory $RepoRoot -WindowStyle Hidden `
            -RedirectStandardOutput $McpLog -RedirectStandardError $McpErr | Out-Null

        if (Wait-ForPort $McpPort 30 'mcp') {
            Write-Host "[mcp   ] 已启动 -> http://127.0.0.1:$McpPort/mcp/sse  (日志: $McpLog)" -ForegroundColor Green
            Write-Host '         外部 Agent 连这里用平台工具；写操作（如 mail.send）经审批闸口，不会被执行' -ForegroundColor DarkGray
        }
    }
}

# ---------------------------------------------------------------------------
# 5. DeerFlow sidecar（可选编排后端）
# ---------------------------------------------------------------------------
# **默认不起**，两个理由：
#   1) 它有自己的 venv、config.yaml、.env，不是每台机器都装了 ——
#      默认起会让"一条命令启动"在没装它的机器上失败，或者刷一堆红字吓到人；
#   2) 平台默认 orchestrator_backend=legacy，这时候 DeerFlow 起了也**没人调它**。
#
# 所以只有下面两种情况才起：显式 -WithDeerFlow，或配置里已经是 deerflow。
$wantDeerFlow = $WithDeerFlow -or ((Get-OrchestratorBackend) -eq 'deerflow')

if (-not $wantDeerFlow) {
    Write-Head '5/6 DeerFlow：未启用（要起加 -WithDeerFlow）'
    Write-Host '         DeerFlow 是可选编排后端：默认用平台自研的 todo 编排，不依赖它' -ForegroundColor DarkGray
} elseif (-not $DeerFlowRoot) {
    Write-Head '5/6 DeerFlow：未找到'
    Write-Host '[flow  ] 没找到 DeerFlow 目录，跳过。它不在时用 -DeerFlowRoot 指定位置' -ForegroundColor Yellow
} else {
    Write-Head "5/6 DeerFlow sidecar (端口 $DeerFlowPort)"

    if (Test-PortServedBy $DeerFlowPort 'app.gateway.app') {
        Write-Host '[flow  ] 已在运行' -ForegroundColor Yellow
    } elseif (Test-PortOpen $DeerFlowPort) {
        Write-PortTakenWarning $DeerFlowPort 'flow ' 'DeerFlow'
        Write-Host '         先停掉它，或改 DeerFlow 端口（run_gateway.cmd 里的 --port 要与本脚本一致）' -ForegroundColor DarkGray
    } else {
        # 复用 DeerFlow 自己的 run_gateway.cmd，**不在这里重写启动命令**。
        # 那个脚本里有两个硬踩出来的坑，抄一份过来迟早会漏改一处：
        #   1) deerflow/.env 必须在 Python 启动前灌进进程环境
        #      （internal_auth 在模块导入时读 token，读不到就随机生成一个，
        #        表现是 /api/threads 一律 403，而日志里看不出鉴权失败）；
        #   2) 代理要带，但必须排除回环地址（否则连平台 MCP 会被回 502）。
        $gatewayCmd = Join-Path $DeerFlowRoot 'run_gateway.cmd'
        $deerFlowVenv = Join-Path $DeerFlowRoot 'backend\.venv\Scripts\python.exe'

        if (-not (Test-Path $gatewayCmd)) {
            Write-Host "[flow  ] 找不到 $gatewayCmd，跳过" -ForegroundColor Yellow
        } elseif (-not (Test-Path $deerFlowVenv)) {
            Write-Host '[flow  ] DeerFlow 依赖没装（backend\.venv 不存在），跳过' -ForegroundColor Yellow
            Write-Host '         要装：cd <deerflow>\backend; uv sync' -ForegroundColor DarkGray
        } else {
            Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$gatewayCmd`"" `
                -WorkingDirectory $DeerFlowRoot -WindowStyle Hidden `
                -RedirectStandardOutput $DeerFlowLog -RedirectStandardError $DeerFlowErr | Out-Null

            if (Wait-ForPort $DeerFlowPort 45 'flow') {
                Write-Host "[flow  ] 已启动 -> http://127.0.0.1:$DeerFlowPort  (日志: $DeerFlowLog)" -ForegroundColor Green
                Write-Host '         它反过来经 :8100/mcp/sse 调平台工具' -ForegroundColor DarkGray
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 6. 前端
# ---------------------------------------------------------------------------
if ($NoFrontend) {
    Write-Head '6/6 前端：已跳过（-NoFrontend）'
} else {
    Write-Head "6/6 前端 (端口 $FrontendPort)"

    if (Test-PortServedBy $FrontendPort 'vite') {
        Write-Host '[front ] 已在运行' -ForegroundColor Yellow
    } elseif (Test-PortOpen $FrontendPort) {
        Write-PortTakenWarning $FrontendPort 'front' '前端'
        Write-Host "         换端口重来：.\start.ps1 -FrontendPort 3001" -ForegroundColor DarkGray
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
# ⚠ 后端没起来时**不要**打印它的地址：那会让人去访问一个其实是
#   别的程序的端口，然后对着 404 查半天（本机 :8000 就是这个情况）。
if ($backendUs) {
    Write-Host "  后端接口   : http://127.0.0.1:$BackendPort/docs" -ForegroundColor White
    Write-Host "  健康检查   : http://127.0.0.1:$BackendPort/health" -ForegroundColor White
} else {
    Write-Host "  后端接口   : **未启动**（端口 $BackendPort 被别的程序占着 —— 见上面 3/6 的告警）" -ForegroundColor Red
}
if (-not $NoMcp) {
    Write-Host "  MCP 服务   : http://127.0.0.1:$McpPort/mcp/sse   （外部 Agent 连这里用平台工具）" -ForegroundColor White
}
if ($wantDeerFlow) {
    Write-Host "  DeerFlow   : http://127.0.0.1:$DeerFlowPort               （可选编排后端）" -ForegroundColor White
}
Write-Host ''
Write-Host '  演示账号   : demo / FashionOps@2026（admin）' -ForegroundColor White
Write-Host ''
Write-Host "  停止       : cd `"$PSScriptRoot`"; .\stop.ps1" -ForegroundColor DarkGray
Write-Host "  看数据层   : .\services.ps1 status" -ForegroundColor DarkGray
if (-not $wantDeerFlow) {
    Write-Host '  起 DeerFlow: .\start.ps1 -WithDeerFlow' -ForegroundColor DarkGray
}
Write-Host ''
Write-Host '  ⚠ 前端只能用 localhost（IPv6 ::1），127.0.0.1 连不上' -ForegroundColor DarkGray
