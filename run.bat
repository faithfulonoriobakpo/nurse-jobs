@echo off
rem Daily entry point for Task Scheduler. Optional API keys go in keys.bat (not required).
cd /d "%~dp0"
if exist keys.bat call keys.bat
python find_jobs.py --open-if-new >> output\run.log 2>&1
