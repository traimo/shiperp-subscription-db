# Start the subscription database UI on http://127.0.0.1:8001
Set-Location $PSScriptRoot
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
