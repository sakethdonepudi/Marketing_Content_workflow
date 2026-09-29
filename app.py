import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
DB = Path(os.environ.get('REACHOUT_DB', ROOT / 'reachout.sqlite3'))
STATES = ('DETECTED','VERIFYING','VERIFIED','CONTENT_PLANNED','SCRIPTED','RENDERING','QA','READY_TO_PUBLISH','PUBLISHED','MEASURED','HOLD','REJECTED')
TRANSITIONS = {
 'DETECTED': {'VERIFYING','HOLD','REJECTED'}, 'VERIFYING': {'VERIFIED','HOLD','REJECTED'},
 'VERIFIED': {'CONTENT_PLANNED','HOLD'}, 'CONTENT_PLANNED': {'SCRIPTED','HOLD'},
 'SCRIPTED': {'RENDERING','HOLD'}, 'RENDERING': {'QA','HOLD'},
 'QA': {'READY_TO_PUBLISH','SCRIPTED','HOLD'}, 'READY_TO_PUBLISH': {'PUBLISHED','HOLD'},
 'PUBLISHED': {'MEASURED'}, 'MEASURED': set(), 'HOLD': {'VERIFYING','REJECTED'}, 'REJECTED': set()
}
def now(): return datetime.now(timezone.utc).isoformat()
def connect():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c
def init():
 with connect() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,title TEXT NOT NULL,source TEXT NOT NULL,source_url TEXT,status TEXT NOT NULL,priority TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS transitions(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL REFERENCES events(id),from_state TEXT,to_state TEXT NOT NULL,at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS metrics(media_id TEXT PRIMARY KEY,views INTEGER,reach INTEGER,likes INTEGER,comments INTEGER,shares INTEGER,saves INTEGER,measured_at TEXT NOT NULL);''')
def create_event(title,source,source_url='',priority='NORMAL'):
 if not title.strip() or not source.strip(): raise ValueError('title and source required')
 if priority not in ('BREAKING','HIGH','NORMAL'): raise ValueError('invalid priority')
 eid='EV-'+uuid.uuid4().hex[:10].upper(); t=now()
 with connect() as c:
  c.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(eid,title.strip(),source.strip(),source_url,'DETECTED',priority,t,t))
  c.execute('INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,?,?,?)',(eid,None,'DETECTED',t))
 return eid
def transition(eid,state):
 if state not in STATES: raise ValueError('invalid state')
 with connect() as c:
  row=c.execute('SELECT status FROM events WHERE id=?',(eid,)).fetchone()
  if row is None: raise KeyError(eid)
  old=row['status']
  if state not in TRANSITIONS[old]: raise ValueError(f'{old} cannot transition to {state}')
  t=now(); c.execute('UPDATE events SET status=?,updated_at=? WHERE id=?',(state,t,eid))
  c.execute('INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,?,?,?)',(eid,old,state,t))
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT/'static'),**kwargs)
 def send_json(self,obj,status=200):
  data=json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(data)
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/api/overview':
   with connect() as c:
    events=[dict(x) for x in c.execute('SELECT * FROM events ORDER BY created_at DESC LIMIT 50')]
    metrics=[dict(x) for x in c.execute('SELECT * FROM metrics ORDER BY measured_at DESC')]
   self.send_json({'events':events,'metrics':metrics,'connected':False,'updated_at':now()}); return
  if path=='/api/health': self.send_json({'ok':True}); return
  super().do_GET()
 def do_POST(self):
  path=urlparse(self.path).path
  length=int(self.headers.get('Content-Length','0'))
  if length>10000: self.send_json({'error':'body too large'},413); return
  try:
   body=json.loads(self.rfile.read(length))
   if path=='/api/events': self.send_json({'id':create_event(body['title'],body['source'],body.get('source_url',''),body.get('priority','NORMAL'))},201); return
   if path.startswith('/api/events/') and path.endswith('/transition'):
    eid=path.split('/')[3]; transition(eid,body['state']); self.send_json({'id':eid,'status':body['state']}); return
   self.send_json({'error':'not found'},404)
  except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e: self.send_json({'error':str(e)},400)
if __name__=='__main__':
 init(); port=int(os.environ.get('PORT','8000')); host=os.environ.get('HOST','127.0.0.1')
 print(f'ReachOut dashboard: http://{host}:{port}',flush=True); ThreadingHTTPServer((host,port),Handler).serve_forever()
