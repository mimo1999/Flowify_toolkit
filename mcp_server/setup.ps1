# Flowify MCP Server Setup Script for Windows
# This script installs dependencies and configures the MCP server for Bob

Write-Host "=== Flowify MCP Server Setup ===" -ForegroundColor Cyan
Write-Host ""

# Check Python installation
Write-Host "Checking Python installation..." -ForegroundColor Yellow
$pythonVersion = python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python is not installed or not in PATH" -ForegroundColor Red
    Write-Host "Please install Python 3.8+ from https://www.python.org/" -ForegroundColor Red
    exit 1
}
Write-Host "Found: $pythonVersion" -ForegroundColor Green

# Install dependencies
Write-Host ""
Write-Host "Installing MCP server dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install dependencies" -ForegroundColor Red
    exit 1
}
Write-Host "Dependencies installed successfully" -ForegroundColor Green

# Check if FastAPI backend is running
Write-Host ""
Write-Host "Checking FastAPI backend..." -ForegroundColor Yellow
try {
    $response = Invoke-WebRequest -Uri "http://localhost:8000" -TimeoutSec 5 -ErrorAction Stop
    Write-Host "FastAPI backend is running" -ForegroundColor Green
} catch {
    Write-Host "WARNING: FastAPI backend is not running" -ForegroundColor Yellow
    Write-Host "Start it with: cd ..\backend && uvicorn app.main:app --port 8000" -ForegroundColor Yellow
}

# Verify MCP configuration
Write-Host ""
Write-Host "Verifying MCP configuration..." -ForegroundColor Yellow
$mcpConfigPath = "..\\.claude\\mcp.json"
if (Test-Path $mcpConfigPath) {
    Write-Host "MCP configuration found at $mcpConfigPath" -ForegroundColor Green
    $mcpConfig = Get-Content $mcpConfigPath | ConvertFrom-Json
    if ($mcpConfig.mcpServers.flowify) {
        Write-Host "Flowify MCP server is configured" -ForegroundColor Green
    } else {
        Write-Host "WARNING: Flowify server not found in MCP config" -ForegroundColor Yellow
    }
} else {
    Write-Host "WARNING: MCP configuration not found" -ForegroundColor Yellow
    Write-Host "Expected at: $mcpConfigPath" -ForegroundColor Yellow
}

# Test MCP server
Write-Host ""
Write-Host "Testing MCP server..." -ForegroundColor Yellow
Write-Host "Running: python -m flowify_mcp --help" -ForegroundColor Gray
python -c "import sys; sys.path.insert(0, '.'); from flowify_mcp import app; print('MCP server module loaded successfully')"
if ($LASTEXITCODE -eq 0) {
    Write-Host "MCP server module is working" -ForegroundColor Green
} else {
    Write-Host "ERROR: MCP server module failed to load" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "1. Ensure FastAPI backend is running: cd ..\backend && uvicorn app.main:app --port 8000" -ForegroundColor White
Write-Host "2. Restart VS Code to load the MCP server" -ForegroundColor White
Write-Host "3. Ask Bob: 'What tools do you have available?'" -ForegroundColor White
Write-Host "4. Test with: 'Ingest the repository at D:/Projects/hackathon/Flowify'" -ForegroundColor White
Write-Host ""
Write-Host "Documentation:" -ForegroundColor Yellow
Write-Host "- Quick Start: .\README.md" -ForegroundColor White
Write-Host "- Full Guide: ..\docs\MCP_INTEGRATION.md" -ForegroundColor White
Write-Host "- Testing: ..\tests\test_llm_integration.md" -ForegroundColor White
Write-Host ""

# Made with Bob
