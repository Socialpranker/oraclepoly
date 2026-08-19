# OraclePoly: Security & DDoS Protection

This document outlines the security measures implemented to protect the OraclePoly API from abuse, DDoS attacks, and unauthorized access.

---

## 🛡️ Multi-Layer Protection

We employ a "Defense in Depth" strategy across three main layers:

### 1. Edge Protection (Cloudflare)
The first line of defense is **Cloudflare**. It handles basic DDoS mitigation and provides a Web Application Firewall (WAF).
- **WAF Rules**: Block common SQL injection, XSS, and bot patterns.
- **Under Attack Mode**: Can be enabled during a heavy DDoS attack to force JS challenges for all users.
- **Geo-Blocking**: (Optional) Block traffic from countries where your users don't reside.

### 2. Infrastructure Protection (Rate Limiting)
Implemented at the application level using `slowapi` (Python).
- **Global Limits**: 100 requests per minute per IP address.
- **Auth Limits**: 5 attempts per minute for sensitive endpoints (e.g., token validation).
- **Implementation**: See `app/core/rate_limiter.py`.

### 3. Application Protection (TMA Validation)
Since this is a Telegram Mini App, we don't use standard JWT tokens. Instead:
- **TMA initData Validation**: Every request must include the `X-Telegram-Init-Data` header.
- **HMAC Verification**: The backend validates the integrity of this data using the Telegram Bot Token. This prevents anyone from spoofing user identities.
- **Scoped CORS**: API access is restricted to official frontend origins (`http://localhost:5173`, `*.vercel.app`, `*.ngrok-free.app`).

---

## 🛠️ Configuration

### Rate Limiting Headers
When a user is rate-limited, they will receive a `429 Too Many Requests` status code with the following headers:
- `X-RateLimit-Limit`: Maximum number of requests.
- `X-RateLimit-Remaining`: Remaining requests in the current window.
- `X-RateLimit-Reset`: Time when the limit resets.

### Adding Rate Limits to Routes
To add a limit to a specific route, use the `@limiter.limit` decorator:

```python
from app.core.rate_limiter import limiter

@router.get("/sensitive-data")
@limiter.limit("10/minute")
async def get_data(request: Request):
    return {"data": "..."}
```

---

## 🚨 Incident Response
If a DDoS attack is detected:
1. Enable **Cloudflare Under Attack Mode**.
2. Increase Rate Limiting strictness in `app/core/rate_limiter.py`.
3. Monitor PostgreSQL logs for unusual query patterns (`docker logs oraclepoly-postgres`).
