# Flask Email Sender

Web app to send personalized emails through Hostinger SMTP.

- Set SMTP login, From address, subject, and message
- Attach images, PDFs, and other files (images can be inserted into the message)
- Upload an Excel file of recipients
- Preview each person’s customized email
- Send in the background with live logs

Default sender: **Sarvesh Gadkari** &lt;founder@mulsetu.com&gt;. Subject and body start empty.

This is a **Python Flask** app. Do not use `npm start` or `npm run dev`.

## Run locally

```powershell
cd "C:\Users\Lenovo\Desktop\Hostinger flask app"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000/

On first run the app creates `config.json` from defaults (or copy `config.example.json` to `config.json`). Enter the Hostinger password for `founder@mulsetu.com` in the web UI. Keep **From** the same as **Login**.

## Excel file

Required column: `email`

Optional columns for personalized mail: `name`, `organization`, `custom_message`, or any other heading.

In the subject or body, insert fields such as `{{first_name}}`, `{{name}}`, `{{organization}}`, `{{custom_message}}`. Each row gets its own values.

## Before you push to GitHub

Do **not** commit:

- `config.json` (can contain your SMTP password)
- `send.log`
- files in `uploads/` and `attachments/`
- `.venv/` or `__pycache__/`

Those paths are listed in `.gitignore`. Commit `config.example.json` instead of your real config.

## Notes

- Delay between sends should stay at 60 seconds or more (Hostinger limits).
- Send a test email first and check Inbox and Spam.
- SPF, DKIM, and DMARC on your domain also affect whether mail lands in spam.
