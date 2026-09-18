@echo off
rem 带本地中继环境变量启动 ZCode 桌面版（先手动退出已运行的 ZCode）
rem 中继地址和二维码网页地址都重定向到本机，云端完全不参与
rem 可用环境变量覆盖:
rem   HIJACK_PORT  中继端口（默认 8899，需与 relay.mjs 一致）
rem   ZCODE_EXE    ZCode.exe 完整路径（默认自动探测常见安装位置）

setlocal
if "%HIJACK_PORT%"=="" set HIJACK_PORT=8899

set "ZCODE_EXE=%ZCODE_EXE%"
if "%ZCODE_EXE%"=="" (
  if exist "%LOCALAPPDATA%\Programs\ZCode\ZCode.exe" set "ZCODE_EXE=%LOCALAPPDATA%\Programs\ZCode\ZCode.exe"
)
if "%ZCODE_EXE%"=="" (
  if exist "%ProgramFiles%\ZCode\ZCode.exe" set "ZCODE_EXE=%ProgramFiles%\ZCode\ZCode.exe"
)
if "%ZCODE_EXE%"=="" (
  echo [!] 未找到 ZCode.exe，请设置 ZCODE_EXE 环境变量后重试
  exit /b 1
)

set ZCODE_WEB_REMOTE_CONTROL_RELAY_WS_URL=ws://127.0.0.1:%HIJACK_PORT%/ws
set ZCODE_WEB_REMOTE_CONTROL_URL=http://127.0.0.1:%HIJACK_PORT%/controller
start "" "%ZCODE_EXE%"
echo ZCode 已用本地中继环境变量启动（127.0.0.1:%HIJACK_PORT%）
endlocal
