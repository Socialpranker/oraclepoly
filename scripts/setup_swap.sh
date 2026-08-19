#!/bin/bash
# OraclePoly - Swap Setup Script
# Adds 2GB of swap space to prevent server freezes

set -e

SWAP_FILE="/swapfile"
SWAP_SIZE="2G"

echo "🚀 Setting up Swap Space..."

# Check if swap exists
if swapon --show | grep -q "$SWAP_FILE"; then
    echo "✅ Swap file already exists and is active."
    exit 0
fi

# Create swap file
if [ ! -f "$SWAP_FILE" ]; then
    echo "📦 Creating $SWAP_SIZE swap file..."
    sudo fallocate -l $SWAP_SIZE $SWAP_FILE
    sudo chmod 600 $SWAP_FILE
    sudo mkswap $SWAP_FILE
else
    echo "⚠️  Swap file exists but not active. Activating..."
fi

# Enable swap
sudo swapon $SWAP_FILE

# Make permanent
if ! grep -q "$SWAP_FILE" /etc/fstab; then
    echo "$SWAP_FILE none swap sw 0 0" | sudo tee -a /etc/fstab
    echo "✅ Added to /etc/fstab"
fi

echo "✅ Swap enabled successfully!"
echo "Current Swap Status:"
sudo swapon --show
free -h
