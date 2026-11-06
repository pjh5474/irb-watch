# Deploying to one Ubuntu server (AWS EC2, also works on Oracle or any VM)

What runs: Docker Compose starts two containers, the API (with headless Chromium, the daily
scheduler on) and Caddy (HTTPS). Hostname used below: `irb-api.warwarsn.online`.

**Status:** written 2026-11-06. The Dockerfile and compose file were **not built or run** by the
author (no Docker on the dev PC), so the first build on the server is the first real test.

## 0. Before you start (done in the consoles)
- EC2 instance: Ubuntu 24.04, **at least 2 GB memory**, 20 GB `gp3` disk, region Seoul.
- Security group inbound: SSH 22 from **My IP**, HTTP 80 and HTTPS 443 from anywhere. Nothing else (not 8000).
- An **Elastic IP** attached to the instance.
- Cloudflare DNS: record type **A**, name `irb-api`, value = the Elastic IP, proxy status **DNS only** (grey cloud) until it works.
- Supabase: the migrations are applied, and the encrypted login for the scan is already stored (`site_credentials` row, via `migrate_credentials`).

## 1. Prepare the server (SSH in as `ubuntu`)
```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu        # then log out and log in again

# 2 GB swap file: a safety net for Chromium on a small server
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h                                # shows memory and swap
```

## 2. Get the code
Public repo:
```bash
git clone https://github.com/pjh5474/irb-watch.git ~/irb-watch
```
Private repo (read-only deploy key):
```bash
ssh-keygen -t ed25519 -f ~/.ssh/irb_deploy -N ""
cat ~/.ssh/irb_deploy.pub              # copy this line
```
GitHub, repo, Settings, Deploy keys, Add deploy key (leave "write access" **off**), paste it. Then:
```bash
printf 'Host github.com\n  IdentityFile ~/.ssh/irb_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
git clone git@github.com:pjh5474/irb-watch.git ~/irb-watch
```

## 3. Create the settings file by hand (never commit it)
```bash
cd ~/irb-watch
cp .env.example .env
nano .env                              # fill in the values, save with Ctrl+O, Enter, Ctrl+X
chmod 600 .env
```
Fill in, copying values from your own env file on the PC:
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- `FRONTEND_ORIGINS` = your frontend address, exactly, with no trailing slash
- `OPENAI_API_KEY`, `OPENAI_MODEL`, `TYPESAFE_API_KEY` (AI parts stay off without them)
- `CREDENTIALS_KEY` = **the same key** that encrypted the stored login, or the login cannot be read
- `ACCESS_ID_HASH_KEY` (keep the same value as on the PC so counts stay consistent)
- `API_DOMAIN=irb-api.warwarsn.online`
- `SCHEDULER_ENABLED=true` (the default in the compose file)
Do not paste these values into chats or tickets. Keep a safe backup of `CREDENTIALS_KEY` somewhere other than the server.

## 4. Check the hospital site is reachable from this server
```bash
curl -sI "https://hrpp.snuh.org/hrpp/lgn/form.do?returnUrl=%2F" | head -5
```
`HTTP/2 200` (or a redirect) is good. A timeout or `403` suggests the site blocks this provider's
addresses; the scans will not work from this server.

## 5. Start
```bash
cd ~/irb-watch
docker compose up -d --build           # first build takes several minutes (downloads Chromium)
docker compose ps                      # both should be "running"; api becomes "healthy" after ~40 s
docker compose logs --tail 50 api
curl -s https://irb-api.warwarsn.online/health     # {"status":"ok"}
```
Fifteen seconds after the API starts, the scheduler's **catch-up** scans every active source whose
last saved scan is older than 24 hours, so expect scans right away on the first start.

## 6. Daily use
- Update after a code change: `cd ~/irb-watch && git pull && docker compose up -d --build`
- Logs: `docker compose logs --tail 100 api` (add `-f` to follow)
- Stop the daily scan: set `SCHEDULER_ENABLED=false` in `.env`, then `docker compose up -d`
- Free disk after many rebuilds: `docker system prune -f`
- Scan everything now (admin login token needed): `POST /admin/scans/run-all`

## 7. When something fails
| Symptom | Likely cause |
|---|---|
| `https://...` shows a certificate error or nothing | DNS record missing or wrong IP, port 80 or 443 closed in the security group, or Cloudflare proxy (orange cloud) turned on too early. Check `docker compose logs caddy`. |
| `docker compose up` stops with `set API_DOMAIN in .env` | `API_DOMAIN` is missing from `.env`. |
| API container keeps restarting | A setting is wrong: read `docker compose logs api`. |
| Browser (Chromium) crashes or the server freezes | Not enough memory: check `free -h` and use a 2 GB or larger instance. |
| Scan of a members-only source says `CREDENTIALS_KEY ... cannot be decrypted` | The key in `.env` is not the key used when the login was stored. |
| Frontend gets a CORS error | `FRONTEND_ORIGINS` does not match the frontend address exactly. |

## 8. Security notes
- SSH only from your IP (update the rule when your home IP changes). Never open port 8000.
- `.env` has mode 600 and is not in the repository. The `.pem` key file stays on your PC.
- After it works you may turn Cloudflare's proxy on; then set Cloudflare SSL mode to **Full (strict)**.
