@echo off
echo Starting Kal research scan...
cd C:\Users\andre\Desktop\kalshi-bot\bot
C:\Users\andre\AppData\Local\Programs\Python\Python313\python.exe main.py --research > C:\Users\andre\Desktop\kalshi-bot\research_output.txt 2>&1
echo Kal exited with code %ERRORLEVEL%
echo Output saved to research_output.txt
