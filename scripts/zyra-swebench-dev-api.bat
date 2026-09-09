@echo off
rem ZYRA dev API 启动（SWE-bench 模式）：加载 deepseek 凭证 + benchmark docker 绑定
cd /d C:\Users\Ylon\Desktop\挑战杯2026\zyra

rem 从 .env.deepseek.local 加载凭证到环境（不打印值）
for /f "usebackq tokens=1,* delims==" %%a in (".env.deepseek.local") do (
  set "%%a=%%b"
)

set ZYRA_DEEPSEEK_ENABLED=true
set ZYRA_MODEL_PROVIDER=deepseek
set ZYRA_MODEL=deepseek-v4-flash
set ZYRA_API_HOST=127.0.0.1
set ZYRA_API_PORT=8000

rem benchmark docker 绑定（flask 容器）
set ZYRA_BENCHMARK_DOCKER_CONTAINER=zyra-flask-5014-bench
set ZYRA_BENCHMARK_DOCKER_WORKDIR=/testbed
set ZYRA_BENCHMARK_HOST_WORKSPACE=C:\Users\Ylon\Desktop\挑战杯2026\zyra\.tmp\zyra-flask-5014-host-workspace

python scripts\dev_api.py
