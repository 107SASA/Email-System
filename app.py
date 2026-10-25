import os
import json
import ssl
import re
import html as html_lib
import time
import threading
import mimetypes
import pandas as pd
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_from_directory,
    flash,
    jsonify,
)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ATTACH_DIR = os.path.join(BASE_DIR, "attachments")
LOG_PATH = os.path.join(BASE_DIR, "send.log")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ATTACH_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "please-change-this")

PRIMARY_EMAIL = "founder@mulsetu.com"
PRIMARY_NAME = "Sarvesh Gadkari"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
ATTACH_EXTS = IMAGE_EXTS | {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
    ".txt",
    ".zip",
    ".ppt",
    ".pptx",
}
MAX_ATTACH_BYTES = 10 * 1024 * 1024

DEFAULT_CONFIG = {
    "SMTP_HOST": "smtp.hostinger.com",
    "SMTP_PORT": 465,
    "EMAIL_USER": PRIMARY_EMAIL,
    "EMAIL_PASS": "",
    "FROM_EMAIL": PRIMARY_EMAIL,
    "SENDER_NAME": PRIMARY_NAME,
    "REPLY_TO": PRIMARY_EMAIL,
    "DELAY_SECONDS": 60,
    "SUBJECT": "",
    "EMAIL_BODY": "",
    "SEND_AS_HTML": True,
    "ATTACHMENTS": [],
}

HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")
URL_RE = re.compile(r"(https?://[^\s<]+)|(www\.[^\s<]+)", re.IGNORECASE)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
NAME_COLUMNS = ("name", "mentor", "mentor name", "full name", "fullname", "first name", "firstname")
NOTE_COLUMNS = ("custom_message", "personal_note", "custom_body")

send_thread = None
send_lock = threading.Lock()
is_sending = False


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for key, value in DEFAULT_CONFIG.items():
            cfg.setdefault(key, value)
        return cfg
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def append_log(line):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")


def unique_filename(directory, filename):
    filename = secure_filename(filename) or "file"
    base, ext = os.path.splitext(filename)
    candidate = filename
    index = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base}_{index}{ext}"
        index += 1
    return candidate


def listed_attachments(cfg):
    items = []
    for name in cfg.get("ATTACHMENTS") or []:
        path = os.path.join(ATTACH_DIR, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        items.append(
            {
                "name": name,
                "is_image": ext in IMAGE_EXTS,
                "size_kb": max(1, round(os.path.getsize(path) / 1024)),
            }
        )
    return items


def save_attachments(cfg):
    names = [n for n in (cfg.get("ATTACHMENTS") or []) if os.path.isfile(os.path.join(ATTACH_DIR, n))]
    remove_name = request.form.get("remove_attachment")
    if remove_name:
        safe = secure_filename(remove_name)
        if safe in names:
            names.remove(safe)
        path = os.path.join(ATTACH_DIR, safe)
        if os.path.isfile(path):
            os.remove(path)

    for upload in request.files.getlist("attachments"):
        if not upload or not upload.filename:
            continue
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in ATTACH_EXTS:
            flash(f"Skipped unsupported file type: {upload.filename}", "warning")
            continue
        upload.stream.seek(0, os.SEEK_END)
        size = upload.stream.tell()
        upload.stream.seek(0)
        if size > MAX_ATTACH_BYTES:
            flash(f"{upload.filename} is over 10 MB and was skipped.", "warning")
            continue
        filename = unique_filename(ATTACH_DIR, upload.filename)
        upload.save(os.path.join(ATTACH_DIR, filename))
        names.append(filename)
        flash(f"Added file: {filename}", "success")

    cfg["ATTACHMENTS"] = names
    return cfg


def rewrite_inline_images(html, names):
    html = html or ""
    for name in names:
        html = html.replace(f"/attachments/{name}", f"cid:{name}")
        html = html.replace(f"attachments/{name}", f"cid:{name}")
    return html


def add_attachments_to_message(msg, cfg, html_body):
    names = cfg.get("ATTACHMENTS") or []
    html_body = html_body or ""
    html_part = None
    if msg.is_multipart():
        payload = msg.get_payload()
        if payload:
            html_part = payload[-1]

    for name in names:
        path = os.path.join(ATTACH_DIR, name)
        if not os.path.isfile(path):
            continue
        ctype, _encoding = mimetypes.guess_type(path)
        if not ctype:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(path, "rb") as handle:
            data = handle.read()
        ext = os.path.splitext(name)[1].lower()
        inline = ext in IMAGE_EXTS and html_part is not None and f"cid:{name}" in html_body
        if inline:
            html_part.add_related(data, maintype, subtype, cid=name, filename=name)
        else:
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)


def domain_of(address):
    addr = parseaddr(address or "")[1].strip().lower()
    return addr.split("@")[-1] if "@" in addr else ""


def looks_like_html(text):
    return bool(HTML_TAG_RE.search(text or ""))


def html_to_text(value):
    text = value or ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"(?i)</h[1-6]>", "\n\n", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = HTML_TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def linkify(escaped_text):
    def repl(match):
        url = match.group(0)
        href = url if url.lower().startswith("http") else f"https://{url}"
        href = html_lib.escape(href, quote=True)
        label = html_lib.escape(url)
        return f'<a href="{href}" style="color:#1a56db;">{label}</a>'

    return URL_RE.sub(repl, escaped_text)


def text_to_html(plain):
    escaped = html_lib.escape(plain or "")
    escaped = linkify(escaped).replace("\n", "<br>\n")
    return escaped


def wrap_html_email(inner_html, sender_name):
    if re.search(r"(?i)<html", inner_html or ""):
        return inner_html
        safe_name = html_lib.escape(sender_name or PRIMARY_NAME)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_name}</title>
</head>
<body style="margin:0;padding:0;background:#f4f5f7;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f5f7;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;">
          <tr>
            <td style="padding:20px 24px;border-bottom:1px solid #e5e7eb;font-family:Arial,Helvetica,sans-serif;font-size:16px;color:#111827;">
              <strong>{safe_name}</strong>
            </td>
          </tr>
          <tr>
            <td style="padding:24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:#1f2937;">
              {inner_html}
            </td>
          </tr>
          <tr>
            <td style="padding:16px 24px;border-top:1px solid #e5e7eb;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.5;color:#6b7280;">
              This message was sent by {safe_name}.
              If you were not expecting it, you can ignore this email or reply to unsubscribe.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def body_for_editor(body):
    body = body or ""
    if looks_like_html(body):
        return body
    return html_lib.escape(body).replace("\n", "<br>\n")


def find_column(columns, needle_fn):
    for col in columns:
        if needle_fn(str(col).lower()):
            return col
    return None


def find_email_column(columns):
    return find_column(columns, lambda c: "email" in c) or (columns[0] if len(columns) else None)


def find_name_column(columns):
    return find_column(columns, lambda c: c.strip() in NAME_COLUMNS)


def cell_text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in ("nan", "none", "nat"):
        return ""
    return text


def token_key(column):
    return re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")


def values_from_parts(email, name=""):
    name = cell_text(name)
    first = name.split()[0] if name else "Mentor"
    return {
        "email": email or "",
        "name": name or "Mentor",
        "first_name": first,
        "full_name": name or "Mentor",
    }


def row_values(row, email_col):
    values = {}
    for col in row.index:
        key = token_key(col)
        if key:
            values[key] = cell_text(row.get(col))
    email = cell_text(row.get(email_col)) if email_col is not None else ""
    name = ""
    for key in ("name", "mentor", "mentor_name", "full_name", "fullname", "first_name", "firstname"):
        if values.get(key):
            name = values[key]
            break
    merged = values_from_parts(email, name)
    merged.update({k: v for k, v in values.items() if v})
    merged["email"] = email or merged.get("email", "")
    if name:
        merged["name"] = name
        merged["full_name"] = name
        merged["first_name"] = name.split()[0]
    return merged


def personalize_text(text, values):
    html_mode = looks_like_html(text)

    def repl(match):
        key = match.group(1).lower()
        val = values.get(key, "")
        return html_lib.escape(val) if html_mode else val

    text = TOKEN_RE.sub(repl, text or "")
    name = values.get("name") or ""
    if name and name != "Mentor":
        greeting = html_lib.escape(values.get("first_name") or name) if html_mode else (values.get("first_name") or name)
        text = re.sub(r"Dear Mentor\b", f"Dear {greeting}", text, count=1, flags=re.IGNORECASE)
    return text


def personalize(text, recipient_email, recipient_name="", row=None, email_col=None):
    if row is not None:
        values = row_values(row, email_col)
    else:
        values = values_from_parts(recipient_email, recipient_name)
    return personalize_text(text, values)


def body_for_recipient(template_body, values):
    body = template_body or ""
    note = ""
    for key in NOTE_COLUMNS:
        if values.get(key):
            note = values[key]
            break
    used_tokens = {key.lower() for key in TOKEN_RE.findall(body)}
    if note and not (used_tokens & set(NOTE_COLUMNS)):
        if looks_like_html(body):
            body = body.rstrip() + f"<p>{html_lib.escape(note)}</p>"
        else:
            body = body.rstrip() + "\n\n" + note
    return personalize_text(body, values)


def resolve_from_email(cfg):
    login = (cfg.get("EMAIL_USER") or "").strip()
    from_email = (cfg.get("FROM_EMAIL") or "").strip()
    if not from_email or "yourdomain.com" in from_email.lower():
        return login
    return from_email


def resolve_reply_to(cfg, from_email):
    reply_to = (cfg.get("REPLY_TO") or "").strip()
    if reply_to and EMAIL_RE.match(reply_to):
        return reply_to
    return from_email


def build_email_parts(cfg, body):
    send_as_html = cfg.get("SEND_AS_HTML", True)
    if looks_like_html(body):
        inner_html = body
        plain = html_to_text(body)
    else:
        plain = body or ""
        inner_html = text_to_html(plain)
    if not send_as_html:
        inner_html = text_to_html(plain)
    html_body = wrap_html_email(inner_html, cfg.get("SENDER_NAME"))
    return plain, html_body


def assess_deliverability(cfg):
    warnings = []
    login = (cfg.get("EMAIL_USER") or "").strip()
    from_email = resolve_from_email(cfg)
    subject = cfg.get("SUBJECT") or ""
    body = html_to_text(cfg.get("EMAIL_BODY") or "")
    delay = int(cfg.get("DELAY_SECONDS") or 0)

    if not cfg.get("EMAIL_PASS"):
        warnings.append(
            {
                "level": "danger",
                "text": "SMTP password is empty. Emails cannot be sent until you save the Hostinger mailbox password.",
            }
        )
    if "yourdomain.com" in login.lower() or not EMAIL_RE.match(login):
        warnings.append(
            {
                "level": "danger",
                "text": f"Login email is still a placeholder. Use your real Hostinger mailbox, for example {PRIMARY_EMAIL}.",
            }
        )
    if from_email.lower() != login.lower():
        warnings.append(
            {
                "level": "danger",
                "text": f"From address ({from_email}) does not match the SMTP login ({login}). Gmail and Outlook often put this in spam. Use the same mailbox for both.",
            }
        )
    if re.search(r"\b(verify your (account|status)|account verification|urgent|password)\b", subject + " " + body, re.I):
        warnings.append(
            {
                "level": "warning",
                "text": "Subject/body sounds like a security verification email. That language often lands in spam. Keep it as a clear, ordinary message.",
            }
        )
    if "chat.whatsapp.com" in body.lower():
        warnings.append(
            {
                "level": "warning",
                "text": "WhatsApp invite links are frequently flagged as spam. Keep it optional, below the main steps, with the full URL visible.",
            }
        )
    if delay < 45:
        warnings.append(
            {
                "level": "warning",
                "text": "Delay is under 45 seconds. Hostinger may throttle bulk mail and providers may mark fast bursts as spam. Keep at least 60 seconds.",
            }
        )
    if not (subject or "").strip():
        warnings.append(
            {
                "level": "warning",
                "text": "Subject is empty. Add a short, clear subject before sending.",
            }
        )
    return warnings


def send_one_email(config, recipient_email, subject, plain_text, html_body=None):
    login = (config.get("EMAIL_USER") or "").strip()
    from_email = resolve_from_email(config)
    sender_name = config.get("SENDER_NAME") or ""
    reply_to = resolve_reply_to(config, from_email)
    from_domain = domain_of(from_email) or "localhost"

    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, from_email))
    msg["To"] = recipient_email
    msg["Subject"] = (subject or "").replace("\n", " ").strip()
    msg["Reply-To"] = reply_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_domain)
    msg["MIME-Version"] = "1.0"
    msg["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if sender_name:
        msg["Organization"] = sender_name

    names = config.get("ATTACHMENTS") or []
    html_body = rewrite_inline_images(html_body, names)
    plain = (plain_text or "").strip() or html_to_text(html_body or "") or " "
    msg.set_content(plain, charset="utf-8")
    if html_body:
        msg.add_alternative(html_body, subtype="html", charset="utf-8")
    add_attachments_to_message(msg, config, html_body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        config.get("SMTP_HOST"), int(config.get("SMTP_PORT", 465)), context=context, timeout=25
    ) as server:
        server.login(login, config.get("EMAIL_PASS"))
        server.send_message(msg, from_addr=login, to_addrs=[recipient_email])


def send_bulk_background(cfg, excel_path, subject, body, delay, send_as_html=False):
    global is_sending
    with send_lock:
        is_sending = True

    try:
        df = pd.read_excel(excel_path)
        email_col = find_email_column(df.columns)
        total = len(df)
        sent = 0
        cfg = dict(cfg)
        cfg["SEND_AS_HTML"] = send_as_html
        append_log(f"Starting send: {total} recipients, delay={delay}s, from={resolve_from_email(cfg)}")

        for position, (index, row) in enumerate(df.iterrows()):
            values = row_values(row, email_col)
            email = values.get("email", "")
            if not email or "@" not in email:
                append_log(f"Skipping invalid email at row {index + 2}: {email}")
            else:
                personalized = body_for_recipient(body, values)
                personalized_subject = personalize_text(subject, values)
                plain, html_body = build_email_parts(cfg, personalized)
                try:
                    send_one_email(cfg, email, personalized_subject, plain, html_body)
                    sent += 1
                    append_log(f"Sent ({sent}/{total}) to: {email} ({values.get('name') or 'no name'})")
                except Exception as e:
                    append_log(f"Failed for {email} — {e}")

            if position < total - 1:
                time.sleep(max(1.0, float(delay)))

        append_log(f"Completed sending. Sent: {sent}/{total}")
    except Exception as e:
        append_log(f"Send job stopped: {e}")
    finally:
        with send_lock:
            is_sending = False


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/attachments/<path:filename>")
def attachment_file(filename):
    return send_from_directory(ATTACH_DIR, filename)


def apply_form_config(cfg):
    cfg["SMTP_HOST"] = request.form.get("smtp_host", cfg.get("SMTP_HOST"))
    cfg["SMTP_PORT"] = int(request.form.get("smtp_port", cfg.get("SMTP_PORT")))
    cfg["EMAIL_USER"] = request.form.get("email_user", cfg.get("EMAIL_USER"))
    password = request.form.get("email_pass")
    if password:
        cfg["EMAIL_PASS"] = password
    cfg["FROM_EMAIL"] = request.form.get("from_email", cfg.get("FROM_EMAIL"))
    cfg["SENDER_NAME"] = request.form.get("sender_name", cfg.get("SENDER_NAME"))
    cfg["REPLY_TO"] = request.form.get("reply_to", cfg.get("REPLY_TO", ""))
    cfg["DELAY_SECONDS"] = int(request.form.get("delay_seconds", cfg.get("DELAY_SECONDS")))
    cfg["SUBJECT"] = request.form.get("subject", cfg.get("SUBJECT"))
    cfg["EMAIL_BODY"] = request.form.get("email_body", cfg.get("EMAIL_BODY"))
    cfg["SEND_AS_HTML"] = True if request.form.get("send_as_html") == "on" else False
    cfg = save_attachments(cfg)
    save_config(cfg)
    return cfg


def save_upload():
    excel_file = request.files.get("excel_file")
    if excel_file and excel_file.filename:
        filename = secure_filename(excel_file.filename)
        uploaded_path = os.path.join(UPLOAD_DIR, filename)
        excel_file.save(uploaded_path)
        return uploaded_path
    return None


@app.route("/", methods=["GET", "POST"])
def index():
    cfg = load_config()

    if request.method == "POST":
        action = request.form.get("action")
        cfg = apply_form_config(cfg)
        uploaded_path = save_upload()

        if request.form.get("remove_attachment") and action not in ("test", "preview", "send"):
            flash("Attachment removed.", "success")
            return redirect(url_for("index"))

        if action == "save":
            flash("Settings saved.", "success")
            return redirect(url_for("index"))

        if action == "test":
            if not cfg.get("EMAIL_PASS"):
                flash("Save your SMTP password first, then send a test email.", "warning")
                return redirect(url_for("index"))
            target = (cfg.get("EMAIL_USER") or "").strip()
            if not EMAIL_RE.match(target):
                flash("Enter a real login email before sending a test.", "warning")
                return redirect(url_for("index"))
            try:
                sample = values_from_parts(target, "Mentor")
                plain, html_body = build_email_parts(cfg, body_for_recipient(cfg.get("EMAIL_BODY"), sample))
                send_one_email(cfg, target, personalize_text(cfg.get("SUBJECT"), sample), plain, html_body)
                append_log(f"Test email sent to {target}")
                flash(
                    f"Test email sent to {target}. Open Inbox and Spam/Junk and mark it as Not spam if needed.",
                    "success",
                )
            except Exception as e:
                append_log(f"Test email failed: {e}")
                flash(f"Test email failed: {e}", "danger")
            return redirect(url_for("index"))

        if action == "preview":
            if not uploaded_path:
                flash("Please upload an Excel file to preview.", "warning")
                return redirect(url_for("index"))
            try:
                df = pd.read_excel(uploaded_path)
            except Exception as e:
                flash(f"Failed to read Excel: {e}", "danger")
                return redirect(url_for("index"))

            email_col = find_email_column(df.columns)
            merge_fields = sorted({token_key(c) for c in df.columns if token_key(c)} | {"name", "first_name", "email"})
            people = []
            for _, row in df.head(100).iterrows():
                values = row_values(row, email_col)
                preview_body = body_for_recipient(cfg.get("EMAIL_BODY", ""), values)
                preview_subject = personalize_text(cfg.get("SUBJECT", ""), values)
                plain, html_body = build_email_parts(cfg, preview_body)
                people.append(
                    {
                        "email": values.get("email", ""),
                        "name": values.get("name", ""),
                        "subject": preview_subject,
                        "html": html_body,
                        "plain": plain,
                    }
                )
            first = people[0] if people else {
                "email": "",
                "name": "",
                "subject": cfg.get("SUBJECT", ""),
                "html": "",
                "plain": "",
            }
            return render_template(
                "preview.html",
                config=cfg,
                preview=people,
                filename=os.path.basename(uploaded_path),
                is_html=True,
                html_body=first["html"],
                plain_body=first["plain"],
                from_email=resolve_from_email(cfg),
                deliverability=assess_deliverability(cfg),
                merge_fields=merge_fields,
                attachments=listed_attachments(cfg),
            )

        if action == "send":
            if not uploaded_path:
                flash("Please upload an Excel file to send.", "warning")
                return redirect(url_for("index"))
            if not cfg.get("EMAIL_PASS"):
                flash("Save your SMTP password before sending.", "warning")
                return redirect(url_for("index"))
            global send_thread
            if send_thread and send_thread.is_alive():
                flash("A sending job is already running.", "warning")
                return redirect(url_for("status"))

            risks = [w for w in assess_deliverability(cfg) if w["level"] == "danger"]
            if risks:
                flash(risks[0]["text"], "danger")
                return redirect(url_for("index"))

            send_thread = threading.Thread(
                target=send_bulk_background,
                args=(
                    cfg,
                    uploaded_path,
                    cfg.get("SUBJECT"),
                    cfg.get("EMAIL_BODY"),
                    cfg.get("DELAY_SECONDS"),
                    cfg.get("SEND_AS_HTML", True),
                ),
                daemon=True,
            )
            send_thread.start()
            flash("Sending started in background. View status page.", "success")
            return redirect(url_for("status"))

    return render_template(
        "index.html",
        config=cfg,
        editor_body=body_for_editor(cfg.get("EMAIL_BODY", "")),
        deliverability=assess_deliverability(cfg),
        attachments=listed_attachments(cfg),
    )


@app.route("/status")
def status():
    cfg = load_config()
    logs = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            logs = f.read().splitlines()[-200:]
    return render_template("status.html", sending=is_sending, logs=logs, config=cfg)


@app.route("/api/logs")
def api_logs():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return jsonify(lines=f.read().splitlines()[-500:])
    return jsonify(lines=[])


if __name__ == "__main__":
    app.run(debug=True)
