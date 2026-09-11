<#
.SYNOPSIS
    初始化 MySQL：建库 + 建应用专用账号 + 自动写入 .env

.DESCRIPTION
    做四件事：
      1. 交互式询问 MySQL root 密码（只在本机终端内使用）
      2. 建库 fashionops（utf8mb4）
      3. 生成随机强密码，创建应用账号 fashionops@localhost（只授权这一个库）
      4. 把连接信息写进 platform\.env

    密码处理方式：
      - root 密码通过临时 option file 传给 mysql.exe，不出现在命令行参数里
        （否则同机器上任何进程都能从进程列表里读走）
      - 应用密码用加密随机数生成，只写进 .env（.env 已被 .gitignore 排除）
      - 脚本全程不打印任何密码
      - 临时文件在 finally 里删除

.EXAMPLE
    cd platform\deploy\scripts
    .\init-mysql.ps1

.EXAMPLE
    # 换个实例 / 端口
    .\init-mysql.ps1 -DbHost 127.0.0.1 -DbPort 3307
#>
[CmdletBinding()]
param(
    [string]$MysqlExe = 'D:\develop\mysql\mysql_8\bin\mysql.exe',
    [string]$RootUser = 'root',
    [string]$AppUser  = 'fashionops',
    [string]$Database = 'fashionops',
    [string]$DbHost   = '127.0.0.1',
    [int]   $DbPort   = 3306,
    [string]$EnvFile  = ''
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
$PlatformRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not $EnvFile) { $EnvFile = Join-Path $PlatformRoot '.env' }
$EnvExample = Join-Path $PlatformRoot '.env.example'

function Write-Step($text) {
    Write-Host ''
    Write-Host ">>> $text" -ForegroundColor Cyan
}
function Write-Ok($text)   { Write-Host "    [OK]   $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    [WARN] $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "    [FAIL] $text" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
function New-OptionFile {
    # MySQL option file 里的值需要处理引号和反斜杠
    param([string]$Path, [string]$User, [string]$Password, [string]$HostName, [int]$Port)
    $escaped = $Password -replace '\\', '\\\\' -replace '"', '\"'
    $lines = @(
        '[client]'
        "user=$User"
        "password=`"$escaped`""
        "host=$HostName"
        "port=$Port"
        'default-character-set=utf8mb4'
    )
    # 不带 BOM，MySQL 的 option file 解析器不认 BOM
    [System.IO.File]::WriteAllText($Path, ($lines -join "`r`n"), (New-Object System.Text.UTF8Encoding $false))
}

function New-RandomPassword {
    # 24 位纯字母数字：不含引号/反斜杠，避免 option file 和 SQL 里的转义问题
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $raw = [Convert]::ToBase64String($bytes)
    return ($raw -replace '[^0-9A-Za-z]', '').Substring(0, 24)
}

function Set-EnvLines {
    # 把若干 KEY=VALUE 写进 .env 的行数组；已存在的键就地替换，不存在的追加
    param([string[]]$Lines, [hashtable]$Pairs)
    $result = [System.Collections.Generic.List[string]]::new()
    foreach ($l in $Lines) { $result.Add($l) }

    foreach ($key in $Pairs.Keys) {
        $pattern = '^' + [regex]::Escape($key) + '='
        $hit = $false
        for ($i = 0; $i -lt $result.Count; $i++) {
            if ($result[$i] -match $pattern) {
                $result[$i] = "$key=$($Pairs[$key])"
                $hit = $true
            }
        }
        if (-not $hit) { $result.Add("$key=$($Pairs[$key])") }
    }
    return $result.ToArray()
}

# ---------------------------------------------------------------------------
# 0. 前置检查
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '========================================' -ForegroundColor Cyan
Write-Host '  MySQL 初始化 —— 服装电商多 Agent 中台' -ForegroundColor Cyan
Write-Host '========================================' -ForegroundColor Cyan

Write-Step '检查 mysql 客户端'
if (-not (Test-Path $MysqlExe)) {
    Write-Bad "找不到 $MysqlExe"
    Write-Host '    请用 -MysqlExe 参数指定 mysql.exe 的实际路径'
    exit 1
}
$ver = (& $MysqlExe --version) 2>&1
Write-Ok $ver

Write-Step "检查端口 $DbHost`:$DbPort 是否可连"
$client = New-Object System.Net.Sockets.TcpClient
try {
    $client.Connect($DbHost, $DbPort)
    Write-Ok '端口已开放'
} catch {
    Write-Bad 'MySQL 端口连不上，先启动 MySQL80 服务：net start MySQL80'
    exit 1
} finally {
    $client.Close()
}

# ---------------------------------------------------------------------------
# 1. 取 root 密码
# ---------------------------------------------------------------------------
Write-Step '输入 MySQL root 密码'
Write-Host '    （只在本机终端内使用，不会出现在命令行里，也不会被打印）' -ForegroundColor DarkGray
$secure = Read-Host -Prompt '    root 密码' -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $rootPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if ([string]::IsNullOrEmpty($rootPassword)) {
    Write-Bad '密码为空。如果你确实设了空密码，请用 mysql -u root 手动执行 deploy\mysql 下的两个 SQL。'
    exit 1
}

$tmpDir = Join-Path $env:TEMP ('fashionops-mysql-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
$rootCnf = Join-Path $tmpDir 'root.cnf'
$appCnf  = Join-Path $tmpDir 'app.cnf'

try {
    New-OptionFile -Path $rootCnf -User $RootUser -Password $rootPassword -HostName $DbHost -Port $DbPort

    Write-Step '用 root 连接测试'
    $out = (& $MysqlExe "--defaults-extra-file=$rootCnf" -e 'SELECT VERSION();' 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Write-Bad 'root 认证失败'
        $out | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        Write-Host '    提示：确认密码是否正确；若忘记密码需先用 --skip-grant-tables 重置。' -ForegroundColor DarkGray
        exit 1
    }
    Write-Ok ($out -join ' ')

    # ---------------------------------------------------------------
    # 2. 生成应用密码
    # ---------------------------------------------------------------
    Write-Step '生成应用账号密码'
    $appPassword = New-RandomPassword
    Write-Ok '已生成 24 位随机密码（不显示，稍后写入 .env）'

    # ---------------------------------------------------------------
    # 3. 建库 + 建号 + 授权
    # ---------------------------------------------------------------
    Write-Step "建库 $Database / 建号 $AppUser@localhost / 授权"
    # 单引号 here-string：里面的反引号是 MySQL 的标识符引用符，不能被 PowerShell 解释
    $sqlTemplate = @'
CREATE DATABASE IF NOT EXISTS `__DB__`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '__USER__'@'localhost' IDENTIFIED BY '__PW__';
ALTER USER '__USER__'@'localhost' IDENTIFIED BY '__PW__';
GRANT ALL PRIVILEGES ON `__DB__`.* TO '__USER__'@'localhost';
FLUSH PRIVILEGES;
'@
    $sql = $sqlTemplate.Replace('__DB__', $Database).Replace('__USER__', $AppUser).Replace('__PW__', $appPassword)
    # 多行参数传给原生 exe 在 PowerShell 5.1 上不可靠，压成一行；语句间 ; 分隔即可
    $sql = ($sql -replace "\s*\r?\n\s*", ' ').Trim()

    $out = (& $MysqlExe "--defaults-extra-file=$rootCnf" -e $sql 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Write-Bad '执行 DDL 失败'
        $out | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        exit 1
    }
    Write-Ok '库、账号、授权已就绪'

    # ---------------------------------------------------------------
    # 4. 用应用账号自测
    # ---------------------------------------------------------------
    Write-Step "用 $AppUser 账号验证连接"
    New-OptionFile -Path $appCnf -User $AppUser -Password $appPassword -HostName $DbHost -Port $DbPort
    $probe = "SELECT COUNT(*) AS tbl_count FROM information_schema.tables WHERE table_schema='$Database';"
    $out = (& $MysqlExe "--defaults-extra-file=$appCnf" -N -B -e $probe 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Write-Bad '应用账号连接失败'
        $out | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        exit 1
    }
    Write-Ok "连接成功，当前表数量：$(($out -join '').Trim())"

    # ---------------------------------------------------------------
    # 5. 写 .env
    # ---------------------------------------------------------------
    Write-Step "写入 $EnvFile"
    if (-not (Test-Path $EnvFile)) {
        if (Test-Path $EnvExample) {
            Copy-Item $EnvExample $EnvFile
            Write-Ok '已从 .env.example 复制模板'
        } else {
            [System.IO.File]::WriteAllText($EnvFile, '', (New-Object System.Text.UTF8Encoding $false))
        }
    }
    # .env 无 BOM 时 PowerShell 不会自动识别 UTF-8，必须显式指定
    $lines = @(Get-Content -Path $EnvFile -Encoding UTF8)
    $pairs = @{
        'FASHIONOPS_DB_HOST'     = $DbHost
        'FASHIONOPS_DB_PORT'     = $DbPort
        'FASHIONOPS_DB_USER'     = $AppUser
        'FASHIONOPS_DB_PASSWORD' = $appPassword
        'FASHIONOPS_DB_NAME'     = $Database
    }
    $newLines = Set-EnvLines -Lines $lines -Pairs $pairs
    # 不带 BOM：pydantic-settings 的 dotenv 解析器会把 BOM 当成第一个键名的一部分
    [System.IO.File]::WriteAllText($EnvFile, ($newLines -join "`r`n"), (New-Object System.Text.UTF8Encoding $false))
    Write-Ok '数据库配置已写入'

    # ---------------------------------------------------------------
    # 6. 汇总
    # ---------------------------------------------------------------
    Write-Host ''
    Write-Host '========================================' -ForegroundColor Green
    Write-Host '  初始化完成' -ForegroundColor Green
    Write-Host '========================================' -ForegroundColor Green
    Write-Host ''
    Write-Host "  实例      : $DbHost`:$DbPort"
    Write-Host "  数据库    : $Database"
    Write-Host "  应用账号  : $AppUser@localhost"
    Write-Host "  密码存放  : $EnvFile  (FASHIONOPS_DB_PASSWORD)"
    Write-Host ''
    Write-Host '  下一步：' -ForegroundColor Cyan
    Write-Host '    cd ..\..' -ForegroundColor Gray
    Write-Host '    uv run fashionops-db init     # 建 14 张业务表' -ForegroundColor Gray
    Write-Host '    uv run fashionops-seed        # 灌假数据' -ForegroundColor Gray
    Write-Host ''
} finally {
    # 兜底清理：临时 option file 里有明文密码，必须删掉
    if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue }
}
