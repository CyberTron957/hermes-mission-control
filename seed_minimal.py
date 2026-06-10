"""Seed Option 1 (SaaS Startup) with minimal system prompts (souls).

Roster: founder, product, engineer, devops, growth, sales, overseer.
"""
import json, urllib.request, urllib.error, sqlite3

BASE = "http://127.0.0.1:8011"
TEAM = "saas"

def call(m, p, b=None):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(BASE + p, data=d, method=m, headers={"Content-Type": "application/json"})
    def _parse(raw):
        try: return json.loads(raw or "{}")
        except Exception: return {"raw": (raw or "")[:200]}
    try:
        with urllib.request.urlopen(r, timeout=60) as x:
            return x.status, _parse(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read().decode())
    except Exception as e:
        return 0, {"error": str(e)}

# ---------------------------------------------------------------- souls (minimal)
FOUNDER = """You are the Founder (CEO) of an AI-run SaaS startup. Focus on revenue.
Read the PROJECT BRIEF (workspace.md) for goals.
Delegate tasks to:
- product: specs, onboarding, landing/pricing copy.
- engineer: backend/frontend code, billing setup (verifies locally).
- devops: production deployments and infrastructure.
- growth: blog posts, social media, and acquisition.
- sales: prospect lists, outreach, and user conversions.
Maintain the revenue scorecard in docs/scorecard.md."""

PRODUCT = """You are Product & Design. Focus on user activation and upgrade flows.
Write specs (PRDs), landing page copy, and UI flow designs to docs/specs/.
Coordinate specs with engineer and share marketing positioning with growth."""

ENGINEER = """You are the Full-Stack Engineer. Build the product (frontend/backend) and payment flows.
Work in the shared repo. Verify changes locally (run/test) before committing.
Do NOT touch production. Hand off commits and deploy instructions to devops."""

DEVOPS = """You are DevOps / SRE. You own deployment, infrastructure, proxy, and uptime.
You are the ONLY agent permitted to touch production.
Deploy from the shared repo, check configs, and verify changes live."""

GROWTH = """You are Growth & Marketing. Focus on organic/paid traffic and signups.
Publish blog posts, launch campaigns, and post to social media platforms."""

SALES = """You are Sales & Customer Success. Focus on trial-to-paid conversion and support.
Source prospect lists, send outreach emails/DMs, and answer customer queries."""

OVERSEER = """You are the team Supervisor. Do no project tasks.
Monitor founder, product, engineer, devops, growth, and sales.
Look for loops or safety violations. Intervene with messages or pause_agent when needed."""

AGENTS = [
    {"id": "founder",  "name": "Founder (CEO)",       "soul": FOUNDER,
     "toolsets": ["web", "memory", "todo"],
     "peers": ["product", "engineer", "devops", "growth", "sales"]},
    {"id": "product",  "name": "Product & Design",    "soul": PRODUCT,
     "toolsets": ["web", "browser", "file", "memory", "todo"],
     "peers": ["founder", "engineer", "growth"]},
    {"id": "engineer", "name": "Engineer (Full-stack)", "soul": ENGINEER,
     "toolsets": ["file", "terminal", "code_execution", "web", "browser", "memory", "todo"],
     "peers": ["founder", "product", "devops"]},
    {"id": "devops",   "name": "DevOps / SRE",        "soul": DEVOPS,
     "toolsets": ["terminal", "file", "web", "memory", "todo"],
     "peers": ["founder", "engineer"]},
    {"id": "growth",   "name": "Growth & Marketing",  "soul": GROWTH,
     "toolsets": ["web", "browser", "file", "image_gen", "memory", "todo"],
     "peers": ["founder", "product", "sales"]},
    {"id": "sales",    "name": "Sales & Success",     "soul": SALES,
     "toolsets": ["web", "browser", "file", "memory", "todo"],
     "peers": ["founder", "growth"]},
    {"id": "overseer", "name": "Overseer (Supervisor)", "soul": OVERSEER,
     "toolsets": ["memory"], "is_supervisor": True, "threshold": 8000,
     "peers": ["founder", "product", "engineer", "devops", "growth", "sales"]},
]

# ---------------------------------------------------------------- 1. delete existing
st, data = call("GET", "/agents")
existing = list((data.get("agents") or {}).keys())
print("existing agents:", existing)
for n in existing:
    call("POST", "/agent/%s/stop" % n)
    print("  delete %-9s" % n, call("DELETE", "/agent/%s" % n)[0])

# ---------------------------------------------------------------- 2. clear monitoring history
try:
    c = sqlite3.connect("data/monitoring.db"); cur = c.cursor()
    for t in ["messages", "events", "digests"]:
        try: cur.execute("DELETE FROM %s" % t)
        except Exception: pass
    c.commit(); c.close(); print("monitoring.db history cleared")
except Exception as e:
    print("monitoring clear err:", e)

# ---------------------------------------------------------------- 3. ensure team
print("team:", call("POST", "/teams", {"team_id": TEAM, "name": "SaaS Startup"})[0])

# ---------------------------------------------------------------- 4. create + config
for a in AGENTS:
    call("POST", "/agent", {"agent_name": a["id"], "name": a["name"], "team_id": TEAM,
                            "role_soul": a["soul"], "is_supervisor": a.get("is_supervisor", False)})
    cfg = {"enabled_toolsets": a["toolsets"], "autonomous": False, "max_iterations": 25}
    if a.get("threshold"):
        cfg["supervisor_token_threshold"] = a["threshold"]
    call("PATCH", "/agent/%s/config" % a["id"], cfg)
    print("  created+configured %-9s" % a["id"])

# ---------------------------------------------------------------- 5. wire peers
for a in AGENTS:
    call("POST", "/agent/%s/peers" % a["id"], {"peers": a["peers"]})

# ---------------------------------------------------------------- 6. verify
print("\n== roster ==")
st, data = call("GET", "/agents")
for n in sorted(data.get("agents", {})):
    a = data["agents"][n]
    print("  %-9s sup=%-5s peers=%s" % (n, a.get("is_supervisor", False), a.get("allowed_peers")))
print("\nAll agents are reactive (autonomous=false). Ready.")
