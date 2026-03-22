@echo off
C:\Users\andre\AppData\Local\Programs\Python\Python313\python.exe -m pip install anthropic httpx "supabase>=2.0" python-dotenv schedule "pydantic>=2.0" pydantic-settings tenacity structlog cryptography > C:\Users\andre\Desktop\kalshi-bot\pip_output.txt 2>&1
echo EXIT_CODE=%ERRORLEVEL% >> C:\Users\andre\Desktop\kalshi-bot\pip_output.txt
