# Photobook

A private, ad-free photo book of the photographers you follow. Once a day it visits their websites, collects their latest photos and lays them out page by page.

You need a free GitHub account. Everything is done in the web browser, and it takes about 10 minutes. A computer is easier than a phone for setup.

## Setup (one time)

**1. Make the repository**
- Sign in at github.com, then click **+** (top right) → **New repository**.
- Name it `photobook`, choose **Public**, then click **Create repository**.

**2. Upload the files**
- On the new page, click **uploading an existing file**.
- Drag in these 5 files: `index.html`, `fetch.py`, `photographers.yml`, `images.json`, `README.md`.
- Click **Commit changes**.

**3. Add the daily updater**
- Click **Add file** → **Create new file**.
- In the name box, type exactly: `.github/workflows/update.yml` (the slashes create the folders).
- Open `update-workflow.yml` from the download, copy all of it, and paste it into the big box.
- Click **Commit changes**.

**4. Turn on the website**
- Go to **Settings** → **Pages**.
- Under "Branch", choose **main** and **/ (root)**, then click **Save**.

**5. Run it the first time**
- Go to the **Actions** tab → **Update photobook** → **Run workflow** → **Run workflow**.
- Wait about 2 minutes for the green tick.

Your book will be at **https://YOUR-USERNAME.github.io/photobook/**. Open it on your phone and use "Add to Home Screen" so it works like an app.

## Adding photographers

Open `photographers.yml` on GitHub and click the pencil icon. The **Add or edit photographers** link at the bottom of your book goes straight there. Copy one block, change the name, site and lists, then click **Commit changes**. The book updates itself in a few minutes.

## If something goes wrong

- **A photographer shows "visit site ↗" with no pages:** their site didn't give up its photos automatically. Try their portfolio or gallery page as the `site` instead of the home page.
- **The Actions run shows a red ✗:** click it to see which step failed, and paste what it says to Claude.
- **Nothing changes after editing the list:** give it 5 minutes, then refresh the page.

The fetcher stores only links to the photos and follows each site's robots.txt rules. No one's photos are copied.
