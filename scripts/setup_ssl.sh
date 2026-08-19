#!/bin/bash
# ===========================================
# OraclePoly - SSL Setup Script (Let's Encrypt)
# Run this on your VPS (example.com)
# ===========================================

set -e

DOMAIN="example.com"
EMAIL="admin@example.com"  # ЗАМЕНИТЕ на ваш email

echo "🔐 OraclePoly SSL Setup"
echo "========================"

# 1. Install Certbot
echo "📦 Installing Certbot..."
sudo apt update
sudo apt install -y certbot python3-certbot-nginx

# 2. Stop nginx temporarily for standalone certificate
echo "🔄 Stopping Nginx..."
sudo systemctl stop nginx || true

# 3. Get SSL Certificate
echo "📜 Obtaining SSL Certificate for $DOMAIN..."
sudo certbot certonly --standalone \
    -d "$DOMAIN" \
    --non-interactive \
    --agree-tos \
    --email "$EMAIL" \
    --no-eff-email

# 4. Copy our nginx config
echo "📋 Deploying Nginx config..."
sudo cp nginx/default.conf /etc/nginx/sites-available/oraclepoly
sudo ln -sf /etc/nginx/sites-available/oraclepoly /etc/nginx/sites-enabled/oraclepoly
sudo rm -f /etc/nginx/sites-enabled/default

# 5. Test nginx config
echo "✅ Testing Nginx configuration..."
sudo nginx -t

# 6. Start Nginx
echo "🚀 Starting Nginx with SSL..."
sudo systemctl start nginx
sudo systemctl enable nginx

# 7. Setup auto-renewal
echo "🔄 Setting up auto-renewal..."
(crontab -l 2>/dev/null; echo "0 0,12 * * * certbot renew --quiet --post-hook 'systemctl reload nginx'") | sort -u | crontab -

# 8. Create certbot webroot directory
sudo mkdir -p /var/www/certbot

echo ""
echo "✅ SSL Setup Complete!"
echo "========================"
echo "🌐 https://$DOMAIN"
echo ""
echo "Next steps:"
echo "  1. Set your Telegram bot token in .env"
echo "  2. Setup webhook: curl -X POST https://$DOMAIN/api/telegram/webhook/setup"
echo "  3. Test: curl https://$DOMAIN/health"
