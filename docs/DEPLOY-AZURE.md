# Put it on a free Azure server (students)

This puts the website and the "brain" of the system on a server in Microsoft's
cloud that is always on, with **permanent addresses** — so the link you share
and the join command your friends run **never change**, and nothing depends on
your laptop being switched on. Your laptop simply joins as one of the computers
that train.

**No credit card needed.** It uses **Azure for Students** ($100 of credit a
year), which you can activate through the
[GitHub Student Developer Pack](https://education.github.com/pack) or at
[azure.microsoft.com/free/students](https://azure.microsoft.com/en-us/free/students).
The small server used here is part of Azure's free services for a year, so the
credit only pays for its public address — about $4 a month.

It takes about 30 minutes, once.

> **The screens below are Microsoft's.** They change their layout from time to
> time, so a button may be in a slightly different place — the names are what
> to look for.

---

## Step 1 — Activate Azure for Students

If [portal.azure.com](https://portal.azure.com) shows **Education | Overview**
with **$100 out of $100** of credit, you're done with this step.

---

## Step 2 — Create the server

1. In [portal.azure.com](https://portal.azure.com), search for **Virtual
   machines** at the top, open it, and click **Create** → **Azure virtual
   machine**.
2. On the **Basics** tab:
   - **Subscription:** *Azure for Students*.
   - **Resource group:** **Create new** → `orchestrator`.
   - **Virtual machine name:** `orchestrator`.
   - **Region:** one close to you (for India: *Central India* or *South India*).
   - **Availability options:** *No infrastructure redundancy required*.
   - **Image:** **Ubuntu Server 24.04 LTS – x64 Gen2** (or the newest Ubuntu
     LTS offered).
   - **Size:** click **See all sizes**, search `B1s`, select **B1s**, and click
     **Select**. It's marked as eligible for free services.
   - **Authentication type:** *SSH public key*.
   - **Username:** `azureuser`.
   - **SSH public key source:** *Generate new key pair*.
   - **Public inbound ports:** *Allow selected ports*, and tick **HTTP (80)**,
     **HTTPS (443)** and **SSH (22)**.
3. On the **Disks** tab: set **OS disk size** to **64 GiB** and **OS disk type**
   to **Premium SSD**. That's the size included in the free services, and room
   for your datasets.
4. On the **Management** tab (or **Monitoring**): make sure **Enable
   auto-shutdown** is **off** — otherwise the server switches itself off every
   evening.
5. Click **Review + create**, then **Create**. When asked, click **Download
   private key and create resource** and keep the `.pem` file safe — it's how
   you get into the server.
6. When it says *Your deployment is complete*, click **Go to resource** and note
   the **Public IP address**.

> **B1s greyed out ("NotAvailableForSubscription")?** Student subscriptions
> can't use every size in every region. Either:
>
> - click **Find deployable options** next to **B1s** to see the regions where
>   you *can* use it, then change **Region** on the Basics tab to one of them
>   (the nearest you can get; for India try *Central India*, *West India*, then
>   *Southeast Asia*); or
> - search for **B2ats_v2** instead. It's also in the free services (2 CPUs,
>   1 GB memory) and works the same way.
>
> **"RequestDisallowedByAzure … best available regions" when you click
> Create?** Student subscriptions may only create resources in a few regions,
> and which ones differs per account. To see yours: search **Policy** in the
> portal → **Assignments** → *Allowed resource deployment regions* → **View
> assignment** → **Parameters**. Pick the nearest region on that list, and
> check your size is offered there.
>
> Before clicking **Create**, check the price summary on **Review + create**:
> a free size shouldn't show a monthly charge for the server itself.

---

## Step 3 — Connect to the server

On **Windows**, open **PowerShell** and run these, putting in your own file name
and IP address. The first line just makes Windows accept the key file.

```powershell
icacls "$HOME\Downloads\orchestrator_key.pem" /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i "$HOME\Downloads\orchestrator_key.pem" azureuser@YOUR.PUBLIC.IP
```

On a **Mac or Linux**, in **Terminal**:

```bash
chmod 600 ~/Downloads/orchestrator_key.pem
ssh -i ~/Downloads/orchestrator_key.pem azureuser@YOUR.PUBLIC.IP
```

Type `yes` if it asks whether to continue connecting. You're in when the prompt
starts with `azureuser@`.

---

## Step 4 — Set everything up

Paste this into the server window and press Enter:

```bash
sudo apt-get update -y && sudo apt-get install -y git && \
git clone https://github.com/AbhishekPoojary/Adaptive-Peer-To-Peer-GPU-Orchestration-For-Distributed-AI-Training.git orchestrator && \
cd orchestrator && bash deploy/cloud/setup.sh
```

It installs everything it needs. On this small server the first run takes
**15–30 minutes** — most of it building the website — so leave it running.
Near the end it asks you to choose an **admin username and password**; that's
your account. When it finishes it prints:

```
   Website (send this to everyone):   https://app.YOUR.PUBLIC.IP.sslip.io
   Machines join through:             https://api.YOUR.PUBLIC.IP.sslip.io
```

**These addresses are permanent.** You can close the server window now; the
system keeps running on its own, and starts again by itself if the server
restarts.

---

## Step 5 — Start using it

1. Open the **Website** link and sign in with the admin account.
2. Add the computers that will do the training — including your own laptop:
   click **Lend** at the top, and run the command it shows on that computer
   ([README, Part 2](../README.md#part-2--lend-your-computer)). Each computer
   runs it once; from then on it reconnects by itself. Friends do the same
   from their own accounts — no need to send them anything but the link.
3. Send the Website link to your friends. They click **Create one** to make an
   account ([README, Part 1](../README.md#part-1--train-a-model)).

---

## Later

**Updating to a newer version.** Connect as in step 3, then:

```bash
cd orchestrator && git pull && bash deploy/cloud/setup.sh
```

Your accounts, datasets, models and settings are kept. Computers that have
joined update themselves.

**Keeping an eye on the credit.** The **Education** page in the portal shows how
much is left. With the free B1s server only the public address uses credit, so
$100 lasts the year. In a year, renew the student offer from the same page.

**If it feels slow.** The free server has 1 GB of memory (setup adds 4 GB of
disk-backed swap to cope). For more room: open the VM → **Size** → choose
**B1ms** (2 GB) → **Resize**. Everything is kept. B1ms isn't free, though:
about $15 a month from your credit, which then lasts around six months.

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

- **When the credit or the year runs out,** Azure for Students switches the
  subscription off (and the server with it) unless you renew. Your data stays
  on the disk; renew and start the VM again.
- **Your data lives on this server.** Uploaded datasets and trained models are
  stored here. Each user can only see their own.
- **The address comes from the IP.** `sslip.io` turns the server's IP address
  into a web name for free, so HTTPS certificates work without buying a domain.
  Azure keeps the IP as long as the VM exists.

## If something goes wrong

**The website doesn't load.** Open the VM → **Networking** and check there are
inbound rules allowing ports **80** and **443**; add them with **Add inbound
port rule** if not. Then run the setup command again — it checks the website at
the end and says if it still can't reach it.

**"Permission denied (publickey)" when connecting.** Use the `.pem` file you
downloaded in step 2, and the user name `azureuser`.

**"Permissions for … are too open" (Windows).** Run the `icacls` line from step 3
again with your exact file name.

**The setup stopped with "Killed" or ran out of memory.** Run the setup command
again — the swap it added makes the second run go through. If it happens again,
resize to B1ms as above.

**Something else.** On the server, this shows what the orchestrator is doing:

```bash
cd orchestrator && sudo docker compose --env-file deploy/cloud/.env \
  -f deploy/compose.yaml -f deploy/cloud/compose.yaml logs --tail 50 orchestrator
```
