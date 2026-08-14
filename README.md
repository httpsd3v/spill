# 💧 Spill

**Post it. Let people spill their minds about it.**

Spill is a full-stack social networking web app built for **text + photo conversations** — memes, screenshots, hot takes and everyday moments. No videos. Ever. Just fast, lively discussions.

🔗 **Live:** https://spill-ujdk.onrender.com

---

## ✨ Features

### Posts & Feeds
- 📝 Text + up to **4 photos** per post (JPEG / PNG / WebP / GIF) with drag-and-drop uploads & alt-text
- ⚠️ Content warnings & 🔒 followers-only visibility
- ❤️ Likes · 🔁 Reposts · ❝ Quote posts · 🔖 Bookmarks · 🔗 Share
- 💬 Nested comments with **Author badge**, sorting & replies
- 🎲 **For You** — shuffled discovery feed, no repeats, ends with *"You're all caught up"*
- 🕒 **Following** & **Latest** chronological feeds with infinite scroll
- #️⃣ Hashtags, @mentions & live trending topics

### People
- 🔐 Email + password auth (Supabase Auth) — passwords never touch our servers
- 👤 Profiles: cover photo, avatar, bio, stats, Posts / Media / Likes tabs
- 🕵️ **Private accounts** with Accept / Deny follow requests
- 🚫 Block & 🚩 Report systems

### Messages & Notifications
- ✉️ Realtime 1-on-1 DMs with photo sharing
- 🔔 Grouped notifications (*"Alex and 3 others liked your post"*)
- 📢 **Push notifications with sound** on desktop & mobile
- 🔴 Unread badges on nav + bottom bar

### The "No Video" Rule 🚫🎥
Videos are rejected **server-side** using magic-byte sniffing — MP4/MOV/AVI/WebM/MKV files (even renamed to `.jpg`) are blocked before they ever reach storage.

---

## 🛠️ Tech Stack

| Layer     | Technology |
|-----------|------------|
| Backend   | Flask (Python) + Gunicorn |
| Database  | Supabase Postgres (Row-Level Security enabled) |
| Auth      | Supabase Auth (JWT verified server-side) |
| Storage   | Supabase Storage (`spill-photos` bucket) |
| Realtime  | Supabase Realtime (`postgres_changes`) |
| Frontend  | Vanilla JS single-page app — no frameworks |
| Hosting   | Render |

---

## 📁 Project Structure

```
spill/
├── app.py               # Flask API · schema · RLS policies · storage · JWT verification
├── requirements.txt
├── .python-version
├── templates/
│   └── index.html       # Entire frontend SPA (JS included)
└── static/
    └── style.css        # Theme-aware design system (dark / light)
```

---

## 🚀 Run It Yourself

### 1. Supabase setup
1. Create a project at [supabase.com](https://supabase.com)
2. **Storage** → create a **public** bucket named `spill-photos`
3. *(Optional)* **Authentication → Providers → Email** → turn off *"Confirm email"* for instant signups
4. Copy: Project URL, anon key, service_role key, JWT secret, and the **Pooler (Session)** DB connection string

### 2. Configure
- Paste `SUPABASE_URL` + anon key at the top of the script in `templates/index.html`
- Set environment variables:

```bash
SUPABASE_URL=https://YOUR_REF.supabase.co
SUPABASE_DB_URL=postgresql://postgres.YOUR_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres
SUPABASE_SERVICE_KEY=your_service_role_key
SUPABASE_JWT_SECRET=your_jwt_secret
```

### 3. Run locally
```bash
pip install -r requirements.txt
python app.py        # → http://localhost:5000
```
> Tables, RLS policies and the realtime publication are **created automatically** on first boot.

### 4. Deploy to Render
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
- Add the 4 env vars → Deploy → 🎉 live.

---

## 🔒 Security Notes
- All visibility rules (private accounts, followers-only posts, blocks) are enforced **in SQL**, not just the UI
- Realtime events are scoped per-user via RLS policies
- The `service_role` key lives only in server environment variables

---

## 🗺️ Roadmap
- [ ] Communities & topic spaces
- [ ] Moderation dashboard
- [ ] PWA install + full background web push
- [ ] Server-side image resizing / AVIF delivery

---

Built with 💧 by **[@httpsd3v](https://github.com/httpsd3v)**
