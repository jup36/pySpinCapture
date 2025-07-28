@echo off
REM Activate the conda environment
call C:\ProgramData\anaconda3\condabin\conda.bat activate behvid
REM Run the Python script with arguments
python C:\Users\buschmanlab\Documents\pySpinCapture\cameraCaptureFaceCamPulse.py %1 %2 %3
