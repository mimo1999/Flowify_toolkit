# PowerShell script to start the backend server
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Made with Bob
