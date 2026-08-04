# Employee Scheduler

A small web app for entering employee work schedules and sending each person
their upcoming shifts by **email** and/or **text message (SMS)**.

- **Web UI** (Flask) — add employees, enter shifts, and hit *Send*.
- **SQLite** storage — a single `schedule.db` file, no server to run.
- **Email** via SMTP (Gmail, Outlook, or any provider).
- **SMS** via [Twilio](https://www.twilio.com/).

It lives in this folder and is completely independent of the UniFi/Hamina
export tooling in the repository root.

## Layout at a glance

```
Dashboard  → status of email/SMS, upcoming shifts, "Send to everyone"
Employees  → add / edit / remove people (name, role, email, phone)
Schedule   → add / remove shifts, and send an individual their schedule
```

## Quick start

```bash
cd schedule_app
python3 -m pip install -r requirements.txt

# Configure delivery + secret key (see below). For a first look you can skip
# this — the app runs fine, sending is just disabled until configured.
cp .env.example .env        # then edit .env
set -a; source .env; set +a

python3 app.py
```

Open <http://127.0.0.1:5000> in your browser.

1. Go to **Employees** and add a few people (include an email and/or phone).
2. Go to **Schedule** and add shifts (employee, date, start, end, location).
3. From the **Dashboard**, click **Send to everyone**, or send one person
   from the Schedule page. Choose Email, SMS, or both.

Only upcoming shifts (today onward) are included in what gets sent.

## Configuration

All configuration is via environment variables (see `.env.example`). Nothing
is hard-coded and no secrets are committed — `.env` and `*.db` are
git-ignored.

### Email (SMTP)

| Variable        | Meaning                                    | Example              |
|-----------------|--------------------------------------------|----------------------|
| `SMTP_HOST`     | SMTP server host                           | `smtp.gmail.com`     |
| `SMTP_PORT`     | SMTP port (587 for STARTTLS)               | `587`                |
| `SMTP_USER`     | Login username                             | `you@gmail.com`      |
| `SMTP_PASSWORD` | Login password / app password              | `abcd efgh ijkl mnop`|
| `SMTP_FROM`     | Optional "From" address (defaults to USER) | `you@gmail.com`      |
| `SMTP_USE_SSL`  | `true` for implicit SSL (port 465)         | `false`              |

For **Gmail**, turn on 2-factor auth and create an
[App password](https://support.google.com/accounts/answer/185833); use that
as `SMTP_PASSWORD`.

### SMS (Twilio)

| Variable              | Meaning                            | Example                |
|-----------------------|------------------------------------|------------------------|
| `TWILIO_ACCOUNT_SID`  | Account SID from the Twilio console| `ACxxxxxxxx…`          |
| `TWILIO_AUTH_TOKEN`   | Auth token from the Twilio console | `your-auth-token`      |
| `TWILIO_FROM_NUMBER`  | A Twilio number you own (E.164)    | `+15551234567`         |

Recipient phone numbers should be in **E.164** format (e.g. `+15551230000`).

### App

| Variable       | Meaning                                        | Default        |
|----------------|------------------------------------------------|----------------|
| `SECRET_KEY`   | Signs flash/session cookies — set a random one | `dev-change-me`|
| `SCHEDULE_DB`  | Path to the SQLite file                        | `./schedule.db`|
| `PORT`         | Web server port                                | `5000`         |

The **Dashboard** shows a green pill for each channel that is fully
configured and a red one for anything still missing, so you can tell at a
glance whether sending will work.

## How it's organized

```
schedule_app/
├── app.py            # Flask routes (dashboard, employees, schedule, send)
├── database.py       # SQLite layer: employees + shifts
├── notifications.py  # Email (smtplib) + SMS (Twilio), message formatting
├── templates/        # Jinja2 HTML (base, dashboard, employees, schedule)
├── static/style.css  # Styling (light + dark)
├── requirements.txt
└── .env.example
```

## Notes & next steps

- Sends happen synchronously when you click *Send*. For a large team you
  might later move sending to a background task/queue.
- To send schedules automatically (e.g. every Sunday evening), you could run
  a scheduled job that calls the same helpers in `notifications.py`.
- The message wording lives in `notifications.py`
  (`build_schedule_text` / `build_schedule_html`) — edit there to change
  what employees receive.
