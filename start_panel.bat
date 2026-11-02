@echo off
rem ============================================================
rem  bilibot һ�������������������
rem  ע�⣺���ļ��� GBK(cp936) ���뱣�棬�ü��±�ֱ�Ӹļ��ɣ�
rem        ��Ҫ����Ϊ UTF-8������������ʾ�������롣
rem ============================================================
setlocal EnableExtensions
title bilibot - �������
cd /d "%~dp0"

echo ============================================================
echo   bilibot  -  �������
echo   Ŀ¼: %CD%
echo ============================================================
echo.

rem ---------- �� Python ----------
set "PY="
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if not defined PY (
    where /q py
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where /q python
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto :no_python

%PY% -c "import aiohttp, yaml" >nul 2>nul
if errorlevel 1 goto :no_deps

echo ��������Զ��� http://127.0.0.1:8765/ ���˿ڱ�ռ��ʱ���Զ���һ����
echo �ر���壺�ڱ����ڰ� Ctrl+C����ֱ�ӹص������ڡ�
echo �����ĵ����ñ���� BOT ���Զ��������أ�Լ���룩����ʾ�ʣ����裩�����������Ч��
echo.

%PY% -m src.app panel
set "RC=%ERRORLEVEL%"

echo.
echo ���������ֹͣ���˳��� %RC%����
echo ��������رմ��ڡ���
pause >nul
exit /b %RC%

:no_python
echo [����] û���ҵ� Python���ѳ��� py -3 �� python��
echo        �밲װ Python 3.10+����װʱ��ѡ "Add python.exe to PATH"��
goto :failed

:no_deps
echo [����] Python ������������aiohttp / pyyaml��
echo        ���ڱ�����ִ�У�
echo        %PY% -m pip install -r requirements.txt
goto :failed

:failed
echo.
echo ��������رմ��ڡ���
pause >nul
exit /b 1
