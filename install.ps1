# companion-kit 一行安装（Windows / PowerShell）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.ps1 | iex"
#
# 全自动，不需要你手动做任何一步：
#   1. 找 Python 3.10+；找不到就下一份 uv（单文件、免管理员），用 uv 拉 3.12
#   2. 建独立 venv（%LOCALAPPDATA%\companion\venv，不碰系统 Python）
#   3. 装 companion-kit（Release 的 whl，不需要 git；失败回退 git+）
#   4. 初始化 store（%USERPROFILE%\.companion）
#   5. 写用户 PATH 与 COMPANION_HOME（不用管理员，新终端生效）
#   6. 装了 dsh 就顺手注册插件
#
# 可选（不传就用默认；也认同名环境变量）：
#   -Store <dir>     对方 store 目录        COMPANION_HOME
#   -Python <path>   指定解释器             COMPANION_PYTHON
#   -NoDsh           不自动注册 DSH         COMPANION_NO_DSH=1
#   -NoEnv           不改 PATH/环境变量      COMPANION_NO_ENV=1
#   -DryRun          只打印，不改任何东西
#
# 说明：这里刻意不用 param() —— `irm ... | iex` 下 param 不是合法语句。
# 参数从 $args 手工解析，因此 -File 与 iex 两种跑法都兼容。

$ErrorActionPreference = "Stop"

# ── 参数解析（-File 会给 $args；iex 时为空）─────────────────
$Opt = @{}
for ($i = 0; $i -lt $args.Count; $i++) {
  $a = [string]$args[$i]
  if ($a -like "-*") {
    $key = $a.TrimStart("-")
    $next = if ($i + 1 -lt $args.Count) { [string]$args[$i + 1] } else { "" }
    if ($next -and -not $next.StartsWith("-")) { $Opt[$key] = $next; $i++ }
    else { $Opt[$key] = $true }
  }
}

$Store  = if ($Opt["Store"])  { $Opt["Store"] }  elseif ($env:COMPANION_HOME) { $env:COMPANION_HOME } else { "$env:USERPROFILE\.companion" }
$Python = if ($Opt["Python"]) { $Opt["Python"] } elseif ($env:COMPANION_PYTHON) { $env:COMPANION_PYTHON } else { "" }
$NoDsh  = [bool]($Opt["NoDsh"] -or ($env:COMPANION_NO_DSH -eq "1"))
$NoEnv  = [bool]($Opt["NoEnv"] -or ($env:COMPANION_NO_ENV -eq "1"))
$DryRun = [bool]$Opt["DryRun"]

$Repo   = "https://github.com/Yullieuli/companion-kit"
$Ver    = "v0.2.2"
$Wheel  = "$Repo/releases/download/$Ver/companion_kit-0.2.2-py3-none-any.whl"
$Root   = Join-Path $env:LOCALAPPDATA "companion"
$Bin    = Join-Path $Root "bin"
$Venv   = Join-Path $Store "venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

function Say($m) { Write-Host "  $m" }

Write-Host "== companion-kit ==" -ForegroundColor Cyan
Say "store : $Store"
Say "venv  : $Venv"

# ── 1. Python 3.10+ ────────────────────────────────────────
function Test-Py($exe) {
  if (-not $exe) { return $false }
  try {
    $v = & $exe -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
    return ($LASTEXITCODE -eq 0 -and [int]$v -ge 310)
  } catch { return $false }
}

$py = $Python
if (-not (Test-Py $py)) { $py = "" }
if (-not $py) {
  foreach ($c in @("python", "python3", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Py $cmd.Source)) { $py = $cmd.Source; break }
  }
}

if (-not $py) {
  Say "没找到 Python 3.10+，用 uv 装一个（单文件，不用管理员）"
  if ($DryRun) {
    Say "[dry] 下载 uv → $Bin，然后 uv python install 3.12"
    $py = ""
  } else {
    New-Item -ItemType Directory -Force -Path $Bin | Out-Null
    $uvExe = Join-Path $Bin "uv.exe"
    if (-not (Test-Path $uvExe)) {
      $sysUv = Get-Command uv -ErrorAction SilentlyContinue
      if ($sysUv) {
        Say "用已有的 uv：$($sysUv.Source)"
        Copy-Item $sysUv.Source $uvExe -Force
      } else {
        $zip = Join-Path $env:TEMP "uv-win.zip"
        $url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
        Say "下载 uv（约 15MB，最多等 120 秒）…"
        try {
          Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 120
        } catch {
          throw ("下载 uv 失败（网络问题）。两个办法：`n" +
                 "  1) 先装 Python 3.10+（python.org 或 Microsoft Store）再重跑这条命令；`n" +
                 "  2) 挂代理后重跑。原始错误：$($_.Exception.Message)")
        }
        Expand-Archive -Path $zip -DestinationPath $Bin -Force
        Remove-Item $zip -Force
      }
      $found = Get-ChildItem -Path $Bin -Filter uv.exe -Recurse | Select-Object -First 1
      if (-not $found) { throw "uv.exe 解压后没找到" }
      if ($found.FullName -ne $uvExe) { Move-Item $found.FullName $uvExe -Force }
    }
    Say "用 uv 拉 Python 3.12（约 30MB）…"
    & $uvExe python install 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv 装 Python 失败" }
    $py = (& $uvExe python find 3.12 2>$null | Select-Object -First 1)
    if (-not $py) { throw "uv 装完但找不到解释器" }
  }
}
if ($py) { Say "python: $py" }

# ── 2. venv ────────────────────────────────────────────────
if ($DryRun) { Say "[dry] 建 venv：$Venv" }
else {
  if (-not (Test-Path $VenvPy)) {
    & $py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "建 venv 失败（$py）" }
  }
}

# ── 3. 装包 ────────────────────────────────────────────────
# pip 会读 HTTP(S)_PROXY；代理挂了就报 502，看起来像"包坏了"。
# 所以：先按环境走，失败就去掉代理重试，再失败才回退 git。
function Pip-Install([string[]]$targets) {
  & $VenvPy -m pip install -q --no-compile @targets
  if ($LASTEXITCODE -eq 0) { return $true }
  $keys = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
  $saved = @{}
  foreach ($k in $keys) {
    $v = [Environment]::GetEnvironmentVariable($k)
    if ($v) { $saved[$k] = $v; [Environment]::SetEnvironmentVariable($k, $null) }
  }
  Say "pip 直连失败（可能代理不通），去掉代理重试…"
  & $VenvPy -m pip install -q --no-compile @targets
  $ok = ($LASTEXITCODE -eq 0)
  foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
  return $ok
}

if ($DryRun) {
  Say "[dry] pip install $Wheel"
} else {
  if (-not (Pip-Install @($Wheel))) {
    Say "whl 装失败，回退 git（需要 git）"
    if (-not (Pip-Install @("git+$Repo"))) {
      throw ("安装失败：whl 与 git 都不通。`n" +
             "  若是代理问题：先关掉系统代理/环境变量里的 HTTP(S)_PROXY 再重跑；`n" +
             "  若是网络问题：挂代理后重跑，或先装 Python 3.10+ 再重试。")
    }
  }
}

# ── 4. 初始化 store ────────────────────────────────────────
$env:COMPANION_HOME = $Store
if (-not $DryRun) {
  & (Join-Path $Venv "Scripts\companion.exe") init | Out-Null
  Say "store 已初始化：$Store"
}

# ── 5. PATH 与环境变量 ─────────────────────────────────────
$scripts = Join-Path $Venv "Scripts"
if ($NoEnv) {
  Say "跳过 PATH/环境变量（-NoEnv）"
} elseif ($DryRun) {
  Say "[dry] 把 $scripts 加进用户 PATH，并设置 COMPANION_HOME"
} else {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not $userPath) { $userPath = "" }
  if ($userPath -notlike "*$scripts*") {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $scripts), "User")
    Say "已写入用户 PATH（新开终端生效）"
  } else {
    Say "PATH 里已有"
  }
  [Environment]::SetEnvironmentVariable("COMPANION_HOME", $Store, "User")
  $env:Path = "$scripts;$env:Path"
}

# ── 6. 顺手注册 DSH ────────────────────────────────────────
if (-not $NoDsh) {
  $dsh = Get-Command dsh -ErrorAction SilentlyContinue
  if ($dsh) {
    if ($DryRun) {
      Say "[dry] dsh plugin --profile desktop add $Repo"
    } else {
      Say "注册到 DSH …"
      & $dsh.Source plugin --profile desktop add $Repo
      if ($LASTEXITCODE -ne 0) { Say "DSH 注册没成功，可稍后在「添加插件」里填 $Repo" }
    }
  } else {
    Say "没检测到 dsh，跳过（桌面端点「添加插件」填 $Repo 即可）"
  }
}

Write-Host ""
if ($DryRun) {
  Write-Host "dry run 结束，没有改任何东西。" -ForegroundColor Yellow
  exit 0
}

Write-Host "装好了。新开一个终端，然后试：" -ForegroundColor Green
Say "companion context --budget 1200"
Say "companion tick"
Say "companion cost"
Write-Host ""
Write-Host "先改人格：$Store\SOUL.md" -ForegroundColor DarkGray
