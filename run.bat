@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================================
echo   MediaForge 2.0  -  Lanzador
echo ========================================================
echo.

REM --- [1/4] Python ---
echo [1/4] Verificando Python...
where py >nul 2>nul
if errorlevel 1 (
    echo   X  No se encontro el lanzador 'py'.
    echo      Instala Python desde https://www.python.org/downloads/
    echo      y marca "Add Python to PATH" + "Install py launcher".
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('py -3 --version 2^>^&1') do echo   OK Python:  %%v

echo.
echo [2/4] Verificando ffmpeg...
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo   !  ffmpeg NO esta en PATH. La conversion y transcripcion fallaran.
    echo      Como instalar:  winget install Gyan.FFmpeg
    echo.
) else (
    echo   OK ffmpeg:   detectado en PATH
)

echo.
echo [3/4] Verificando dependencias Python...
py -3 -c "import faster_whisper, customtkinter, fpdf, PIL, imagehash" >nul 2>nul
if errorlevel 1 (
    echo   Instalando dependencias ^(primera vez^)...
    py -3 -m pip install --upgrade pip >nul 2>nul
    py -3 -m pip install -r requirements.txt
    if errorlevel 1 (
        echo   X  Fallo la instalacion de dependencias.
        echo      Revisa tu conexion a internet / firewall.
        pause
        exit /b 1
    )
)
echo   OK dependencias listas.

REM [Opcional] Soporte GPU NVIDIA: si hay driver NVIDIA pero los wheels
REM nvidia-cublas-cu12 / nvidia-cudnn-cu12 / nvidia-cuda-runtime-cu12
REM no estan instalados, los instalamos automaticamente. Esto evita el
REM error "cublas64_12.dll is not found or cannot be loaded" que sale
REM cuando ctranslate2 intenta usar la GPU sin las DLLs disponibles.
where nvidia-smi >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import nvidia.cublas" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   GPU NVIDIA detectada. Para transcribir por GPU hacen falta
        echo   los wheels de CUDA ^(cuBLAS + cuDNN + runtime, ~1.4 GB^).
        choice /C SN /N /M "  Descargarlos ahora? [S/N]: "
        if errorlevel 2 goto :skip_cuda
        echo.
        py -3 -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12
        if errorlevel 1 (
            echo   !  No se pudieron instalar los wheels CUDA. La app
            echo      usara CPU ^(la transcripcion sigue funcionando^).
        ) else (
            echo   OK wheels CUDA instalados. La transcripcion por GPU
            echo      estara disponible ^(mas rapida^).
        )
    ) else (
        echo   OK wheels CUDA ya instalados.
    )
)

:skip_cuda
REM [Opcional] Si en el futuro quieres soporte Unicode completo (PDF con
REM japones, arabe, cirilico, etc.), descarga DejaVuSans.ttf y
REM DejaVuSans-Bold.ttf en la carpeta assets\. Hasta entonces, la app
REM usa Helvetica built-in y reemplaza cualquier caracter fuera de
REM Latin-1 (espanol funciona perfecto; japones/chino/arabe salen como ?).

echo.
echo [4/4] Lanzando MediaForge...
echo   Cuando cierres la ventana de la app, esta consola
echo   mostrara el resultado y se quedara esperando una tecla.
echo.

py -3 main.py
set "RC=%errorlevel%"

echo.
echo ========================================================
if %RC% neq 0 (
    echo   MediaForge termino con codigo de error: %RC%
    echo   Si viste una ventana de error arriba, copialo y pegamelo.
) else (
    echo   MediaForge se cerro normalmente.
)
echo ========================================================
echo.
pause