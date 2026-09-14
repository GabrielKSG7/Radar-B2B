@echo off
setlocal EnableDelayedExpansion

:: ============================================================
:: RADAR B2B - AUTOMATIC GITHUB PUSH
:: ============================================================

title Radar B2B - GitHub Push

:: URL DO REPOSITORIO
set "GITHUB_URL=https://github.com/GabrielKSG7/Radar-B2B.git"

echo.
echo ============================================================
echo                  RADAR B2B - GITHUB
echo ============================================================
echo.

:: ============================================================
:: 1. VERIFICAR GIT
:: ============================================================

echo [1/6] Checking Git...
echo.

git --version >nul 2>&1

if errorlevel 1 (
    echo [ERROR] Git not found on the system.
    echo.
    pause
    exit /b 1
)

echo [OK] Git is installed.
echo.

:: ============================================================
:: 2. VERIFICAR REPOSITORIO
:: ============================================================

echo [2/6] Checking repository...
echo.

git rev-parse --is-inside-work-tree >nul 2>&1

if errorlevel 1 (
    echo [ERROR] This folder is not a Git repository.
    echo.
    echo Please run first:
    echo git init
    echo.
    pause
    exit /b 1
)

echo [OK] Git repository identified.
echo.

:: ============================================================
:: 3. CONFIGURAR REMOTE
:: ============================================================

echo [3/6] Configuring GitHub remote...
echo.

git remote get-url origin >nul 2>&1

if errorlevel 1 (
    echo [INFO] Remote origin not found.
    echo Creating origin...
    echo.

    git remote add origin "%GITHUB_URL%"

    if errorlevel 1 (
        echo [ERROR] Could not create remote origin.
        echo.
        pause
        exit /b 1
    )
) else (
    git remote set-url origin "%GITHUB_URL%"

    if errorlevel 1 (
        echo [ERROR] Could not update remote origin.
        echo.
        pause
        exit /b 1
    )
)

echo [OK] Remote configured:
echo %GITHUB_URL%
echo.

:: ============================================================
:: IDENTIFICAR BRANCH
:: ============================================================

for /f "delims=" %%B in ('git branch --show-current') do set "BRANCH=%%B"

if "!BRANCH!"=="" (
    echo [INFO] Creating main branch...
    echo.

    git checkout -b main

    if errorlevel 1 (
        echo [ERROR] Could not create main branch.
        echo.
        pause
        exit /b 1
    )

    set "BRANCH=main"
)

echo [OK] Current branch: !BRANCH!
echo.

:: ============================================================
:: 4. VERIFICAR ALTERACOES
:: ============================================================

echo [4/6] Checking for changes...
echo.

git status --short

echo.

:: ============================================================
:: ADICIONAR ARQUIVOS
:: ============================================================

echo Adding files...
echo.

git add .

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to execute git add.
    echo.
    pause
    exit /b 1
)

echo [OK] Files staged for commit.
echo.

:: ============================================================
:: VERIFICAR SE EXISTEM ALTERACOES
:: ============================================================

git diff --cached --quiet

if not errorlevel 1 (
    echo.
    echo ============================================================
    echo [INFO] NO CHANGES TO COMMIT
    echo ============================================================
    echo.
    echo The repository is already up to date.
    echo.
    pause
    exit /b 0
)

:: ============================================================
:: 5. SOLICITAR MENSAGEM DO COMMIT
:: ============================================================

echo [5/6] Preparing commit...
echo.

echo ============================================================
echo                  COMMIT MESSAGE
echo ============================================================
echo.
echo Use Conventional Commits standard. Examples:
echo   feat: add RFB data ingestion pipeline
echo.
echo   fix: correct silver establishments model
echo.
echo   docs: update project README
echo.
echo ============================================================
echo.

:COMMIT_MESSAGE

set "COMMIT_MSG="

set /p "COMMIT_MSG=Enter commit message: "

if "!COMMIT_MSG!"=="" (
    echo.
    echo [WARNING] Commit message cannot be empty.
    echo.
    goto COMMIT_MESSAGE
)

echo.
echo Selected message:
echo "!COMMIT_MSG!"
echo.

:: ============================================================
:: MOSTRAR ARQUIVOS
:: ============================================================

echo ============================================================
echo                  CHANGED FILES
echo ============================================================
echo.

git diff --cached --stat

echo.

:: ============================================================
:: CONFIRMACAO
:: ============================================================

echo ============================================================
echo                          SUMMARY
echo ============================================================
echo.

echo Repository:
echo %GITHUB_URL%

echo.
echo Branch:
echo !BRANCH!

echo.
echo Commit:
echo !COMMIT_MSG!

echo.

set /p "CONFIRM=Confirm commit and push? (Y/N): "

if /i not "!CONFIRM!"=="Y" (
    echo.
    echo [INFO] Operation cancelled.
    echo.
    pause
    exit /b 0
)

:: ============================================================
:: CRIAR COMMIT
:: ============================================================

echo.
echo Creating commit...
echo.

git commit -m "!COMMIT_MSG!"

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create commit.
    echo.
    pause
    exit /b 1
)

echo.
echo [OK] Commit created successfully.
echo.

:: ============================================================
:: 6. PUSH
:: ============================================================

echo [6/6] Pushing to GitHub...
echo.

git push -u origin "!BRANCH!"

if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ERROR] FAILED TO PUSH TO GITHUB
    echo ============================================================
    echo.
    echo Please verify:
    echo.
    echo - Internet connection
    echo - GitHub authentication/credentials
    echo - Repository permissions
    echo - Remote branch status
    echo - Possible merge conflicts
    echo.
    pause
    exit /b 1
)

:: ============================================================
:: SUCESSO
:: ============================================================

echo.
echo ============================================================
echo               PUSH COMPLETED SUCCESSFULLY!
echo ============================================================
echo.

echo Repository:
echo %GITHUB_URL%

echo.
echo Branch:
echo !BRANCH!

echo.
echo Commit:
echo !COMMIT_MSG!

echo.
echo ============================================================
echo               RADAR B2B IS UP TO DATE!
echo ============================================================
echo.

pause