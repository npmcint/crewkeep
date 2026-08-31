#!/bin/bash
# deploy_to_nas.sh — deploy CrewKeep to NastyNas (NEW stack, needs Nigel's go).
#
# Nigel's locked rule: prod is touched ONLY via an approved gate. This script
# is the CrewKeep gate: tests green -> git clean -> build image -> create OR
# update stack `crewkeep` on the NAS -> health check on the LAN.
#
# Manual one-time steps (documented, NOT automated — need Nigel):
#   1. Caddy: add to /volume4/dockerfast/caddy/Caddyfile
#        http://crewkeep.nastynas.net { reverse_proxy crewkeep:8091 }
#      then `docker restart caddy` (reload misses replaced inodes).
#   2. Cloudflare tunnel: DNS CNAME crewkeep.nastynas.net -> HomeAssist tunnel
#      + ingress `crewkeep.nastynas.net -> http://192.168.1.99:8081`.
#   3. Create the data dir on the NAS: mkdir -p /volume4/dockerfast/crewkeep/data
#
# Usage: bash deploy_to_nas.sh [--dry-run]
set -euo pipefail
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
cd ~/crewkeep
PY=./.venv/bin/python
TOKEN="$(cat /Users/monday/.hermes/profiles/monday-main/portainer_api_key)"
API="http://192.168.1.99:9000"
STACK_NAME="crewkeep"

echo "== 1/4 test suite =="
"$PY" -m pytest tests/ -q 2>&1 | tail -1 | grep -E "passed|failed"

echo "== 2/4 git clean =="
git diff --quiet && git diff --cached --quiet \
  || { echo "ABORT: uncommitted changes — commit + push first."; exit 1; }
HEAD="$(git rev-parse --short HEAD)"
echo "clean at $HEAD"
[ "$DRY" = "1" ] && { echo "DRY-RUN OK — would deploy $HEAD. Run without --dry-run."; exit 0; }

echo "== 3/4 build image =="
export COPYFILE_DISABLE=1
tar --exclude='.venv' --exclude='__pycache__' --exclude='.git' --exclude='*.pyc' \
    --exclude='.DS_Store' --exclude='.auth.env' --exclude='data' --exclude='tests' \
    -cf /tmp/ckpromote.tar .
curl -s -X POST -H "X-API-Key: $TOKEN" -H "Content-Type: application/x-tar" \
  --data-binary @/tmp/ckpromote.tar \
  "$API/api/endpoints/3/docker/build?dockerfile=Dockerfile&t=crewkeep:latest" \
  -o /tmp/ckpromote-build.log -w "build: %{http_code}\n"
tail -c 300 /tmp/ckpromote-build.log | tr '\r' '\n' | grep -q "Successfully tagged" \
  || { echo "ABORT: build failed — see /tmp/ckpromote-build.log"; exit 1; }

NEWIMG=$(curl -s -H "X-API-Key: $TOKEN" "$API/api/endpoints/3/docker/images/json?all=1" \
  | "$PY" -c "import json,sys; ids=[i['Id'] for i in json.load(sys.stdin) if 'crewkeep:latest' in (i.get('RepoTags') or [])]; print(ids[0] if ids else '')")
[ -n "$NEWIMG" ] || { echo "ABORT: could not resolve crewkeep:latest image id"; exit 1; }
echo "new image: $NEWIMG"

echo "== 4/4 create-or-update stack '$STACK_NAME' =="
CK_DEEPSEEK_KEY="$(grep '^DEEPSEEK_API_KEY=' ~/jobhunt/.auth.env | head -1 | cut -d= -f2- || true)"
[ -n "$CK_DEEPSEEK_KEY" ] || { echo "ABORT: DEEPSEEK_API_KEY not found in ~/jobhunt/.auth.env"; exit 1; }
NEWIMG="$NEWIMG" CK_DEEPSEEK_KEY="$CK_DEEPSEEK_KEY" "$PY" - <<'PYEOF'
import json, os, urllib.request
TOKEN = open('/Users/monday/.hermes/profiles/monday-main/portainer_api_key').read().strip()
BASE = 'http://192.168.1.99:9000'
ENV_LIST = [{"name": "DEEPSEEK_API_KEY", "value": os.environ.get("CK_DEEPSEEK_KEY", "")}]
COMPOSE = """services:
  crewkeep:
    image: crewkeep:latest
    restart: unless-stopped
    environment:
      - CREWKEEP_HOST=0.0.0.0
      - CREWKEEP_PORT=8091
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
    volumes:
      - /volume4/dockerfast/crewkeep/data:/app/data
    networks:
      - caddy_default
networks:
  caddy_default:
    external: true
"""
def api(path, body=None, method=None):
    headers = {'X-API-Key': TOKEN, 'Content-Type': 'application/json'}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method or ('POST' if body is not None else 'GET'))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {'_error': e.code, '_body': e.read().decode()[:300]}

# 1) does the stack already exist? (plain GET /api/stacks — the
#    ?filters={"EndpointId":3} query 404s on Portainer 2.39.5)
stacks = api('/api/stacks')
if not isinstance(stacks, list):
    raise SystemExit(f'ABORT: unexpected stacks response: {stacks}')
existing = [s for s in stacks if s.get('Name') == 'crewkeep']
if existing:
    sid = existing[0]['Id']
    res = api(f'/api/stacks/{sid}?endpointId=3',
              {'StackFileContent': COMPOSE, 'Env': ENV_LIST, 'Prune': True, 'PullImage': False},
              method='PUT')
    assert res.get('DeploymentStartStatus') == 1, res
    print(f'stack {sid} updated (start status {res.get("DeploymentStartStatus")})')
else:
    res = api(f'/api/stacks/create/standalone?endpointId=3',
              {'Name': 'crewkeep', 'StackFileContent': COMPOSE, 'Env': ENV_LIST})
    assert res.get('Id'), res
    print(f"stack created id={res['Id']} name={res.get('Name')}")
PYEOF

echo "== health check (LAN) =="
for _ in $(seq 1 12); do
  if curl -s -o /dev/null -w "%{http_code}" http://192.168.1.99:8091/ | grep -q 200; then
    echo "crewkeep UP on LAN ✅  ($HEAD)"
    exit 0
  fi
  sleep 5
done
echo "ABORT: crewkeep not reachable on 192.168.1.99:8091 after deploy"
exit 1
