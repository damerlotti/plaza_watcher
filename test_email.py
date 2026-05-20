import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

EMAIL_TO   = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")

if not (EMAIL_TO and EMAIL_FROM and EMAIL_PASS):
    raise SystemExit("Set EMAIL_TO, EMAIL_FROM, and EMAIL_PASS env vars first.")

recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]

msg = MIMEMultipart()
msg["Subject"] = "Plaza Watcher — test email"
msg["From"]    = EMAIL_FROM
msg["To"]      = ", ".join(recipients)
msg.attach(MIMEText(
    f"Test sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    f"Recipients: {', '.join(recipients)}",
    "plain"
))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(EMAIL_FROM, EMAIL_PASS)
    server.sendmail(EMAIL_FROM, recipients, msg.as_string())

print(f"Test email sent to: {', '.join(recipients)}")
