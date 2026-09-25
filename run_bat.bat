@echo off
if not exist "logs" mkdir "logs" 2>nul
start pythonw -u run_temp.py > logs\pythonw.log 2>&1