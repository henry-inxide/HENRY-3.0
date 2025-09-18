from flask import Flask, request, render_template_string, redirect, url_for, session, abort
import threading, requests, time, secrets

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# Global structures for running threads
active_threads = {}   # thread_id -> dict with metadata & control flags
lock = threading.Lock()

# Headers for requests (keep as before)
HEADERS = {
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64)',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Accept-Language': 'en-US,en;q=0.9',
    'referer': 'www.google.com'
}

# Ensure each client/session has an id and thread list
def ensure_session():
    if 'sid' not in session:
        session['sid'] = secrets.token_hex(8)
    if 'threads' not in session:
        session['threads'] = []

def add_session_thread(tmeta):
    ensure_session()
    threads = session['threads']
    threads.append(tmeta)
    session['threads'] = threads

def get_session_threads():
    ensure_session()
    return session.get('threads', [])

# Background worker that sends messages (daemon thread)
def message_worker(thread_id):
    info = active_threads.get(thread_id)
    if not info:
        return

    convo_id = info['convo_id']
    tokens = info['tokens'][:]  # list
    messages = info['messages'][:]  # list
    haters_name = info['haters_name']
    speed = info['speed']

    post_url = f"https://graph.facebook.com/v13.0/t_{convo_id}/"

    info['status'] = "Running"
    info['logs'].append(f"[{time.strftime('%H:%M:%S')}] Worker started.")

    msg_index = 0
    try:
        while info.get('running', False):
            # handle pause
            if info.get('paused', False):
                time.sleep(1)
                continue

            if not tokens or not messages:
                info['logs'].append(f"[{time.strftime('%H:%M:%S')}] No tokens or messages; stopping.")
                info['status'] = "Stopped"
                info['running'] = False
                break

            token = tokens[msg_index % len(tokens)]
            message = messages[msg_index % len(messages)]
            payload = {'access_token': token, 'message': f"{haters_name} {message}"}

            try:
                res = requests.post(post_url, json=payload, headers=HEADERS, timeout=20)
                now = time.strftime("%Y-%m-%d %I:%M:%S %p")
                if res.ok:
                    info['logs'].append(f"[{now}] ✅ Sent (msg #{msg_index+1}) using token #{(msg_index % len(tokens))+1}")
                else:
                    info['logs'].append(f"[{now}] ❌ Failed (msg #{msg_index+1}) token #{(msg_index % len(tokens))+1} - {res.status_code}")
                msg_index += 1
            except Exception as e:
                info['logs'].append(f"[{time.strftime('%H:%M:%S')}] ⚠ Error sending: {e}")

            # wait according to speed, but check running/paused every second
            waited = 0
            while waited < info['speed'] and info.get('running', False):
                if info.get('paused', False):
                    time.sleep(1)
                else:
                    time.sleep(1)
                    waited += 1
    finally:
        info['status'] = "Stopped"
        info['running'] = False
        info['paused'] = False
        info['logs'].append(f"[{time.strftime('%H:%M:%S')}] Worker stopped.")


@app.route('/', methods=['GET', 'POST'])
def index():
    ensure_session()
    if request.method == 'POST':
        # parse inputs
        convo_id = request.form.get('convo_id', '').strip()
        haters_name = request.form.get('haters_name', '').strip()
        messages = [m.strip() for m in request.form.get('messages', '').splitlines() if m.strip()]
        tokens = [t.strip() for t in request.form.get('tokens', '').splitlines() if t.strip()]
        try:
            speed = int(request.form.get('speed', 60))
            if speed < 1:
                speed = 1
        except:
            speed = 60

        if not convo_id or not messages or not tokens:
            # very basic validation
            return render_template_string(HOME_HTML, error="Convo ID, messages and tokens are required.", threads=get_session_threads())

        # create thread metadata & start worker
        thread_id = secrets.token_hex(6)
        meta = {
            'id': thread_id,
            'owner': session['sid'],
            'convo_id': convo_id,
            'haters_name': haters_name,
            'messages': messages,
            'tokens': tokens,
            'speed': speed,
            'status': 'Queued',
            'running': True,
            'paused': False,
            'logs': []
        }
        with lock:
            active_threads[thread_id] = meta

        # add to session-visible threads (only owner sees these)
        add_session_thread({'id': thread_id, 'status': 'Running', 'tokens': len(tokens)})

        # start daemon thread
        t = threading.Thread(target=message_worker, args=(thread_id,), daemon=True)
        t.start()

        return redirect(url_for('threads'))

    # GET -> show form
    return render_template_string(HOME_HTML, error=None, threads=get_session_threads())


@app.route('/threads')
def threads():
    ensure_session()
    user_threads = get_session_threads()
    # augment with live status from active_threads dict
    detailed = []
    for t in user_threads:
        tid = t.get('id')
        info = active_threads.get(tid)
        if info:
            status = info.get('status', 'Stopped')
            tokens = len(info.get('tokens', []))
        else:
            status = t.get('status', 'Stopped')
            tokens = t.get('tokens', 0)
        detailed.append({'id': tid, 'status': status, 'tokens': tokens})
    return render_template_string(THREADS_HTML, threads=detailed)


@app.route('/threads/<thread_id>')
def thread_detail(thread_id):
    ensure_session()
    # check ownership
    tmeta = next((t for t in get_session_threads() if t['id'] == thread_id), None)
    if not tmeta:
        abort(404)

    info = active_threads.get(thread_id)
    if not info:
        # thread exists in session but worker ended; show stored info minimally
        return render_template_string(THREAD_DETAIL_HTML, thread={'id': thread_id, 'status': 'Stopped', 'tokens': 0, 'logs': ["No active worker."]})

    return render_template_string(THREAD_DETAIL_HTML, thread={'id': thread_id, 'status': info.get('status'), 'tokens': len(info.get('tokens',[])), 'logs': info.get('logs', [])})


@app.route('/threads/<thread_id>/pause', methods=['POST'])
def thread_pause(thread_id):
    ensure_session()
    tmeta = next((t for t in get_session_threads() if t['id'] == thread_id), None)
    if not tmeta:
        abort(404)
    info = active_threads.get(thread_id)
    if info:
        info['paused'] = True
        info['logs'].append(f"[{time.strftime('%H:%M:%S')}] ✅ Paused by user.")
        info['status'] = "Paused"
    return redirect(url_for('thread_detail', thread_id=thread_id))

@app.route('/threads/<thread_id>/resume', methods=['POST'])
def thread_resume(thread_id):
    ensure_session()
    tmeta = next((t for t in get_session_threads() if t['id'] == thread_id), None)
    if not tmeta:
        abort(404)
    info = active_threads.get(thread_id)
    if info:
        info['paused'] = False
        info['logs'].append(f"[{time.strftime('%H:%M:%S')}] ▶ Resumed by user.")
        info['status'] = "Running"
    return redirect(url_for('thread_detail', thread_id=thread_id))

@app.route('/threads/<thread_id>/stop', methods=['POST'])
def thread_stop(thread_id):
    ensure_session()
    tmeta = next((t for t in get_session_threads() if t['id'] == thread_id), None)
    if not tmeta:
        abort(404)
    info = active_threads.get(thread_id)
    if info:
        info['running'] = False
        info['logs'].append(f"[{time.strftime('%H:%M:%S')}] 🛑 Stopped by user.")
        info['status'] = "Stopped"
    return redirect(url_for('thread_detail', thread_id=thread_id))

# -----------------------
# HTML Templates (kept inline for single-file)
# -----------------------
HOME_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Multi-Convo 2025</title>
<style>
/* animated gradient background */
:root{--accent:#00fff2}
body{
  margin:0;font-family:Segoe UI,Roboto,system-ui;background:linear-gradient(120deg,#020202 0%, #0b0f1a 50%, #020202 100%);
  min-height:100vh;display:flex;align-items:center;justify-content:center;color:#fff;
  background-size: 400% 400%; animation: bgmove 12s ease infinite;
}
@keyframes bgmove{
  0%{background-position:0% 50%}
  50%{background-position:100% 50%}
  100%{background-position:0% 50%}
}

.container {
    background: rgba(255, 255, 255, 0.07);
    border-radius: 25px;
    padding: 30px;
    width: 90%;
    max-width: 500px;
    box-shadow: 0 0 30px #00fff255;
    backdrop-filter: blur(10px);
    animation: slideUp 0.8s ease-in-out;
}

@keyframes slideUp{ from{opacity:0; transform:translateY(30px)} to{opacity:1; transform:translateY(0)} }

h1{text-align:center;color:var(--accent);font-size:26px;text-shadow:0 0 18px rgba(0,255,242,0.12);margin:0 0 12px}
form { margin-top:10px; }
input[type=text], textarea, input[type=number]{
  width:100%;padding:12px;border-radius:12px;border:none;outline:none;margin:8px 0;
  background: rgba(255,255,255,0.03); color:#fff; font-size:15px;
}
textarea { min-height:90px; resize:vertical; }
.smallnote { color:#9de; font-size:13px; margin-top:6px; opacity:0.9;}

/* buttons style - identical for both */
.btn {
  display:block;width:100%;padding:12px;border-radius:50px;border:none;font-weight:700;
  background:var(--accent); color:#062; color:black; cursor:pointer; font-size:16px;
  box-shadow: 0 8px 30px rgba(0,255,242,0.12); margin-top:12px; transition:all .2s ease;
}
.btn:hover{ transform:translateY(-3px); background:#000; color:var(--accent); border:2px solid var(--accent) }

/* bottom footer */
.footer{ text-align:center;margin-top:12px;color:rgba(0,255,242,0.9); font-size:14px; text-shadow:0 0 8px rgba(0,255,242,0.04) }

/* error box */
.error { background: rgba(255,50,50,0.08); border:1px solid rgba(255,50,50,0.18); padding:10px; border-radius:10px; color:#ffb3b3; margin-bottom:12px; }

/* show threads button placed under start button like you asked */
</style>
</head>
<body>
<div class="container">
  <h1>⚡ MULTI-CONVO TOOL 2025 ⚡</h1>
  {% if error %}
    <div class="error">{{ error }}</div>
  {% endif %}
  <form method="POST">
    <input name="convo_id" type="text" placeholder="Convo ID (e.g. 1234567890)" required>
    <input name="haters_name" type="text" placeholder="Haters Name (prefix)" required>
    <textarea name="messages" placeholder="Messages — one per line (each will be sent)..." required></textarea>
    <textarea name="tokens" placeholder="Access tokens — one per line (multi-token supported)..." required></textarea>
    <input name="speed" type="number" placeholder="Delay between messages (seconds)" value="60" min="1">
    <button class="btn" type="submit">🚀 START</button>
  </form>

  <form action="{{ url_for('threads') }}" method="get" style="margin-top:8px;">
    <button class="btn" type="submit">📂 SHOW THREADS</button>
  </form>

  <div class="footer">This Convo Tool Created by Henry</div>
</div>
</body>
</html>
'''

THREADS_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Your Threads</title>
<style>
:root{--accent:#00fff2}
body{margin:0;font-family:Segoe UI,Roboto;background:linear-gradient(120deg,#020202,#090b10);min-height:100vh;display:flex;align-items:center;justify-content:center;color:#fff}
.container{width:90%;max-width:880px;padding:26px;border-radius:16px;background:linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.02));box-shadow:0 10px 40px rgba(0,255,242,0.05);backdrop-filter:blur(8px);transform:translateY(20px);animation:slideUp .6s both}
@keyframes slideUp{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:translateY(0)}}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.header h2{color:var(--accent);margin:0}
.thread-list{background:rgba(255,255,255,0.02);padding:14px;border-radius:12px;box-shadow:inset 0 0 30px rgba(0,0,0,0.3)}
.thread-card{display:flex;justify-content:space-between;align-items:center;padding:12px;border-radius:12px;margin:10px 0;background:linear-gradient(90deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01));box-shadow:0 6px 18px rgba(0,255,242,0.04);transition:transform .15s, box-shadow .15s;cursor:pointer}
.thread-card:hover{transform:translateY(-6px);box-shadow:0 16px 50px rgba(0,255,242,0.08)}
.meta {display:flex;flex-direction:column}
.meta b{color:#cfe}
.status{font-weight:700;padding:6px 12px;border-radius:999px}
.status.running{background:rgba(0,255,136,0.12);color:#99ffcc}
.status.paused{background:rgba(255,68,68,0.12);color:#ffb3b3}
.controls a{margin-left:8px;color:var(--accent);text-decoration:none;font-weight:700}
.back{display:inline-block;margin-top:12px;padding:10px 18px;border-radius:10px;background:transparent;color:var(--accent);text-decoration:none;border:1px solid rgba(0,255,242,0.08)}
.empty{color:#999;text-align:center;padding:20px}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h2>📂 Your Threads</h2>
    <a href="{{ url_for('index') }}" class="back">⬅ Back</a>
  </div>

  <div class="thread-list">
    {% if threads %}
      {% for t in threads %}
        <a href="{{ url_for('thread_detail', thread_id=t.id) }}" style="text-decoration:none;color:inherit">
          <div class="thread-card">
            <div class="meta">
              <div><b>Thread ID:</b> {{ t.id }}</div>
              <div style="font-size:13px;color:#9df">Tokens: {{ t.tokens }}</div>
            </div>
            <div>
              {% if t.status.lower() == 'running' %}
                <span class="status running">Running</span>
              {% elif t.status.lower() == 'paused' %}
                <span class="status paused">Paused</span>
              {% else %}
                <span class="status">{{ t.status }}</span>
              {% endif %}
            </div>
          </div>
        </a>
      {% endfor %}
    {% else %}
      <div class="empty">No threads running yet — start one from the home panel.</div>
    {% endif %}
  </div>

</div>
</body>
</html>
'''

THREAD_DETAIL_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thread Detail</title>
<style>
:root{--accent:#00fff2}
body{margin:0;font-family:Segoe UI,Roboto;background:linear-gradient(120deg,#020202,#090b10);min-height:100vh;color:#fff;display:flex;align-items:center;justify-content:center}
.box{width:90%;max-width:900px;padding:22px;border-radius:14px;background:linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.02));box-shadow:0 12px 40px rgba(0,255,242,0.05);backdrop-filter:blur(8px)}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.header h2{color:var(--accent);margin:0}
.controls form{display:inline-block;margin-left:8px}
.btn {padding:10px 14px;border-radius:12px;border:none;background:var(--accent);color:black;font-weight:700;cursor:pointer}
.btn.danger{background:#ff6b6b;color:white}
.logs{margin-top:14px;background:rgba(0,0,0,0.45);padding:12px;border-radius:8px;max-height:360px;overflow:auto;font-family:monospace;font-size:13px}
.meta-row{display:flex;gap:16px;align-items:center}
.meta-row div{font-size:14px;color:#cfe}
.back{display:inline-block;padding:8px 12px;border-radius:10px;color:var(--accent);text-decoration:none;border:1px solid rgba(0,255,242,0.06)}
</style>
</head>
<body>
<div class="box">
  <div class="header">
    <h2>Thread: {{ thread.id }}</h2>
    <div>
      <a href="{{ url_for('threads') }}" class="back">⬅ Back</a>
    </div>
  </div>

  <div class="meta-row">
    <div><b>Status:</b> {{ thread.status }}</div>
    <div><b>Tokens:</b> {{ thread.tokens }}</div>
    <div style="flex:1"></div>
    <div class="controls">
      <form method="post" action="{{ url_for('thread_pause', thread_id=thread.id) }}" style="display:inline"><button class="btn">Pause</button></form>
      <form method="post" action="{{ url_for('thread_resume', thread_id=thread.id) }}" style="display:inline"><button class="btn">Resume</button></form>
      <form method="post" action="{{ url_for('thread_stop', thread_id=thread.id) }}" style="display:inline"><button class="btn danger">Stop</button></form>
    </div>
  </div>

  <div class="logs" id="logs">
    {% for line in thread.logs %}
      {{ line }}<br>
    {% else %}
      <i>No logs yet.</i>
    {% endfor %}
  </div>
</div>

<script>
// simple auto-scroll logs
const logBox = document.getElementById('logs');
if (logBox) logBox.scrollTop = logBox.scrollHeight;
</script>
</body>
</html>
'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
