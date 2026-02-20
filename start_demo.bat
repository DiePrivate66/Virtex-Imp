@echo off
title SERVIDOR RAMON BY BOSCO - MODO DEMO
color 0A

echo ==================================================
echo   INICIANDO SISTEMA RAMON BY BOSCO (DEMO)
echo ==================================================
echo.

:: 1. Activar Entorno (CRÍTICO PARA DEMO)
:: Si no se activa, no encuentra Django
call venv\scripts\activate
if %errorlevel% neq 0 (
    echo [ERROR] No se encontro el entorno virtual 'venv'.
    echo Asegurate de que la carpeta 'venv' exista.
    pause
    exit
)

:: 2. Verificar IP Local
echo [INFO] Tu IP Local para conectar le celular es:
ipconfig | findstr /i "IPv4"
echo.

:: 3. Iniciar Servidor
echo [INFO] Iniciando Django en 0.0.0.0:8000...
echo [INSTRUCCION] En el celular del cliente, abre Chrome y escribe:
echo              http://TU_IP_DE_ARRIBA:8000
echo.

python manage.py runserver 0.0.0.0:8000

pause
