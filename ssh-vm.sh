#!/bin/bash
# Connect to the OpenClaw VM and land in the trading-bot directory
# Run from the repo root: bash ssh-vm.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ssh -t \
    -i "$SCRIPT_DIR/OpenClaw_key.pem" \
    -o ServerAliveInterval=60 \
    -o ServerAliveCountMax=10 \
    azureuser@172.169.207.235 \
    "cd trading-bot && exec bash -l"