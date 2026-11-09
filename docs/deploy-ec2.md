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

## 9. Automatic deploy from GitHub (written 2026-11-09; a push ran test and deploy successfully, as reported by the owner)
**Status:** after setup, the first run failed in `test` because some offline tests start a real
headless Chromium and the runner had none; the workflow now runs `playwright install --with-deps
chromium` first. The first deploy also needed a manual `git pull` and `docker compose up -d --build`
on the server, because `scripts/deploy.sh` did not exist there yet. Not yet verified: the lock test
(a login with the CI key must print the hash error and give no shell), and a deploy that interrupts
a running scan.

On every push to `main`, `.github/workflows/deploy.yml` runs `ruff` and `pytest`; if both pass it
SSHes to the server with a **locked key** that can only run `scripts/deploy.sh <commit>`. The
script fast-forwards the checkout to that commit (it must be on `origin/main`), rebuilds with
`docker compose up -d --build`, and waits up to 180 s for the API to be healthy. Migrations are
not deployed: apply them by hand first.

One-time setup:
1. On your PC (not on the server): `ssh-keygen -t ed25519 -f irb_deploy_ci -N ""`. Never reuse your `.pem`.
2. On the server, add ONE line to `~/.ssh/authorized_keys`, with the contents of `irb_deploy_ci.pub` after `restrict `:
   ```
   command="bash /home/ubuntu/irb-watch/scripts/deploy.sh",restrict ssh-ed25519 AAAA... irb-deploy-ci
   ```
   `restrict` turns off shells, port forwarding and agent forwarding; `command=` forces the script.
3. Get the server's host key: on the server, `cat /etc/ssh/ssh_host_ed25519_key.pub`, and put
   `<EC2 address> ` in front of that line (for example `ec2-x-x-x-x.ap-northeast-2.compute.amazonaws.com ssh-ed25519 AAAA...`).
4. GitHub, repo, Settings, Secrets and variables, Actions, add four **repository secrets**:
   `DEPLOY_HOST` (Elastic IP or hostname, same as in step 3), `DEPLOY_USER` (`ubuntu`),
   `DEPLOY_SSH_KEY` (the whole private key file `irb_deploy_ci`), `DEPLOY_HOST_KEY` (the line from step 3).
   Then delete the private key file from the PC or keep it only in a password manager.
5. **Port 22.** GitHub's runners use changing addresses, so the "SSH only from My IP" rule must be
   widened to `0.0.0.0/0` for this to work. Password logins are off by default on Ubuntu EC2, and
   this key cannot open a shell, but the port is then open to the internet: expect scanning noise in
   the logs. Your own `.pem` stays the only key that opens a shell.
6. In GitHub, Settings, Branches, protect `main` (require the `test` check) if other people can push.

Notes:
- A deploy restarts the API container. A scan running at that moment dies; its lock clears after
  30 minutes. Avoid pushing around 06:00 Asia/Seoul.
- The server's `.env` is never touched; it stays only on the server.
- Roll back: push a revert commit (the script only moves forward).

## 10. Email alarms (Resend; written 2026-11-09, not run yet)
Admins are mailed when a source turns amber or red, when it recovers, and with the daily scan
result (after the 06:00 run). Off until all three settings exist.
1. Create a Resend account and an API key (resend.com). Keep the key out of chats and the repo.
2. In Resend, add your sending domain and add the DNS records it shows in Cloudflare (record
   type as shown, proxy status **DNS only**). Wait until the domain says verified. Until then
   Resend only delivers to your own account address.
3. On the server, in `~/irb-watch/.env` (typed by you, mode 600):
   - `RESEND_API_KEY=` the key
   - `MAIL_FROM=IRB Watch <alerts@your-domain>` (an address on the verified domain)
   - `APP_URL=https://your-frontend-address` (no trailing slash; used for the links)
   Then `docker compose up -d`.
4. Check: as an admin, `POST /admin/mail/test` sends a test mail to your own address (409 if a
   setting is missing, 502 with Resend's reason if the send is refused).
Mail problems never stop a scan. Look for `mail.sent` / `mail.failed` entries in the audit log.

## 11. Settings in AWS Parameter Store (written 2026-11-09, not run yet)
So you can change a setting in the AWS console instead of editing `.env` over SSH. The server
reads the parameters with its own IAM role (no AWS keys on the server) and rewrites `.env`
(`scripts/load-env.sh`, run by `scripts/deploy.sh`). Until you create the file `.ssm-path` on the
server, nothing changes: the hand-made `.env` keeps working.

**Costs and limits** (general knowledge, check AWS's pricing page): standard parameters are free;
`SecureString` with the AWS-managed key `aws/ssm` has no extra charge. A parameter cannot be
empty, so simply leave unused settings out. A value must not contain a newline or a single quote.

**1. Create the parameters** (console: Systems Manager, Parameter Store, Create parameter). Name
them `/irb-watch/prod/<SETTING>`, for example `/irb-watch/prod/OPENAI_API_KEY`. Use type
`SecureString` for every secret (`SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`,
`TYPESAFE_API_KEY`, `CREDENTIALS_KEY`, `RESEND_API_KEY`) and `String` for the rest
(`SUPABASE_URL`, `FRONTEND_ORIGINS`, `APP_URL`, `MAIL_FROM`, `OPENAI_MODEL`, `API_DOMAIN`,
`SCHEDULER_ENABLED`, ...). The names are exactly the names in `.env.example`.
Copy the real values from the current server `.env` once; never paste them into chats.

**2. Give the server permission.** IAM, Roles, create a role for EC2 (trusted entity: EC2) with
this inline policy, then attach the role to the instance (EC2, Actions, Security, Modify IAM
role). Replace `ACCOUNT_ID`; the region is Seoul:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["ssm:GetParametersByPath", "ssm:GetParameter", "ssm:GetParameters"],
    "Resource": [
      "arn:aws:ssm:ap-northeast-2:ACCOUNT_ID:parameter/irb-watch/prod",
      "arn:aws:ssm:ap-northeast-2:ACCOUNT_ID:parameter/irb-watch/prod/*"
    ]
  }]
}
```
If you encrypt with your own KMS key instead of `aws/ssm`, also allow `kms:Decrypt` on that key.

**3. Install the AWS CLI on the server** if `aws --version` does not work (Ubuntu 24.04:
`sudo snap install aws-cli --classic`).

**4. Try it by hand, then switch on:**
```bash
cd ~/irb-watch
git pull                               # or wait for the next deploy
cp .env .env.backup                    # keep the working file until you trust the new one
SSM_PATH=/irb-watch/prod bash scripts/load-env.sh   # prints "wrote N settings", no values
diff <(sed 's/=.*//' .env.backup | sort) <(sed 's/=.*//' .env | sort)   # same setting names?
echo /irb-watch/prod > .ssm-path       # from now on every deploy and refresh loads Parameter Store
docker compose up -d
```
If anything is wrong: `cp .env.backup .env` and delete `.ssm-path`.

**5. Change a setting later:** edit the parameter in the console, then on GitHub: Actions,
"refresh-settings", Run workflow. The server reloads the settings and restarts the API (about a
minute). A normal code deploy also reloads them. This needs the first deploy that contains
`scripts/deploy.sh` with the `refresh` command to have run once.

**Security notes:** the generated `.env` is mode 600 and still sits on the server, so the exposure
is the same as before; what changes is who edits it. Access is controlled by IAM and logged in
CloudTrail. Do not widen the policy to `*`. The refresh workflow uses the same locked SSH key as
the deploy: it can only run `deploy.sh refresh` or a commit deploy.

## 8. Security notes
- SSH only from your IP (update the rule when your home IP changes). Never open port 8000.
- `.env` has mode 600 and is not in the repository. The `.pem` key file stays on your PC.
- After it works you may turn Cloudflare's proxy on; then set Cloudflare SSL mode to **Full (strict)**.
