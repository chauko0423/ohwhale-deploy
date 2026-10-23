@echo off
setlocal
rem Only this installer process uses the Tsinghua PyPI mirror.
rem The CUDA wheel command still specifies the official PyTorch index.
set "PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
set "PIP_EXTRA_INDEX_URL="
call "%~dp0Run-device-test.cmd"
endlocal
