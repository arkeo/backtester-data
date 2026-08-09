@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Publish market history
color 0B

echo.
echo   ================================================================
echo     PUBLISH MARKET HISTORY TO GITHUB
echo   ================================================================
echo.
echo   This does everything: signs you in, creates the repository,
echo   uploads it, makes your secret key, and starts the first update.
echo.
echo   You only need to do one thing by hand: sign in to GitHub in the
echo   browser window that opens.
echo.
pause

where gh >nul 2>&1
if errorlevel 1 (
  echo.
  echo   The GitHub tool is not on this computer.
  echo   Run this line, then start me again:
  echo.
  echo       winget install GitHub.cli
  echo.
  pause
  exit /b 1
)

where git >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Git is not installed. Get it from https://git-scm.com
  echo.
  pause
  exit /b 1
)

echo.
echo   [1/7] Checking your GitHub sign-in...
gh auth status >nul 2>&1
if errorlevel 1 (
  echo.
  echo   ----------------------------------------------------------------
  echo    READ THIS BEFORE PRESSING A KEY
  echo.
  echo    A short code is about to appear HERE, like   A1B2-C3D4
  echo.
  echo      1. Write it down or copy it
  echo      2. Press Enter - the browser opens
  echo      3. Type that code into the browser page
  echo.
  echo    The code is always in THIS black window, never in the browser.
  echo   ----------------------------------------------------------------
  echo.
  echo    If it asks, choose:  GitHub.com  /  HTTPS  /  Yes
  echo.
  pause
  echo.
  gh auth login --hostname github.com --git-protocol https --web
  if errorlevel 1 (
    echo.
    echo   Sign-in did not finish. Run me again when you are ready.
    pause
    exit /b 1
  )
) else (
  echo         Already signed in.
)

for /f "delims=" %%i in ('gh api user --jq .login 2^>nul') do set "GHUSER=%%i"
for /f "delims=" %%i in ('gh api user --jq .id 2^>nul') do set "GHID=%%i"
if "%GHUSER%"=="" (
  echo.
  echo   Could not read your GitHub account. Try running me again.
  pause
  exit /b 1
)
echo         Signed in as %GHUSER%

echo.
echo   [2/7] Setting your name on commits...
git config --global user.name "%GHUSER%"
git config --global user.email "%GHID%+%GHUSER%@users.noreply.github.com"
git config --global credential.helper manager
echo         Done.

echo.
echo   [3/7] Preparing the files...
rem Start the history clean, so every commit carries the right name. Nothing
rem is lost: this folder is generated and has never been pushed anywhere.
if exist ".git" rmdir /s /q ".git"
git init -b main >nul 2>&1
git add -A >nul 2>&1
git commit -q -m "Market history mirror" >nul 2>&1
if errorlevel 1 (
  echo   Could not prepare the files.
  pause
  exit /b 1
)
echo         Done.

echo.
echo   [4/7] Making your secret key...
if exist "YOUR-SECRET-KEY.txt" (
  set /p NEWKEY=<"YOUR-SECRET-KEY.txt"
  echo         Reusing the key already saved here.
) else (
  for /f "delims=" %%i in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString(\"N\") + [guid]::NewGuid().ToString(\"N\")"') do set "NEWKEY=%%i"
  > "YOUR-SECRET-KEY.txt" echo !NEWKEY!
  echo         Made a new one and saved it to YOUR-SECRET-KEY.txt
)
if "!NEWKEY!"=="" (
  echo   Could not make a key.
  pause
  exit /b 1
)

echo.
echo   [5/7] Creating the repository and uploading...
gh repo view "%GHUSER%/backtester-data" >nul 2>&1
if errorlevel 1 (
  gh repo create backtester-data --public --source=. --remote=origin --push
  if errorlevel 1 (
    echo.
    echo   Could not create it. If the name is taken, delete the old one at
    echo   https://github.com/%GHUSER%/backtester-data/settings and run me again.
    pause
    exit /b 1
  )
) else (
  echo         It already exists - updating it.
  git remote remove origin >nul 2>&1
  git remote add origin "https://github.com/%GHUSER%/backtester-data.git"
  git push -u --force origin main
  if errorlevel 1 (
    echo   Upload failed.
    pause
    exit /b 1
  )
)
echo         Uploaded.

echo.
echo   [6/7] Storing your settings on GitHub...
gh secret set BACKTESTER_KEY --repo "%GHUSER%/backtester-data" --body "!NEWKEY!" >nul 2>&1
if errorlevel 1 (
  echo   Could not store the key. Do it by hand at
  echo   https://github.com/%GHUSER%/backtester-data/settings/secrets/actions
) else (
  echo         Key stored.
)
gh variable set SYMBOLS --repo "%GHUSER%/backtester-data" --body "all" >nul 2>&1
gh variable set MINUTES --repo "%GHUSER%/backtester-data" --body "150" >nul 2>&1
rem No YEARS setting any more: every instrument is fetched back as far as its
rem source goes, which for the currency pairs is the year 2000.
gh variable delete YEARS --repo "%GHUSER%/backtester-data" >nul 2>&1
echo         All 71 instruments, full history.

echo.
echo   [7/7] Starting the first update...
rem GitHub needs a moment to notice a workflow that was only just pushed,
rem so the first attempt normally fails. Without retrying, the very first
rem run never starts and nothing is ever published.
set "STARTED="
for /l %%n in (1,1,10) do (
  if not defined STARTED (
    gh workflow run publish-history.yml --repo "%GHUSER%/backtester-data" >nul 2>&1
    if not errorlevel 1 set "STARTED=yes"
    if not defined STARTED (
      echo         waiting for GitHub to register the job...
      timeout /t 10 /nobreak >nul
    )
  )
)
if not defined STARTED (
  echo         Could not start it automatically. Click the green
  echo         "Run workflow" button here:
  echo         https://github.com/%GHUSER%/backtester-data/actions
) else (
  echo         Started. It runs on GitHub's computers, not yours.
echo.
echo         The first run will not finish everything - it fetches each
echo         market back to the year its data starts, and the Dow comes
echo         one day at a time. Every run publishes what it managed and
echo         the next one carries on. It is complete within a day or so.
)

set "MIRROR=https://github.com/%GHUSER%/backtester-data/releases/download/history"
> "MIRROR-ADDRESS.txt" echo !MIRROR!

echo.
echo   ================================================================
echo     FINISHED
echo   ================================================================
echo.
echo   Watch it work here:
echo     https://github.com/%GHUSER%/backtester-data/actions
echo.
echo   Nothing to paste anywhere. This address is saved for you in
echo   MIRROR-ADDRESS.txt, and BUILD-INSTALLER.bat writes it straight
echo   into the application your customers install:
echo.
echo     !MIRROR!
echo.
echo   ----------------------------------------------------------------
echo   KEEP THIS FILE:  YOUR-SECRET-KEY.txt
echo.
echo   That key seals the files so only your application can open them.
echo   Build the installer with BUILD-INSTALLER.bat, which reads this
echo   same key - otherwise your customers cannot open anything.
echo   ----------------------------------------------------------------
echo.
pause
