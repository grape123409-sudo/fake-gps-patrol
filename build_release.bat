@echo off
setlocal enabledelayedexpansion

REM ==========================================================
REM build_release.bat
REM One-click build: produces a folder you can hand to someone else
REM who has NOT installed Python or anything else - they just
REM double-click FakeGpsPatrol.exe inside the output folder.
REM
REM Two stages:
REM   1. Freeze pymobiledevice3 itself into its own pymobiledevice3.exe
REM      (only needed the first time, or after upgrading pymobiledevice3;
REM       set SKIP_PMD3=1 to skip this stage on later rebuilds)
REM   2. Freeze the main app, bundling adb/, theme.qss, and the
REM      pymobiledevice3.exe from step 1 alongside it
REM
REM Requirements on THIS build machine (not on the end user's machine):
REM   pip install -r requirements.txt pyinstaller pymobiledevice3
REM
REM This window stays open (press any key) whether the build
REM succeeds or fails, so you can always read what happened.
REM ==========================================================

cd /d "%~dp0"
set DIST=dist\FakeGpsPatrol

echo ===== Environment check =====
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] "python" was not found on PATH.
    echo         This script calls the "python" on your system PATH.
    echo         Open a plain Command Prompt and run "python --version" first
    echo         to confirm it works there, or run this script from the same
    echo         environment / virtualenv where "python main.py" already works.
    goto fail
)
echo [OK] python found:
where python
echo.

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller not found. Run: pip install pyinstaller
    goto fail
)
echo [OK] PyInstaller is installed
echo.

if not exist main.py (
    echo [ERROR] main.py not found in this folder.
    echo         Put build_release.bat next to main.py / gps_core.py and re-run.
    echo         Current folder: %CD%
    goto fail
)

if "%SKIP_PMD3%"=="1" goto build_app

echo.
echo ===== [1/2] Building pymobiledevice3.exe (first run: ~5-15 min, please wait) =====
echo.

python -m pip show pymobiledevice3 >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pymobiledevice3 package not found. Run: pip install pymobiledevice3
    goto fail
)

for /f "delims=" %%i in ('python -c "import pymobiledevice3, os; print(os.path.join(os.path.dirname(pymobiledevice3.__file__), '__main__.py'))"') do set PMD3_MAIN=%%i

if "%PMD3_MAIN%"=="" (
    echo [ERROR] Could not locate pymobiledevice3's entry point file. Package may be broken.
    goto fail
)

rmdir /s /q build_pmd3 2>nul
rmdir /s /q dist_pmd3 2>nul
del pymobiledevice3.spec 2>nul

python -m PyInstaller --noconfirm --clean --name pymobiledevice3 --onedir ^
  --distpath dist_pmd3 --workpath build_pmd3 --contents-directory . ^
  --collect-all pymobiledevice3 ^
  --collect-all typer ^
  --collect-all pytun_pmd3 ^
  --copy-metadata readchar ^
  --copy-metadata typer ^
  --copy-metadata click ^
  --copy-metadata rich ^
  --copy-metadata inquirer3 ^
  "%PMD3_MAIN%"

if errorlevel 1 (
    echo.
    echo [ERROR] pymobiledevice3.exe build failed - scroll up for the PyInstaller error.
    goto fail
)

:build_app
echo.
echo ===== [2/2] Building the main app =====
echo.

rmdir /s /q build 2>nul
rmdir /s /q "%DIST%" 2>nul
del FakeGpsPatrol.spec 2>nul

python -m PyInstaller --noconfirm --clean --name FakeGpsPatrol --onedir --windowed ^
  --contents-directory . ^
  --add-data "theme.qss;." ^
  --add-data "adb;adb" ^
  main.py

if errorlevel 1 (
    echo.
    echo [ERROR] Main app build failed - scroll up for the PyInstaller error.
    goto fail
)

echo.
echo ===== Merging pymobiledevice3.exe into the release folder =====
echo.
if not exist dist_pmd3\pymobiledevice3 goto merge_check_existing
xcopy /e /i /y /q dist_pmd3\pymobiledevice3 "%DIST%\pymobiledevice3" >nul
echo [OK] merged
goto merge_done

:merge_check_existing
if not exist "%DIST%\pymobiledevice3" goto merge_missing
echo [OK] already present (SKIP_PMD3 was used, reusing the previous build)
goto merge_done

:merge_missing
echo [WARNING] dist_pmd3\pymobiledevice3 not found - iOS features will NOT
echo           work on other people's machines that don't have Python.
echo           (Running locally on this machine will still fall back to
echo            the system-installed python + pymobiledevice3.)

:merge_done

echo.
echo ===== Done =====
echo Release folder: %CD%\%DIST%
echo Copy the WHOLE %DIST% folder (not just the exe) to give to someone else.
echo They just double-click FakeGpsPatrol.exe inside it.
echo.
pause
endlocal
exit /b 0

:fail
echo.
echo Build aborted.
echo.
pause
endlocal
exit /b 1
