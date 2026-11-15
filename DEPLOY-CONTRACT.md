# For developers: how to get your app deployed

You write the app and keep it in your own private GitHub repo. Someone with Azure
access runs the deployment agent against your repo and sends you back the URLs.
This page is the contract your repo must satisfy.

## 1. Install the deploy GitHub App on your repo (once)

Open https://github.com/apps/az-deploy-agent-shiperp/installations/new, choose **Only select
repositories**, pick your repo, and click **Install**. The app can only *read* that
repo's contents, the token it uses lives one hour, and you can uninstall it any time
from GitHub → Settings → Applications.

## 2. What your repo must contain

```
compose.yaml                # one service per container app (az-deploy-agent reads this name only)
services/<name>/Dockerfile  # one Dockerfile per service (any layout; path set in compose `build`)
```

Images are built by Azure Container Registry Tasks from your Dockerfile. Nothing in
your repo is executed on the operator's machine.

## 3. Compose conventions

| Compose | What happens in Azure |
|---|---|
| `build:` | image built and tagged with the git commit SHA |
| `ports: ["8080:8000"]` | HTTPS ingress to container port **8000** (first port only) |
| no `ports` | no ingress (background worker) |
| `environment:` with literal values | plain env vars |
| `environment: {DB_URL: ${DB_URL}}` (no default) | **secret** read from Key Vault secret `db-url` |
| `environment: {FLAG: ${FLAG:-off}}` | plain env var with the default value |
| `secrets: [my_key]` + top-level `secrets: {my_key: {external: true}}` | file mounted at `/run/secrets/my_key` from Key Vault secret `my-key` |
| `healthcheck.test: ["CMD","curl","-f","http://localhost:8000/healthz"]` | HTTP liveness + readiness probe on `/healthz` |
| `deploy.replicas: 2` | min = max = 2 replicas |
| `x-azure: {min_replicas: 0, max_replicas: 3}` | scale range (0 = scale to zero) |
| `deploy.resources.limits: {cpus: "0.5", memory: 1G}` | snapped to a Container Apps CPU/memory combo |
| `depends_on` | deployment ordering |
| `command` / `entrypoint` | container args / command |
| Azure Functions base image or `host.json` in the build context | deployed as a Functions app on Container Apps |

**Secrets never go in the repo.** Reference them as `${NAME}` and tell the operator
which names you need. Each app gets its own Key Vault (`kv-<repo>-xxxx`, in the app's own
resource group `rg-app-<repo>`); the operator creates it on the first run and sets the
values there before the deploy. No other app can read it. A literal password in compose is
flagged as a warning in the deployment report.

## 4. Things Container Apps cannot do

The agent reports these and stops if they would change behavior:

- host bind mounts and named volumes (alternative: Azure Files share)
- `privileged`, `cap_add`, `devices`, `sysctls`, `network_mode`, `pid`, `ipc`
- more than one published port per service (use one app per port)
- `exec`-style healthchecks without an HTTP URL (a TCP probe is used instead)

## 5. Your container app names

Each service becomes `<repo-name>-<service>` (lowercase), e.g. repo `shop-api`
with services `api` and `worker` → `shop-api-api`, `shop-api-worker`. Apps are
tagged with your repo name; a deploy from a different repo cannot overwrite them.

## 6. Public or VPN-only

Tell the operator whether your app should be reachable from the internet or only from the
office network / VPN. That is chosen by the operator at deploy time, not in your repo.

## 7. What you get back

A report with, per service: create/update, image tag (git SHA), revision name,
endpoint URL, health check result, any warnings, and the exact rollback command.
