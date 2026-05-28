# Unstop Rate Limiting Research - Findings Summary

## 1. What Was Done

- **Analyzed unstop.com's `robots.txt`** — revealed crawl restrictions, sitemaps, and API disallow rules
- **Downloaded and reverse-engineered the main Angular app bundle** (524KB) — identified all API endpoints, auth flows, and security mechanisms
- **Probed registration, login, and auth API endpoints** directly with HTTP requests to test rate limiting behavior
- **Examined client-side code** for rate limit, captcha, CSRF, and HMAC signing logic
- **Identified all CORS and security headers** on API responses

## 2. Key Findings

### Registration Endpoint
- **`POST /api/register`** is the registration endpoint (returns HTTP 422 with validation errors on bad input)
- Required fields: `first_name`, `last_name`, `username` (8-16 chars, alphanumeric + `-_`), `email`, `password`, `password_confirmation`
- **No rate limiting detected** — 20+ rapid sequential requests all returned HTTP 422 (validation errors), none returned HTTP 429
- No `X-RateLimit-*` headers in any response
- No captcha required on the registration endpoint (no captcha scripts loaded on the signup page)

### Login Endpoint
- **Login is client-side routed** at `/auth/login` (Angular SPA route)
- **No direct REST API login endpoint discovered** — `/api/login`, `/api/user/login`, `/api/v2/login` all return 404
- Login authentication is handled via OAuth token flows (`/api/oauth/token` returns 200 but is likely an OAuth2 server endpoint)
- Login modal is loaded as a **lazy-loaded Angular chunk** (`chunk-O2KIY6BX.js`) — could be extracted for analysis
- The frontend shows a "Please wait. Logging you in!" loader component, suggesting login may take time or involve multiple steps

### CSRF / Anti-CSRF Protection
- **`GET /api/micro/oauth/v2/generate/csrf-cookie`** — generates `XSRF-TOKEN` cookie with 2-hour expiry, `httponly`, `samesite=strict`
- The Angular app initializes this cookie on every page load

### HMAC Request Signing (Anti-Replay)
- The CORS headers accept custom security headers: `X-Nonce`, `X-Signature`, `X-Timestamp`
- This suggests **HMAC-based request signing** on sensitive API calls (anti-replay/anti-tampering)
- The app contains a debug endpoint that displays X-Timestamp, X-Nonce, and X-Signature values (likely for internal debugging)
- This HMAC scheme could be a rate limiting mechanism itself (nonce reuse detection = rate limit)

### Captcha
- **No captcha solution** found in the client-side code (no reCAPTCHA, hCaptcha, Turnstile, etc.)
- However, the CORS headers reference **`x-captcha-token`** as an allowed header — suggesting captcha is optional or used only for specific flows (possibly resume uploads or spammy actions)
- The main signup/login page does NOT load any captcha widget

### Enforcement Mechanism
- **Infrastructure**: Served via **CloudFront CDN** (not Cloudflare), hosted on AWS S3/CloudFront
- **Anti-replay via HMAC**: X-Nonce + X-Signature + X-Timestamp headers suggest server-side nonce validation
- **No IP-based rate limiting** observed during testing (20+ requests from same IP with no 429)
- **No visible captcha gating** on registration
- **CSRF tokens** provide basic request forgery protection but not rate limiting
- **Session-based**: Auth tokens stored in localStorage (`accessToken`, `currentUser` as encrypted item)

## 3. Potential Bypass Vectors

| Vector | Likelihood | Notes |
|--------|-----------|-------|
| **No captcha on registration** | HIGH | Registration endpoint `POST /api/register` has no captcha requirement, only CSRF token validation |
| **No IP rate limiting** | HIGH | 20+ rapid requests from same IP with no 429 response |
| **Lack of server-side throttling** | MEDIUM | All requests returned HTTP 422 (validation) rather than 429 (rate limited) |
| **Anti-replay HMAC bypass** | MEDIUM | If HMAC signing can be reversed or bypassed, the nonce/timestamp mechanism could be undermined |
| **Multiple auth flows** | MEDIUM | Different endpoints for OAuth, email login, Google login — each may have different rate limits |
| **X-Captcha-Token header present but optional** | MEDIUM | The header exists in allowed CORS list but captcha may only be enforced server-side intermittently |
| **Registration validation errors don't count toward limits** | LOW | All tested requests returned 422 validation errors — these may not trigger rate limit counters |

## 4. Recommended Testing Strategy

1. **Test registration with valid data** to see if successful registration has rate limiting
2. **Test the OAuth token endpoint** (`POST /api/oauth/token`) with various grant types to discover rate limits
3. **Attempt to trigger captcha** by sending repeated invalid logins
4. **Compare rate limits across auth methods** (Google OAuth vs email login vs SSO)
5. **Test without CSRF/XSRF cookie** to see if bypassing CSRF check also bypasses any rate limiting
6. **Test with different IPs/User-Agents** to detect IP-based vs session-based rate limiting
7. **Check if competition registration has different rate limits** than general account registration
8. **Analyze the HMAC signing implementation** to identify potential bypass of the nonce mechanism

## 5. Notes
- Unstop was formerly known as **Dare2Compete**
- The platform is built with **Angular** (not Next.js/React as initially assumed)
- Infrastructure: AWS (S3 + CloudFront), Laravel/PHP backend (based on `XSRF-TOKEN` format `eyJ...` = base64-encoded JWT)
- The `x-behaviour: api` header suggests a Laravel API backend
- The registration endpoint has no username availability check endpoint (username uniqueness is checked on submit)
