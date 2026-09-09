@echo off
rem Preserve arbitrary Docker arguments, including the gateway's quoted shell
rem wrapper, while forwarding output through the Ubuntu Docker CLI.
D:\Anaconda\python.exe "%~dp0docker_wsl_bridge.py" %*
