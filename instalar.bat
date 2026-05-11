@echo off
cd /d "%~dp0"
echo Criando ambiente virtual (Python 3.11)...
py -3.11 -m venv venv
echo.
echo Atualizando pip...
venv\Scripts\python -m pip install --upgrade pip
echo.
echo Instalando dependencias...
venv\Scripts\pip install -r requirements.txt
echo.
echo Pronto! Use run.bat para iniciar.
pause
