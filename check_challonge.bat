@echo off
for /f "usebackq tokens=1,2 delims==" %%a in (".env.test") do set %%a=%%b
python check_challonge.py
echo.
echo --- Check finished. Press any key to close. ---
pause >nul
