#!/usr/bin/env bash
# Starts Dovecot (IMAP), an SMTP sink and Radicale (CalDAV) for local integration tests.
# Needs root, `apt install dovecot-imapd`, and `pip install radicale aiosmtpd`.
set -euo pipefail
D=/tmp/icmcp-dev; HERE="$(cd "$(dirname "$0")" && pwd)"
rm -rf $D; mkdir -p $D/run $D/mail $D/collections
id vmail >/dev/null 2>&1 || useradd -r -M vmail
chown -R vmail:vmail $D/mail
echo "test:testpass" > $D/users
dovecot -c "$HERE/dovecot.conf" >/dev/null 2>&1 </dev/null
setsid nohup python "$HERE/smtp_sink.py" >$D/smtp.log 2>&1 </dev/null &
setsid nohup python -m radicale --config "$HERE/radicale.conf" >$D/radicale.log 2>&1 </dev/null &
sleep 3
echo "IMAP 127.0.0.1:1143 | SMTP 127.0.0.1:1025 | CalDAV http://127.0.0.1:5232  (user: test / pass: testpass)"
echo "Then: pytest tests/integration"
