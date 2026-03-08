🚀 QuantBot Deployment Guide: From Local to Cloud

This guide covers the entire lifecycle of your trading bot:

Phase 1: Local Setup & Paper Trading (Safety Test)

Phase 2: Live Trading (Real Money)

Phase 3: Cloud Deployment (24/7 Uptime on Oracle Free Tier)

Phase 1: Local Setup & Paper Trading

Objective: Verify the logic works without risking a single dollar.

1. Install Dependencies

Open your terminal (Command Prompt or Terminal) and run:

pip install -r requirements.txt

2. Configure Environment

Ensure you have the file named .env in the same folder.

Open it. Leave BINANCE_API_KEY and BINANCE_SECRET_KEY empty.

The bot automatically detects missing keys and enters Paper Mode.

3. Run the Engine

Open a terminal and run:

python bot.py

What you see: The bot will initialize, connect to Binance (public data only), and start printing logs like [10:42:01] Price: 42000.00 | Bal: $1000.00.

What is happening: It simulates a wallet with $1,000. It performs "forward testing," making fake trades based on real-time price movements.

4. Launch the Dashboard

Open a second terminal window (keep the bot running in the first one) and run:

streamlit run dashboard.py

A browser window will open (usually http://localhost:8501).

Watch the "Total Equity" and "Trade Log" update in real-time as the bot runs in the background.

Phase 2: Transition to Real Money

Objective: Connect to Binance Futures.

1. Get API Keys

Log in to Binance -> API Management.

Create a new API Key.

Permissions: Check "Enable Futures". Do NOT check "Enable Withdrawals".

Copy the API Key and Secret Key.

2. Update Configuration

Stop the bot (Ctrl+C in the terminal).

Edit your .env file:

BINANCE_API_KEY=your_actual_api_key_here
BINANCE_SECRET_KEY=your_actual_secret_key_here

Safety Check: Change RISK_PER_TRADE to 0.01 (1%) for the first week.

3. Restart

Run python bot.py again.

Log Check: It should now say 🚀 LIVE TRADING MODE ACTIVATED.

Verify: Check your Binance mobile app to ensure orders appear when the bot says "ENTER LONG".

Phase 3: Cloud Deployment (Oracle Free Tier)

Objective: Run the bot 24/7 on a free Virtual Machine.

1. Create the Instance

Sign up for Oracle Cloud Free Tier.

Create a Compute Instance (VM.Standard.E2.1.Micro is free).

OS: Ubuntu 22.04.

Save the SSH Key (ssh-key-2024.key) to your computer.

2. Connect to Server

Open your terminal and SSH into the machine:

ssh -i "path/to/your/ssh-key.key" ubuntu@YOUR_INSTANCE_IP

3. Setup Environment on Server

Run these commands one by one to install Python and tools:

sudo apt update && sudo apt install python3-pip tmux -y

4. Upload Your Code

You can use scp (Secure Copy) to send your files from your local computer to the server. Run this from your local computer:

scp -i "path/to/key.key" bot.py dashboard.py requirements.txt .env ubuntu@YOUR_INSTANCE_IP:~/

5. Install Libraries & Run

Back in your SSH session on the server:

pip3 install -r requirements.txt

6. Run 24/7 using TMUX

tmux lets programs run even after you disconnect.

Step A: Start the Bot

tmux new -s trading_bot
python3 bot.py

# Press 'Ctrl+B' then 'D' to detach (exit while keeping it running)

Step B: Start the Dashboard

tmux new -s dashboard
streamlit run dashboard.py --server.port 8501

# Press 'Ctrl+B' then 'D' to detach

7. Access Dashboard Remotely

In Oracle Cloud Console -> Networking -> Security Lists.

Add an Ingress Rule: Allow TCP traffic on port 8501.

Visit http://YOUR_INSTANCE_IP:8501 in your browser.

Congratulations! Your quant system is now live in the cloud.
