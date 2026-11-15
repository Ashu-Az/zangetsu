#!/bin/bash
# Send every .eml in a folder through Gmail web (Ashu's Gmail, signed in inside state/freelance-profile).
# Ashu's mail rules: sent from ashuazadblk@gmail.com, never only drafted; tailored resume attached; no links in the body;
# daily cap OUTREACH_DAILY_CAP (default 25); 45-90 s between sends; an address already in state/outreach_sent.json
# is never mailed twice.
# Usage: tools/send_outreach.sh output/outreach/2026-11-14
cd "$(dirname "$0")/.." || exit 1
DIR="$1"; CAP="${OUTREACH_DAILY_CAP:-25}"
[ -d "$DIR" ] || { echo "no folder $DIR"; exit 0; }
ls "$DIR"/*.eml >/dev/null 2>&1 || { echo "no drafts in $DIR"; exit 0; }
started=""
if ! pgrep -f "tools/drive.py start" >/dev/null; then
  nohup .venv/bin/python tools/drive.py start state/freelance-profile > output/debug/drive.log 2>&1 &
  started=1; sleep 10
fi
for e in "$DIR"/*.eml; do
  sent_today=$(.venv/bin/python -c "
import json,time; g=json.load(open('state/outreach_sent.json'))
print(sum(1 for v in g.values() if str(v.get('at','')).startswith(time.strftime('%Y-%m-%d'))))")
  [ "$sent_today" -ge "$CAP" ] && { echo "daily cap $CAP reached"; break; }
  res=$(.venv/bin/python tools/drive.py run "
import sys, re; sys.path.insert(0,'.'); from pathlib import Path
from tools.gmail_web_send import load, send
gp=Path('state/outreach_sent.json'); g=json.loads(gp.read_text())
msg=load(Path('$e')); msg['subject']=' '.join(msg['subject'].split())
if msg['to'].lower() in g: out='skip dup '+msg['to']
elif re.search(r'https?://|www\.', msg['body']): out='skip (link in body) '+msg['to']
elif not msg.get('att'): out='skip (no resume attached) '+msg['to']
else:
    pg.goto('https://mail.google.com/mail/u/0/#inbox'); pg.wait_for_timeout(4000)
    ok=send(pg,msg,False)
    if ok:
        g=json.loads(gp.read_text())
        g[msg['to'].lower()]={'at':time.strftime('%Y-%m-%dT%H:%M:%S'),'title':msg['subject'],'company':'','source':'naukri JD email (Gmail web)'}
        gp.write_text(json.dumps(g,indent=1))
    out=time.strftime('%H:%M:%S')+(' SENT ' if ok else ' NOT CONFIRMED ')+msg['to']
" 2>&1 | tail -1)
  echo "$res"
  case "$res" in *SENT*|*CONFIRMED*) sleep $((45 + RANDOM % 46));; esac
done
[ -n "$started" ] && pkill -f "tools/drive.py start"
echo "send_outreach done"
