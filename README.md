# AgentIQ

## 🚀 Initialization & Prerequisites

Before setting up the project, ensure you have the following installed on your system. You can check this by running the provided commands in your terminal.

### 1. Git
Check if Git is installed:
```shell
git -v
```
> **Not installed?** Download and install here:[Git v2.53.0 (Windows 64-bit)](https://github.com/git-for-windows/git/releases/download/v2.53.0.windows.2/Git-2.53.0.2-64-bit.exe)

### 2. Python (v3.11.9 strictly required)
Check if Python is installed and verify the version:
```shell
python -V
```
> **Not installed?** Download and install here: [Python 3.11.9 (amd64)](https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe)

### 3. Node.js & NPM
Check if Node and NPM are installed:
```shell
node -v
npm -v
```
> **Not installed?** Download and install here: [Node v22.22.2 (x64)](https://nodejs.org/dist/v22.22.2/node-v22.22.2-x64.msi)

---

## ⚙️ Installation & Setup

1. Open **CMD** in the directory where you want to save the project.
2. Run the following commands to clone the repository and set up the dependencies:

```shell
git clone https://github.com/Jagadish2003/AgentIQ.git
cd AgentIQ/backend
python -m venv .venv
.venv/scripts/activate
pip install -r requirements.txt
cd ..
cd frontend
npm install
```

---

## 🏃‍♂️ Running the WebApp

> ⚠️ **Important:** You will need to keep **two** separate terminal windows open to run both the backend and frontend servers simultaneously.

### Step 1: Start the Backend Server
Open **Git Bash** from the `AgentIQ\backend` directory (do not close this window):
```shell
source .venv/scripts/activate
./run.sh
```

### Step 2: Start the Frontend Server
Open **CMD** from the `AgentIQ\frontend` directory (do not close this window):
```shell
npm run dev
```

### Step 3: Access the Application
Once both servers are running, open your web browser and navigate to:  
👉 **http://localhost:5173/**
