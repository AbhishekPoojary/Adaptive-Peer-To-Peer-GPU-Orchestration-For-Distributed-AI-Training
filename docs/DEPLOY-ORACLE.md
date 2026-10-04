# Put it on a free cloud server

This puts the website and the "brain" of the system on a free server that is
always on, with **permanent addresses** — so the link you share and the join
command your friends run **never change**, and nothing depends on your laptop
being switched on. Your laptop simply joins as one of the computers that train.

It takes about 30 minutes, once. You'll need a debit or credit card for Oracle's
identity check; the "Always Free" server used here is not charged.

> **The screens below are Oracle's.** They change their layout from time to
> time, so a button may be in a slightly different place — the names are what
> to look for.

---

## Step 1 — Create a free Oracle Cloud account

1. Go to **[oracle.com/cloud/free](https://www.oracle.com/cloud/free/)** and
   click **Start for free**.
2. Fill in your details. When asked for a **Home Region**, pick the one closest
   to you — it can't be changed later.
3. Add your card when asked. It's a check, not a charge.
4. Wait for the "your account is ready" email, then sign in.

---

## Step 2 — Create the server

1. In the Oracle Cloud console, open the menu (☰) → **Compute** →
   **Instances** → **Create instance**.
2. **Name:** anything, e.g. `orchestrator`.
3. **Image and shape** → **Edit**:
   - **Change image** → **Canonical Ubuntu** → the newest version → **Select image**.
   - **Change shape** → **Ampere** → **VM.Standard.A1.Flex** → set
     **2 OCPUs** and **12 GB memory** → **Select shape**.
     The card should say **Always Free-eligible**.
4. **Networking:** leave the defaults (a new network with a **public IPv4
   address**).
5. **Add SSH keys:** choose **Generate a key pair for me** and click
   **Save private key**. Keep this file safe — it's how you get into the server.
6. Click **Create**. After a minute or two it shows **Running**. Note the
   **Public IP address** on that page.

> **"Out of capacity"?** Free servers are popular. Try a different
> **Availability domain** (in the Placement section), or try again in a few
> hours.

---

## Step 3 — Let web traffic in

The server is behind a cloud firewall that only lets SSH in. Open the web ports:

1. On your instance's page, under **Primary VNIC**, click the **Subnet** link.
2. Click **Security Lists** → **Default Security List** → **Add Ingress Rules**.
3. Fill in:
   - **Source CIDR:** `0.0.0.0/0`
   - **IP Protocol:** TCP
   - **Destination Port Range:** `80,443`
4. Click **Add Ingress Rules**.

(The server's own firewall is opened for you by the setup command in step 5.)

---

## Step 4 — Connect to the server

On **Windows**, open **PowerShell** and run these, putting in your own file name
and IP address. The first line just makes Windows accept the key file.

```powershell
icacls "$HOME\Downloads\ssh-key-2026-10-04.key" /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i "$HOME\Downloads\ssh-key-2026-10-04.key" ubuntu@YOUR.PUBLIC.IP
```

On a **Mac or Linux**, in **Terminal**:

```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@YOUR.PUBLIC.IP
```

Type `yes` if it asks whether to continue connecting. You're in when the prompt
starts with `ubuntu@`.

---

## Step 5 — Set everything up

Paste this into the server window and press Enter:

```bash
sudo apt-get update -y && sudo apt-get install -y git && \
git clone https://github.com/AbhishekPoojary/Adaptive-Peer-To-Peer-GPU-Orchestration-For-Distributed-AI-Training.git orchestrator && \
cd orchestrator && bash deploy/cloud/setup.sh
```

It installs everything it needs, so the first run takes **5–15 minutes**. Near
the end it asks you to choose an **admin username and password** — that's your
account. When it finishes it prints:

```
   Website (send this to everyone):   https://app.YOUR.PUBLIC.IP.sslip.io
   Machines join through:             https://api.YOUR.PUBLIC.IP.sslip.io
```

**These addresses are permanent.** You can close the server window now; the
system keeps running on its own, and starts again by itself if the server
restarts.

---

## Step 6 — Start using it

1. Open the **Website** link and sign in with the admin account.
2. Add the computers that will do the training — including your own laptop:
   **Machines** → **Add a node**, and run the command it shows on each one
   ([README, Part 2](../README.md#part-2--lend-your-computer)). Each computer
   runs it once; from then on it reconnects by itself.
3. Send the Website link to your friends. They click **Create one** to make an
   account ([README, Part 1](../README.md#part-1--train-a-model)).

---

## Later

**Updating to a newer version.** Connect as in step 4, then:

```bash
cd orchestrator && git pull && bash deploy/cloud/setup.sh
```

Your accounts, datasets, models and settings are kept. Computers that have
joined update themselves.

**Turning off sign-ups.** On the server, run
`nano orchestrator/deploy/cloud/.env`, change `ALLOW_REGISTRATION=true` to
`false`, save (Ctrl+O, Enter, Ctrl+X), and run the update command above. You
can still add people on the **People** page.

**Google sign-in (optional).** Because the address never changes, Google
sign-in can work for everyone. In Google Cloud, create an OAuth client ID
(type *Web application*) with `https://app.YOUR.PUBLIC.IP.sslip.io` as an
**Authorized JavaScript origin**. Then add these two lines to
`deploy/cloud/.env` and run the update command:

```
GOOGLE_OAUTH_CLIENT_ID=your-id.apps.googleusercontent.com
GOOGLE_OAUTH_ORIGINS=https://app.YOUR.PUBLIC.IP.sslip.io
```

---

## Good to know

- **Free, with conditions.** Oracle's Always Free resources aren't charged.
  Oracle may reclaim an Always Free server that stays almost completely idle
  for a week; upgrading the account to *Pay As You Go* stops that, and Always
  Free resources stay free on it — but you'd be billed if you created anything
  beyond them.
- **Your data lives on this server.** Uploaded datasets and trained models are
  stored here (the server has a 50 GB disk by default, and up to 200 GB free).
  Each user can only see their own.
- **The address comes from the IP.** `sslip.io` turns the server's IP address
  into a web name for free, so HTTPS certificates work without buying a domain.
  If the server is ever deleted and recreated, its IP — and so the addresses —
  change. To make the IP permanent even then, reserve it under
  **Networking → Reserved public IPs**.

## If something goes wrong

**The website doesn't load.** Almost always step 3: check the rule allows TCP
ports `80,443` from `0.0.0.0/0`. Then run the setup command again — it checks
the website at the end and says if it still can't reach it.

**"Permission denied (publickey)" when connecting.** Use the key file you saved
in step 2, and the user name `ubuntu`.

**"Permissions for … are too open" (Windows).** Run the `icacls` line from step 4
again with your exact file name.

**Something else.** On the server, this shows what the orchestrator is doing:

```bash
cd orchestrator && sudo docker compose --env-file deploy/cloud/.env \
  -f deploy/compose.yaml -f deploy/cloud/compose.yaml logs --tail 50 orchestrator
```
