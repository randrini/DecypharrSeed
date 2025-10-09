#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, sqlite3, time, logging, threading
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from flask import Flask, request, redirect, url_for, render_template, session, flash, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from jinja2 import DictLoader
import qbittorrentapi

# -------------------- Config --------------------
APP_PORT = int(os.environ.get("MCC_PORT", "8069"))
APP_HOST = os.environ.get("MCC_HOST", "0.0.0.0")
LOGIN_USER = os.environ.get("MCC_USER", "admin")
LOGIN_PASS = os.environ.get("MCC_PASS", "adminadmin")
DB_PATH    = os.environ.get("MCC_DB", os.path.expanduser("~/.magnet_cc.sqlite"))

DEFAULT_JSON_DIRS = [p for p in os.environ.get("MCC_JSON_DIRS","/data/alldebrid").split(":") if p]

logging.basicConfig(level=os.environ.get("LOGLEVEL","INFO"))
logger = logging.getLogger("app")

# -------------------- In-memory live logs --------------------
LOG_RING = deque(maxlen=2000)
LOG_SEQ = {"n": 0}

class UILogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        LOG_SEQ["n"] += 1
        LOG_RING.append({"seq": LOG_SEQ["n"], "msg": msg, "ts": int(time.time())})

ui_handler = UILogHandler()
ui_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(ui_handler)
logger.addHandler(ui_handler)

# -------------------- Utils --------------------
def human(n: Optional[int]) -> str:
    if not n or n <= 0: return "0 B"
    units = ["B","KB","MB","GB","TB","PB"]
    s = 0; f = float(n)
    while f >= 1024 and s < len(units)-1:
        f /= 1024.0; s += 1
    if s <= 1: return f"{int(f)} {units[s]}"
    return f"{f:.2f} {units[s]}"

def now_ts() -> int: return int(time.time())
def ensure_int(x, default=0):
    try: return int(x)
    except: return default

# -------------------- DB --------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, host TEXT, user TEXT, pass TEXT,
  precheck INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS state ( key TEXT PRIMARY KEY, ts INTEGER );
CREATE TABLE IF NOT EXISTS rules (
  host TEXT PRIMARY KEY,
  category TEXT,
  ratio REAL,
  seed_days INTEGER
);
CREATE TABLE IF NOT EXISTS sent (
  infohash TEXT PRIMARY KEY,
  client_id INTEGER,
  ts INTEGER
);
"""
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c
def init_db():
    with closing(db()) as con:
        con.executescript(SCHEMA)
        con.commit()
init_db()

def set_setting(k: str, v):
    with closing(db()) as con:
        con.execute("REPLACE INTO settings(k,v) VALUES (?,?)", (k, json.dumps(v)))
        con.commit()
def get_setting(k: str, default=None):
    with closing(db()) as con:
        cur = con.execute("SELECT v FROM settings WHERE k=?", (k,))
        row = cur.fetchone()
        if not row: return default
        try: return json.loads(row["v"])
        except: return default

def list_clients() -> List[dict]:
    with closing(db()) as con:
        cur = con.execute("SELECT id,name,host,user,pass,precheck FROM clients ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
def get_active_client_id() -> Optional[int]:
    cid = get_setting("active_client_id")
    if cid: return int(cid)
    cl = list_clients()
    return cl[0]["id"] if cl else None

def get_rules() -> Dict[str, dict]:
    with closing(db()) as con:
        cur = con.execute("SELECT host,category,ratio,seed_days FROM rules")
        return { r["host"]: dict(r) for r in cur.fetchall() }
def upsert_rule(host, category, ratio, seed_days):
    with closing(db()) as con:
        con.execute("REPLACE INTO rules(host,category,ratio,seed_days) VALUES (?,?,?,?)",
                    (host.strip().lower(), category.strip(), float(ratio) if ratio else None,
                     int(seed_days) if seed_days else None))
        con.commit()
def del_rule(host):
    with closing(db()) as con:
        con.execute("DELETE FROM rules WHERE host=?", (host,))
        con.commit()

def record_sent(infohash: str, client_id: int):
    with closing(db()) as con:
        con.execute("REPLACE INTO sent(infohash,client_id,ts) VALUES (?,?,?)",
                    (infohash.lower(), int(client_id), now_ts()))
        con.commit()
def sent_map() -> Dict[str, Tuple[int,int]]:
    with closing(db()) as con:
        cur = con.execute("SELECT infohash,client_id,ts FROM sent")
        return { r["infohash"].lower(): (r["client_id"], r["ts"]) for r in cur.fetchall() }

def delete_sent_all() -> int:
    with closing(db()) as con:
        cur = con.execute("SELECT COUNT(*) AS n FROM sent")
        n = cur.fetchone()["n"]
        con.execute("DELETE FROM sent")
        con.commit()
        return n
def delete_sent_by_infohashes(hashes: List[str]) -> int:
    if not hashes: return 0
    placeholders = ",".join("?" for _ in hashes)
    with closing(db()) as con:
        cur = con.execute(f"DELETE FROM sent WHERE infohash IN ({placeholders})", [h.lower() for h in hashes])
        con.commit()
        return cur.rowcount

# -------------------- Auth --------------------
def logged_in() -> bool: return bool(session.get("auth"))
def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def _w(*a, **kw):
        if not logged_in(): return redirect(url_for("login"))
        return fn(*a, **kw)
    return _w

# -------------------- Templates --------------------
T_BASE = """
<!doctype html>
<html lang="fr" data-bs-theme="light">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DecypharrSeed</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
  <style>
  :root {
  --primary-gradient: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  --success-gradient: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
  --danger-gradient: linear-gradient(135deg, #ee0979 0%, #ff6a00 100%);
  --card-shadow: 0 2px 12px rgba(0,0,0,.08);
  --card-shadow-hover: 0 8px 24px rgba(0,0,0,.12);
  --transition-smooth: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

[data-bs-theme="dark"] {
  --card-shadow: 0 2px 12px rgba(0,0,0,.3);
  --card-shadow-hover: 0 8px 24px rgba(0,0,0,.4);
}

* {
  transition: background-color 0.2s ease, color 0.2s ease, border-color 0.2s ease;
}

body {
  padding-top: 80px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  background: var(--bs-body-bg);
}

/* Navbar moderne avec glassmorphism */
.navbar {
  backdrop-filter: blur(10px);
  background: rgba(33, 37, 41, 0.95) !important;
  box-shadow: 0 2px 20px rgba(0,0,0,.1);
  border-bottom: 1px solid rgba(255,255,255,.1);
}

.navbar-brand {
  font-weight: 700;
  font-size: 1.4rem;
  background: var(--primary-gradient);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  letter-spacing: -0.5px;
}

.nav-item .btn {
  border-radius: 8px;
  font-weight: 500;
  padding: 0.5rem 1.2rem;
  transition: var(--transition-smooth);
  border: 1px solid transparent;
}

.nav-item .btn-primary {
  background: var(--primary-gradient);
  border: none;
  box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
}

.nav-item .btn-outline-light:hover {
  background: rgba(255,255,255,0.1);
  transform: translateY(-1px);
}

/* Cards améliorées */
.card {
  border: none;
  border-radius: 16px;
  box-shadow: var(--card-shadow);
  transition: var(--transition-smooth);
  overflow: hidden;
}

.card:hover {
  box-shadow: var(--card-shadow-hover);
  transform: translateY(-2px);
}

.card-title {
  font-weight: 600;
  font-size: 1.1rem;
  margin-bottom: 1rem;
  color: var(--bs-heading-color);
}

/* Badges modernes */
.badge {
  padding: 0.4em 0.8em;
  border-radius: 8px;
  font-weight: 600;
  letter-spacing: 0.3px;
}

.badge.bg-primary {
  background: var(--primary-gradient) !important;
}

.badge.bg-success {
  background: var(--success-gradient) !important;
}

/* Nouveau : badges orange & rouge modernes */
.badge.bg-warning {
  background: linear-gradient(135deg, #f59e0b 0%, #f97316 100%) !important;
  color: #111;
}
.badge.bg-danger {
  background: var(--danger-gradient) !important;
}

/* Boutons */
.btn {
  border-radius: 10px;
  font-weight: 500;
  padding: 0.6rem 1.4rem;
  transition: var(--transition-smooth);
  border-width: 2px;
}

.btn-primary {
  background: var(--primary-gradient);
  border: none;
  box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
}

.btn-primary:hover {
  box-shadow: 0 6px 20px rgba(102, 126, 234, 0.4);
  transform: translateY(-2px);
}

.btn-outline-primary:hover, .btn-outline-secondary:hover, .btn-outline-dark:hover {
  transform: translateY(-1px);
}

/* Tables */
.table {
  border-radius: 12px;
  overflow: hidden;
}

.table thead {
  background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
}

[data-bs-theme="dark"] .table thead {
  background: linear-gradient(135deg, #2d3238 0%, #1a1d23 100%);
}

.table tbody tr {
  transition: var(--transition-smooth);
}

.table tbody tr:hover {
  background: rgba(102, 126, 234, 0.05);
  transform: scale(1.005);
}

.sticky-th {
  position: sticky;
  top: 0;
  background: var(--bs-body-bg);
  z-index: 1;
  cursor: pointer;
  padding: 1rem !important;
  font-weight: 600;
}

.sticky-th:hover {
  background: rgba(102, 126, 234, 0.1);
}

.sticky-th .sort-hint {
  font-size: 0.75rem;
  opacity: 0.7;
  margin-left: 0.35rem;
}

/* Accordion moderne */
.accordion-item {
  border: none !important;
  margin-bottom: 1rem;
  border-radius: 12px !important;
  box-shadow: var(--card-shadow);
  overflow: hidden;
}

.accordion-button {
  border-radius: 12px !important;
  font-weight: 600;
  padding: 1.2rem 1.5rem;
  background: var(--bs-body-bg);
}

.accordion-button:not(.collapsed) {
  background: linear-gradient(135deg, rgba(102, 126, 234, 0.1) 0%, rgba(118, 75, 162, 0.1) 100%);
  box-shadow: none;
}

.accordion-button:focus {
  box-shadow: 0 0 0 0.25rem rgba(102, 126, 234, 0.25);
}

/* Formulaires */
.form-control, .form-select {
  border-radius: 10px;
  border: 2px solid var(--bs-border-color);
  padding: 0.7rem 1rem;
  transition: var(--transition-smooth);
}

.form-control:focus, .form-select:focus {
  border-color: #667eea;
  box-shadow: 0 0 0 0.25rem rgba(102, 126, 234, 0.15);
}

/* Stats cards spéciales */
.stat-card {
  background: var(--bs-body-bg);
  border-left: 4px solid transparent;
  transition: var(--transition-smooth);
}

.stat-card:hover {
  border-left-color: #667eea;
}

.stat-number {
  font-size: 2rem;
  font-weight: 700;
  background: var(--primary-gradient);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

/* Footer */
footer {
  margin-top: 80px;
  padding: 2rem 0;
  border-top: 1px solid var(--bs-border-color);
  color: var(--bs-secondary-color);
  font-size: 0.9rem;
}

footer a {
  color: #667eea;
  text-decoration: none;
  transition: var(--transition-smooth);
}

footer a:hover {
  color: #764ba2;
}

/* Animations */
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.card, .accordion-item {
  animation: fadeIn 0.3s ease-out;
}

/* Chart container */
.chart-holder {
  height: 400px;
  position: relative;
  padding: 1.5rem;
  background: var(--bs-body-bg);
  border-radius: 12px;
}

/* Logs */
pre.logbox {
  background: linear-gradient(135deg, #2d3238 0%, #1a1d23 100%);
  color: #e0e0e0;
  padding: 1.5rem;
  border-radius: 12px;
  height: 60vh;
  overflow: auto;
  font-family: 'Courier New', monospace;
  font-size: 0.9rem;
  box-shadow: inset 0 2px 8px rgba(0,0,0,.2);
}

/* Back to top */
#toTop {
  position: fixed;
  bottom: 30px;
  right: 30px;
  display: none;
  z-index: 1030;
  width: 50px;
  height: 50px;
  border-radius: 50%;
  background: var(--primary-gradient);
  border: none;
  box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
  font-size: 1.5rem;
  transition: var(--transition-smooth);
}

#toTop:hover {
  transform: translateY(-3px);
  box-shadow: 0 6px 20px rgba(102, 126, 234, 0.5);
}

/* Sticky elements */
.sticky-actions {
  position: sticky;
  top: var(--sticky-actions-top, 64px);
  z-index: 1020;
  background: var(--bs-body-bg);
  padding: 0.75rem;
  border: 1px solid var(--bs-border-color);
  border-radius: 0.5rem;
  margin-bottom: 1rem;
}

.sticky-trackers {
  position: sticky;
  top: var(--sticky-trackers-top, 126px);
  z-index: 1010;
  background: var(--bs-body-bg);
  padding: 0.5rem 0;
  border-bottom: 1px solid var(--bs-border-color);
}

/* Utilitaires */
.small-muted {
  font-size: 0.85rem;
  color: var(--bs-secondary-color);
}

.nowrap {
  white-space: nowrap;
}

.name-cell {
  white-space: normal;
  word-break: break-word;
  overflow-wrap: anywhere;
}

.tracker-badge {
  text-transform: lowercase;
}

/* Dark mode pills */
[data-bs-theme="dark"] .sticky-trackers .btn-outline-dark {
  color: #f1f1f1;
  border-color: #8a8a8a;
}

[data-bs-theme="dark"] .sticky-trackers .btn-outline-dark:hover {
  color: #000;
  background: #f1f1f1;
  border-color: #f1f1f1;
}

[data-bs-theme="dark"] .sticky-trackers .small-muted {
  color: #ddd !important;
}

/* Responsive */
@media (min-width: 1200px) {
  .container {
    max-width: 1320px;
  }
  .navbar-nav .btn {
    border-radius: 0.5rem;
  }
}

@media (min-width: 992px) {
  .nav-center {
    position: absolute;
    left: 50%;
    transform: translateX(-50%);
  }
}

@media (max-width: 991.98px) {
  .nav-center {
    position: static;
    transform: none;
  }
}

/* Scan accordion headers */
.scan-acc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.scan-acc-meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  justify-content: flex-end;
  width: 340px;
}

@media (max-width: 992px) {
  .scan-acc-meta {
    width: auto;
  }
}

  </style>
</head>
<body id="top">
<nav class="navbar navbar-expand-lg navbar-dark bg-dark fixed-top">
  <div class="container">
    <!-- Gauche : Logo -->
    <a class="navbar-brand" href="{{ url_for('dashboard') }}">DecypharrSeed</a>

    <!-- Burger -->
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
      <span class="navbar-toggler-icon"></span>
    </button>

    <!-- Centre + Droite -->
    <div class="collapse navbar-collapse" id="navbarNav">
      <!-- Menu CENTRÉ (sur la largeur du container) -->
      <ul class="navbar-nav nav-center d-flex flex-wrap flex-md-row flex-column gap-2 justify-content-center">
        <li class="nav-item">
          <a class="btn btn-sm {{ 'btn-primary' if request.endpoint=='dashboard' else 'btn-outline-light' }}" href="{{ url_for('dashboard') }}">Tableau de bord</a>
        </li>
        <li class="nav-item">
          <a class="btn btn-sm {{ 'btn-primary' if request.endpoint=='scan' else 'btn-outline-light' }}" href="{{ url_for('scan') }}">Scan & Sélection</a>
        </li>
        <li class="nav-item">
          <a class="btn btn-sm {{ 'btn-primary' if request.endpoint=='rules' else 'btn-outline-light' }}" href="{{ url_for('rules') }}">Règles Trackers</a>
        </li>
        <li class="nav-item">
          <a class="btn btn-sm {{ 'btn-primary' if request.endpoint=='settings' else 'btn-outline-light' }}" href="{{ url_for('settings') }}">Paramètres</a>
        </li>
        <li class="nav-item">
          <a class="btn btn-sm {{ 'btn-primary' if request.endpoint=='logs' else 'btn-outline-light' }}" href="{{ url_for('logs') }}">Logs</a>
        </li>
      </ul>

      <!-- Droite : actions -->
      <div class="d-flex align-items-center gap-2 ms-auto">
        <button id="themeBtn" class="btn btn-outline-light btn-sm" type="button" title="Basculer le thème">🌙</button>
        <a class="btn btn-outline-light btn-sm" href="{{ url_for('logout') }}">Logout</a>
      </div>
    </div>
  </div>
</nav>

<div class="container">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div class="mt-2">
      {% for cat, msg in messages %}
        <div class="alert alert-{{ 'danger' if cat=='error' else cat }}">{{ msg|safe }}</div>
      {% endfor %}
      </div>
    {% endif %}
  {% endwith %}
  {% block content %}{% endblock %}

  <footer class="text-center">
    DecypharrSeed — <a href="https://github.com/sirrobot01/decypharr" target="_blank" rel="noopener">Decypharr</a> ·
    <a href="https://github.com/aerya" target="_blank" rel="noopener">@aerya</a>
  </footer>
</div>
<button id="toTop" class="btn btn-dark" title="Haut de page">↑</button>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
function applyTheme(t){
  document.documentElement.setAttribute('data-bs-theme', t);
  localStorage.setItem('theme', t);
  const btn = document.getElementById('themeBtn');
  if(btn) btn.textContent = (t==='dark'?'☀️':'🌙');
  window.dispatchEvent(new CustomEvent('theme-changed', {detail:{theme:t}}));
}
(function initTheme(){
  const stored = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark':'light');
  applyTheme(stored);
})();
document.getElementById('themeBtn')?.addEventListener('click', ()=>{
  const cur = document.documentElement.getAttribute('data-bs-theme') || 'light';
  applyTheme(cur==='light' ? 'dark':'light');
});
const toTop = document.getElementById('toTop');
window.addEventListener('scroll', ()=>{ toTop.style.display = window.scrollY>400 ? 'block':'none'; });
toTop?.addEventListener('click', ()=>{ window.scrollTo({top:0, behavior:'smooth'}); });
function toggleGroup(cls, checked){ document.querySelectorAll('.'+cls).forEach(cb=>cb.checked=checked); }
function updateStickyOffsets(){
  const nav = document.querySelector('.navbar');
  const actions = document.querySelector('.sticky-actions');
  const root = document.documentElement;
  const navH = nav ? nav.offsetHeight : 56;
  const actionsH = actions ? actions.offsetHeight : 0;
  root.style.setProperty('--sticky-actions-top', navH + 'px');
  root.style.setProperty('--sticky-trackers-top', (navH + actionsH + 8) + 'px');
}
window.addEventListener('load', updateStickyOffsets);
window.addEventListener('resize', updateStickyOffsets);
const _ro = (typeof ResizeObserver!=='undefined') ? new ResizeObserver(updateStickyOffsets) : null;
if(_ro){
  const actions = document.querySelector('.sticky-actions');
  if(actions) _ro.observe(actions);
}
/* Tri client-side (utilisé par /scan) */
function sortTableBy(table, key){
  const tbody = table.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const curKey = table.dataset.sortKey || '';
  const curDir = table.dataset.sortDir || 'desc';
  const nextDir = (curKey===key && curDir==='desc') ? 'asc' : 'desc';
  rows.sort((a,b)=>{
    const av = Number(a.dataset[key]||0);
    const bv = Number(b.dataset[key]||0);
    return nextDir==='asc' ? (av-bv) : (bv-av);
  });
  rows.forEach(r=>tbody.appendChild(r));
  table.dataset.sortKey = key;
  table.dataset.sortDir = nextDir;
  table.querySelectorAll('.sort-hint').forEach(s=>s.textContent='');
  const hint = table.querySelector('.sort-hint[data-for="'+key+'"]');
  if(hint){ hint.textContent = nextDir==='desc' ? '▼' : '▲'; }
}
document.addEventListener('click', (ev)=>{
  const btn = ev.target.closest('.sort-btn');
  if(!btn) return;
  ev.preventDefault();
  const tableId = btn.dataset.table;
  const key = btn.dataset.key;
  const table = document.getElementById(tableId);
  if(table) sortTableBy(table, key);
});

/* Reset helper (évite les <form> imbriqués) */
function setReset(scope, label){
  const s = document.getElementById('resetScope');
  const l = document.getElementById('resetLabel');
  if(s) s.value = scope;
  if(l) l.value = label || '';
}
</script>
</body>
</html>
"""

T_LOGIN = """{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center">
 <div class="col-md-6">
  <div class="card shadow">
    <div class="card-body">
      <h5 class="card-title">Connexion</h5>
      <form method="post">
        <div class="row g-2">
          <div class="col-4">
            <label class="form-label">Utilisateur</label>
            <input class="form-control" name="user">
          </div>
          <div class="col-8">
            <label class="form-label">Mot de passe</label>
            <input type="password" class="form-control" name="pass">
          </div>
        </div>
        <button class="btn btn-primary w-100 mt-3">Entrer</button>
      </form>
    </div>
  </div>
 </div>
</div>
{% endblock %}
"""

T_DASH = """{% extends "base.html" %}
{% block content %}
<h3>Tableau de bord</h3>


<div class="row g-3">
  <div class="col-xl-4 col-md-6">
    <div class="card shadow-sm h-100">
      <div class="card-body">
        <div class="d-flex justify-content-between">
          <h6 class="mb-0">Trackers (dernier scan)</h6>
          <span class="badge bg-primary">{{ dash["stats"]["trackers"] or 0 }}</span>
        </div>
        <div class="small-muted mt-1">Scannés: {{ dash["stats"]["items"] or 0 }}</div>
        <div class="small-muted">Total: {{ dash["stats"]["total_hr"] or '?' }}</div>
        <a class="btn btn-link p-0 mt-2" href="{{ url_for('scan') }}">Voir le détail</a>
      </div>
    </div>
  </div>
  <div class="col-xl-4 col-md-6">
    <div class="card shadow-sm h-100">
      <div class="card-body">
        <div class="d-flex justify-content-between">
          <h6 class="mb-0">Règles</h6>
          <span class="badge bg-secondary">{{ dash["rules_count"] }}</span>
        </div>
        <div class="small-muted mt-1">Catégories qBit auto : oui</div>
        <a class="btn btn-link p-0 mt-2" href="{{ url_for('rules') }}">Gérer</a>
      </div>
    </div>
  </div>
  <div class="col-xl-4 col-md-6">
    <div class="card shadow-sm h-100">
      <div class="card-body">
        <div class="d-flex justify-content-between">
          <h6 class="mb-0">Clients qBittorrent</h6>
          <span class="badge bg-secondary">{{ dash["clients_count"] }}</span>
        </div>
        <div class="small-muted mt-1">Actif: {{ dash["active_client"] or '(aucun)' }}</div>
        <a class="btn btn-link p-0 mt-2" href="{{ url_for('settings') }}">Configurer</a>
      </div>
    </div>
  </div>
</div>

{% if dash.chart and dash.chart.labels|length>0 %}
<div class="card shadow-sm mt-3">
  <div class="card-body">
    <div class="chart-holder"><canvas id="comboChart"></canvas></div>
  </div>
</div>
<script>
Chart.register(ChartDataLabels);
const labels    = {{ dash.chart.labels|tojson }};
const scanned   = {{ dash.chart.counts_scan|tojson }};
const seededAct = {{ dash.chart.counts_qbit|tojson }};
const seededAll = {{ dash.chart.counts_seed_global|tojson }};
const sizesGB   = {{ dash.chart.sizes_gb|tojson }};

function isDark(){ return (document.documentElement.getAttribute('data-bs-theme')||'light')==='dark'; }
function labelColor(){ return isDark() ? '#fff' : '#111'; }
function gridColor(){ return getComputedStyle(document.documentElement).getPropertyValue('--bs-border-color').trim() || (isDark()?'#555':'#ddd'); }

const ctx = document.getElementById('comboChart');

let chart = new Chart(ctx, {
  type:'bar',
  data:{
    labels,
    datasets:[
      // Volume (Go) sur axe droit (barres)
      { type:'bar', label:'Volume (Go)', data:sizesGB, yAxisID:'y2',
        borderWidth:1.5, borderRadius:6, categoryPercentage:.7, barPercentage:.7 },

      // Comptes en lignes, axe gauche
      { type:'line', label:'Torrents', data:scanned, yAxisID:'y',
        tension:.2, pointRadius:3, pointHoverRadius:5 },
      { type:'line', label:'En seed', data:seededAct, yAxisID:'y',
        tension:.2, pointRadius:3, pointHoverRadius:5 },
      { type:'line', label:'Seedés', data:seededAll, yAxisID:'y',
        tension:.2, pointRadius:3, pointHoverRadius:5 }
    ]
  },
  options:{
    responsive:true, maintainAspectRatio:false, animation:{ duration: 200 },
    layout:{ padding:{ top:36, right:12, bottom:8, left:12 } },
    interaction:{ mode:'index', intersect:false },
    plugins:{
      legend:{
        display:true,
        labels:{ color: labelColor() }
      },
      tooltip:{ callbacks:{
        label:(ctx)=>{
          const v = ctx.raw;
          if(ctx.dataset.yAxisID==='y2'){
            return ` ${ctx.dataset.label}: ${Number(v||0).toLocaleString('fr-FR')} Go`;
          }
          return ` ${ctx.dataset.label}: ${v}`;
        }
      }},
      datalabels:{
        color: labelColor(), font:{ weight:700, size:11 }, align:'top', anchor:'end', offset:4, clip:false, clamp:true,
        formatter:(value, ctx)=> (ctx.dataset.type==='line') ? `${value}` : ''
      }
    },
    scales:{
      x:{ ticks:{ color:labelColor(), maxRotation:0, autoSkip:false }, grid:{ color:gridColor() }},
      y:{ beginAtZero:true,
          title:{ display:true, text:'Nombre de torrents', color:labelColor() },
          ticks:{ color:labelColor(), precision:0 },
          grid:{ color:gridColor() } },
      y2:{ beginAtZero:true, position:'right',
           title:{ display:true, text:'Volume (Go)', color:labelColor() },
           ticks:{ color:labelColor() },
           grid:{ drawOnChartArea:false } }
    }
  }
});
window.addEventListener('theme-changed', ()=>{
  chart.options.plugins.datalabels.color = labelColor();
  chart.options.plugins.legend.labels.color = labelColor();
  chart.options.scales.x.ticks.color = labelColor();
  chart.options.scales.y.ticks.color = labelColor();
  chart.options.scales.y2.ticks.color = labelColor();
  chart.options.scales.x.grid.color = gridColor();
  chart.options.scales.y.grid.color = gridColor();
  chart.update();
});
</script>
{% endif %}

<!-- Les 3 blocs -->
<div class="card shadow-sm mt-3">
  <div class="card-body">
    <h6 class="card-title mb-2">10 torrents les plus lourds (au dernier scan)</h6>
    <div class="table-responsive">
      <table class="table table-sm align-middle mb-0">
        <thead class="table-light"><tr><th>Nom</th><th>Tracker</th><th class="text-end">Taille</th><th class="text-end">Seeding</th></tr></thead>
        <tbody>
        {% for row in dash.top_heaviest %}
          <tr>
            <td class="name-cell">{{ row['name'] }}</td>
            <td class="small">{{ row['label'] }}</td>
            <td class="text-end nowrap">{{ row['size_hr'] }}</td>
            <td class="text-end">
              {% if row.get('live_seed') %}
                <span class="badge bg-success">En seed</span>
              {% elif row['sent'] %}
                <span class="badge bg-warning">Seedé</span>
              {% else %}
                <span class="badge bg-danger">Jamais</span>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</div>

<div class="card shadow-sm mt-3">
  <div class="card-body">
    <h6 class="card-title mb-2">10 derniers torrents</h6>
    <div class="table-responsive">
      <table class="table table-sm align-middle mb-0">
        <thead class="table-light"><tr><th>Nom</th><th>Tracker</th><th class="text-end">Date</th><th class="text-end">Seeding</th></tr></thead>
        <tbody>
        {% for row in dash.top_latest %}
          <tr>
            <td class="name-cell">{{ row['name'] }}</td>
            <td class="small">{{ row['label'] }}</td>
            <td class="text-end small nowrap">{{ row['date_hr'] }}</td>
            <td class="text-end">
              {% if row.get('live_seed') %}
                <span class="badge bg-success">En seed</span>
              {% elif row['sent'] %}
                <span class="badge bg-warning">Seedé</span>
              {% else %}
                <span class="badge bg-danger">Jamais</span>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</div>

<div class="card shadow-sm mt-3">
  <div class="card-body">
    <h6 class="card-title mb-2">3 derniers torrents par tracker</h6>
    <div class="table-responsive">
      <table class="table table-sm align-middle mb-0">
        <thead class="table-light"><tr><th>Nom</th><th>Tracker</th><th class="text-end">Date</th><th class="text-end">Seeding</th></tr></thead>
        <tbody>
        {% for row in dash.top3_by_tracker %}
          <tr>
            <td class="name-cell">{{ row['name'] }}</td>
            <td class="small">{{ row['label'] }}</td>
            <td class="text-end small nowrap">{{ row['date_hr'] }}</td>
            <td class="text-end">
              {% if row.get('live_seed') %}
                <span class="badge bg-success">En seed</span>
              {% elif row['sent'] %}
                <span class="badge bg-warning">Seedé</span>
              {% else %}
                <span class="badge bg-danger">Jamais</span>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</div>
{% endblock %}
"""

T_RULES = """{% extends "base.html" %}
{% block content %}
<h3>Règles Trackers</h3>
<p class="small-muted">Définis le libellé (catégorie qBittorrent), le ratio et le seedtime (jours) par Tracker. Le .torrent sera supprimé de qBittorrent dès la 1ère limite de ratio OU seedtime atteinte.
Quand plusieurs URLs ont le même <em>libellé</em>, elles seront regroupées partout.</p>

<form method="post" class="mb-3">
  <input type="hidden" name="action" value="add">
  <div class="row g-2">
    <div class="col-md-4">
      <label class="form-label">URL du tracker (host)</label>
      <input name="host" class="form-control" placeholder="ex: connect.maxp2p.org">
    </div>
    <div class="col-md-3">
      <label class="form-label">Catégorie qBittorrent</label>
      <input name="category" class="form-control" placeholder="ex: yggtorrents">
    </div>
    <div class="col-md-2">
      <label class="form-label">Ratio minimum à atteindre</label>
      <input name="ratio" class="form-control" placeholder="2.0">
    </div>
    <div class="col-md-2">
      <label class="form-label">Seedtime maximum (jours)</label>
      <input name="seed_days" class="form-control" placeholder="14">
    </div>
    <div class="col-md-1 d-grid">
      <label class="form-label">&nbsp;</label>
      <button class="btn btn-primary">Ajouter</button>
    </div>
  </div>
</form>

<form method="post">
  <input type="hidden" name="action" value="save">
  <div class="table-responsive">
    <table class="table table-sm align-middle">
      <thead class="table-light"><tr>
        <th>URL du tracker</th><th>Catégorie qBittorrent</th><th>Ratio minimum à atteindre</th><th>Seedtime maximum (jours)</th><th></th>
      </tr></thead>
      <tbody>
        {% for r in rules %}
        <tr>
          <td><input class="form-control" name="host_{{loop.index}}" value="{{r['host']}}" readonly></td>
          <td><input class="form-control" name="cat_{{loop.index}}" value="{{r['category'] or ''}}"></td>
          <td><input class="form-control" name="ratio_{{loop.index}}" value="{{r['ratio'] if r['ratio'] is not none else ''}}"></td>
          <td><input class="form-control" name="seed_{{loop.index}}" value="{{r['seed_days'] if r['seed_days'] is not none else ''}}"></td>
          <td class="text-end">
            <button name="del" value="{{r['host']}}" class="btn btn-sm btn-outline-danger" formaction="{{ url_for('rules') }}" formmethod="post" onclick="this.form.action='{{ url_for('rules') }}'; this.form.elements['action'].value='del';">Supprimer</button>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <div class="d-flex gap-2">
    <button class="btn btn-primary">Enregistrer</button>
    {% if last_hosts and last_hosts|length>0 %}
      <form method="post" class="d-inline">
        <input type="hidden" name="action" value="add_from_scan">
        <button class="btn btn-outline-secondary">Ajouter depuis le dernier scan ({{ last_hosts|length }})</button>
      </form>
    {% endif %}
  </div>
</form>
{% endblock %}
"""

# ---------- SETTINGS (STACKED) + backup ----------
T_SETTINGS = """{% extends "base.html" %}
{% block content %}
<h3>Paramètres</h3>
<p class="small-muted">
  Note&nbsp;: la vérification du statut des .torrent (<em>En seed</em> / <em>Seedés</em>) est effectuée à chaque scan des JSON.
</p>

<!-- Dossier.s JSON -->
<div class="card shadow-sm mb-3">
  <div class="card-body">
    <h6>Dossiers JSON</h6>
    <form method="post">
      <input type="hidden" name="action" value="save_json_dirs">
      <div class="mb-2">
        <label class="form-label">1 chemin par ligne</label>
        <textarea class="form-control" name="json_dirs" rows="5" placeholder="/data/alldebrid">{{ json_dirs|join('\\n') }}</textarea>
      </div>
      <button class="btn btn-primary">Enregistrer</button>
    </form>
  </div>
</div>

<!-- Clients qBittorrent -->
<div class="card shadow-sm mb-3">
  <div class="card-body">
    <h6>Client.s qBittorrent</h6>
    <form method="post" class="mb-2">
      <input type="hidden" name="action" value="add_qbit">
      <div class="row g-2 align-items-end">
        <div class="col-md-3"><label class="form-label">Nom</label><input class="form-control" name="name" placeholder="Nom"></div>
        <div class="col-md-4"><label class="form-label">Host</label><input class="form-control" name="host" placeholder="http://192.168.x.x:8080"></div>
        <div class="col-md-2"><label class="form-label">Utilisateur</label><input class="form-control" name="user" placeholder="user"></div>
        <div class="col-md-2"><label class="form-label">Mot de passe</label><input class="form-control" name="pass" placeholder="pass"></div>
        <div class="col-md-1">
          <div class="form-check">
            <input class="form-check-input" type="checkbox" name="precheck" id="precheckAdd" checked>
            <label class="form-check-label" for="precheckAdd" title="Vérifier l'espace disque avant d'envoyer">Pré-check</label>
          </div>
        </div>
      </div>
      <button class="btn btn-primary mt-2">Ajouter</button>
    </form>

    <div class="table-responsive">
      <table class="table table-sm">
        <thead class="table-light"><tr><th>Nom</th><th>Host</th><th>Utilisateur</th><th>Pré-check espace libre</th><th></th></tr></thead>
        <tbody>
          {% for c in clients %}
          <tr>
            <td>{{ c['name'] }}</td>
            <td>{{ c['host'] }}</td>
            <td>{{ c['user'] }}</td>
            <td>
              <form method="post" class="d-inline">
                <input type="hidden" name="action" value="toggle_precheck">
                <input type="hidden" name="id" value="{{ c['id'] }}">
                <button class="btn btn-sm {{ 'btn-success' if c['precheck'] else 'btn-outline-secondary' }}" title="Activer/désactiver le pré-check">{{ 'ON' if c['precheck'] else 'OFF' }}</button>
              </form>
            </td>
            <td class="text-end">
              <form method="post" class="d-inline">
                <input type="hidden" name="action" value="del_qbit">
                <input type="hidden" name="id" value="{{ c['id'] }}">
                <button class="btn btn-sm btn-outline-danger">Supprimer</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- Scan régulier -->
<div class="card shadow-sm mb-3">
  <div class="card-body">
    <h6>Recherche régulière</h6>
    <form method="post" class="row g-2 align-items-end">
      <input type="hidden" name="action" value="save_autoscan">
      <div class="col-md-4">
        <div class="form-check">
          <input class="form-check-input" type="checkbox" name="autoscan_enabled" id="autoscan_enabled" {% if autoscan.enabled %}checked{% endif %}>
          <label class="form-check-label" for="autoscan_enabled">Automatiser</label>
        </div>
      </div>
      <div class="col-md-4">
        <label class="form-label">Intervalle (minutes)</label>
        <input class="form-control" name="autoscan_interval" value="{{ autoscan.interval }}">
      </div>
        <div class="col-md-4 d-grid">
        <button class="btn btn-primary">Enregistrer</button>
      </div>
    </form>
    <div class="small-muted mt-2">Dernière fois : {{ autoscan.last or '(jamais)' }}</div>
  </div>
</div>

<!-- Envoi automatique -->
<div class="card shadow-sm mb-3">
  <div class="card-body">
    <h6>Envoi automatique (basé sur l'intervalle de recherche)</h6>
    <form method="post" class="mb-2">
      <input type="hidden" name="action" value="save_autosend_global">
      <div class="row g-2 align-items-end">
        <div class="col-md-4">
          <div class="form-check">
            <input class="form-check-input" type="checkbox" name="as_global_enabled" id="as_global_enabled" {% if autosend.global_enabled %}checked{% endif %}>
            <label class="form-check-label" for="as_global_enabled">Activer globalement</label>
          </div>
        </div>
        <div class="col-md-4">
          <label class="form-label">Client cible</label>
          <select class="form-select" name="as_global_client">
            {% for c in clients %}<option value="{{c['id']}}" {% if c['id']==autosend.global_client %}selected{% endif %}>{{c['name']}} — {{c['host']}}</option>{% endfor %}
          </select>
        </div>
        <div class="col-md-4 d-grid">
          <button class="btn btn-primary">Enregistrer</button>
        </div>
      </div>
    </form>

    <form method="post">
      <input type="hidden" name="action" value="save_autosend_trackers">
      <div class="mb-2">
        <label class="form-label">Trackers → client</label>
        <div class="row g-2">
          {% for label in tracker_labels %}
          <div class="col-md-6">
            <div class="input-group input-group-sm">
              <span class="input-group-text">{{ label }}</span>
              <select class="form-select" name="map__{{ label }}">
                <option value="">(désactivé)</option>
                {% for c in clients %}
                  <option value="{{c['id']}}" {% if autosend.map.get(label)==c['id'] %}selected{% endif %}>{{c['name']}} — {{c['host']}}</option>
                {% endfor %}
              </select>
            </div>
          </div>
          {% endfor %}
        </div>
      </div>
      <button class="btn btn-primary btn-sm mt-2">Enregistrer la configuration selon les trackers</button>
    </form>
  </div>
</div>

<!-- Backup auto (24h, rétention 7 jours) -->
<div class="card shadow-sm mb-3">
  <div class="card-body">
    <h6>Backup automatique de la BDD</h6>
    <p class="small-muted mb-2">Fréquence : <strong>toutes les 24h</strong> · Rétention : <strong>7 jours</strong>.</p>
    <form method="post" class="row g-2 align-items-end">
      <input type="hidden" name="action" value="save_backup">
      <div class="col-md-4">
        <div class="form-check">
          <input class="form-check-input" type="checkbox" name="bk_enabled" id="bk_enabled" {% if backup.enabled %}checked{% endif %}>
          <label class="form-check-label" for="bk_enabled">Activer</label>
        </div>
      </div>
      <div class="col-md-4">
        <label class="form-label">Dossier</label>
        <input class="form-control" name="bk_dir" value="{{ backup.dir }}">
      </div>
      <div class="col-md-4 d-grid">
        <button class="btn btn-primary">Enregistrer</button>
      </div>
    </form>
    <div class="small-muted mt-2">Dernier backup : {{ backup.last or '(jamais)' }}</div>
    <form method="post" class="mt-2">
      <input type="hidden" name="action" value="backup_now">
      <button class="btn btn-outline-secondary btn-sm">Sauvegarder maintenant</button>
    </form>
  </div>
</div>
{% endblock %}
"""

# -------------------- SCAN --------------------
NO_TRACKER_LABEL = "(sans-tracker)"

T_SCAN = """{% extends "base.html" %}
{% block content %}
<h3>Scan JSON & sélection</h3>

<form id="enqueueForm" method="post" action="{{ url_for('enqueue') }}">
  <div class="sticky-actions d-flex flex-wrap align-items-end gap-2">
    <a class="btn btn-secondary" href="{{ url_for('scan', do=1) }}">Scanner maintenant</a>
    <a class="btn btn-outline-secondary" href="{{ url_for('dashboard') }}">Retour</a>

    <div class="ms-auto d-flex align-items-end gap-2">
      <button type="button" id="expandAllBtn" class="btn btn-outline-dark">Tout déplier</button>
      <button type="button" id="collapseAllBtn" class="btn btn-outline-dark">Tout replier</button>
    </div>

    <div class="vr d-none d-md-inline mx-2"></div>
    {% if clients and clients|length>0 %}
      <div>
        <label class="form-label mb-1">Envoyer la sélection sur…</label>
        <select class="form-select" name="client_id" style="min-width: 320px;">
          {% for c in clients %}
            <option value="{{c['id']}}" {% if c['id']==active_id %}selected{% endif %}>
              {{ c['name'] }} — {{ c['host'] }} {% if c.get('precheck',1) %}(pré-check){% else %}(sans pré-check){% endif %}
            </option>
          {% endfor %}
        </select>
      </div>
      <button class="btn btn-primary">Ajouter à qBittorrent</button>
    {% else %}
      <div class="alert alert-warning mb-0">Aucun client qBittorrent configuré. Va dans <a href="{{ url_for('settings') }}">Paramètres</a>.</div>
      <button class="btn btn-primary" disabled>Ajouter à qBittorrent</button>
    {% endif %}
  </div>

  {% if grouped %}
  <div class="sticky-trackers">
    <div class="nav nav-pills flex-wrap gap-2">
      <a class="btn btn-sm btn-outline-dark" href="#grp-all">Tous (vue globale)
        <span class="badge bg-secondary">{{ global_items|length }}</span>
        <span class="small-muted ms-1">{{ global_total_hr }}</span>
      </a>
      {% for label, grp in grouped.items() %}
        {% set anchor = 'grp-' ~ (label|slug) %}
        <a class="btn btn-sm btn-outline-dark" href="#{{ anchor }}">
          {{ label }}
          <span class="badge bg-secondary">{{ grp['items']|length }}</span>
          <span class="small-muted ms-1">{{ grp.total_hr }}</span>
        </a>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="accordion mt-3" id="acc-scan">
    <!-- Vue globale -->
    <div class="accordion-item" id="grp-all">
      <h2 class="accordion-header" id="h-0">
        <div class="scan-acc-header">
          <button class="accordion-button collapsed flex-grow-1 text-start"
                  type="button" data-bs-toggle="collapse" data-bs-target="#c-0">
            <div class="d-flex align-items-center gap-2">
              <span class="badge bg-dark">Tous les trackers (vue globale)</span>
              <span class="small-muted">{{ global_items|length }} élément(s)</span>
              <span class="small-muted">Total: <strong>{{ global_total_hr }}</strong></span>
              <span class="small-muted">Tri: clic sur <em>Taille</em> / <em>Date</em></span>
            </div>
          </button>
          <div class="ms-2 scan-acc-meta">
            <button type="submit" class="btn btn-sm btn-outline-danger"
                    form="resetForm" onclick="setReset('global','')">RàZ le seeding</button>
            <input type="checkbox" onclick="toggleGroup('grp0', this.checked)" title="Tout (global)">
            <!-- placeholder invisible pour stabiliser la largeur vs autres sections -->
            <span class="small-muted d-none d-md-inline opacity-0">Règles: mixtes</span>
          </div>
        </div>
      </h2>
      <div id="c-0" class="accordion-collapse collapse" data-bs-parent="#acc-scan">
        <div class="accordion-body p-0">
          <div class="table-responsive">
            <table class="table table-sm mb-0 align-middle" id="tbl-global" data-sort-key="ts" data-sort-dir="desc">
              <thead class="table-light">
                <tr>
                  <th class="sticky-th" style="width:40px"></th>
                  <th class="sticky-th">Nom</th>
                  <th class="sticky-th">
                    <button type="button" class="btn btn-link p-0 text-reset sort-btn" data-table="tbl-global" data-key="size">Taille</button>
                    <span class="sort-hint" data-for="size"></span>
                  </th>
                  <th class="sticky-th">
                    <button type="button" class="btn btn-link p-0 text-reset sort-btn" data-table="tbl-global" data-key="ts">Date</button>
                    <span class="sort-hint" data-for="ts">▼</span>
                  </th>
                  <th class="sticky-th">Seeding</th>
                  <th class="sticky-th">Tracker</th>
                </tr>
              </thead>
              <tbody>
                {% for it in global_items %}
                <tr data-size="{{ it['size_b'] or 0 }}" data-ts="{{ it['ts'] or 0 }}">
                  <td><input type="checkbox" class="grp0" name="sel" value="{{ it['magnet'] }}||{{ it['tracker_host'] }}||{{ it['infohash'] }}||{{ it['json_path'] }}"></td>
                  <td class="name-cell">{{ it['name'] }}</td>
                  <td class="nowrap">{{ it['size_hr'] }}</td>
                  <td class="small nowrap">{{ it['date_hr'] }}</td>
                  <td class="small">
                    {% if it['live_seed'] %}
                      <a href="{{ it['live_client_url'] }}" target="_blank" rel="noopener" class="text-decoration-none">
                        <span class="badge bg-success">En seed</span>
                        <span class="small-muted">({{ it['live_client'] or it['sent_client'] }})</span>
                        {% if it['qbit_state'] %}<span class="small-muted">[{{ it['qbit_state'] }}]</span>{% endif %}
                      </a>
                    {% elif it['sent'] %}
                      <span class="badge bg-warning">Seedé</span> <span class="small-muted">({{ it['sent_client'] }})</span>
                    {% else %}
                      <span class="badge bg-danger">Jamais</span>
                    {% endif %}
                  </td>
                  <td class="small">{{ it['tracker_label'] }}</td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- Par tracker -->
    {% for label, grp in grouped.items() %}
    {% set gi = loop.index %}
    {% set anchor = 'grp-' ~ (label|slug) %}
    <div class="accordion-item" id="{{ anchor }}">
      <h2 class="accordion-header" id="h-{{ gi }}">
        <div class="scan-acc-header">
          <button class="accordion-button collapsed flex-grow-1 text-start"
                  type="button" data-bs-toggle="collapse" data-bs-target="#c-{{ gi }}">
            <div class="d-flex align-items-center gap-2">
              <span class="badge bg-dark tracker-badge">{{ label }}</span>
              <span class="small-muted">{{ grp['items']|length }} élément(s)</span>
              <span class="small-muted">Total: <strong>{{ grp.total_hr }}</strong></span>
              {% if grp.hosts and grp.hosts|length>1 %}
                <span class="small-muted">(regroupement: {{ grp.hosts|length }} URLs)</span>
              {% endif %}
            </div>
          </button>
          <div class="ms-2 scan-acc-meta">
            <button type="submit" class="btn btn-sm btn-outline-danger"
                    form="resetForm" onclick="setReset('label','{{ label }}')">RàZ le seeding</button>
            <input type="checkbox" onclick="toggleGroup('grp{{ gi }}', this.checked)" title="Tout (tracker)">
            <span class="small-muted">{% if grp.rule_summary %}{{ grp.rule_summary }}{% else %}Règles: mixtes{% endif %}</span>
          </div>
        </div>
      </h2>
      <div id="c-{{ gi }}" class="accordion-collapse collapse" data-bs-parent="#acc-scan">
        <div class="accordion-body p-0">
          <div class="table-responsive">
            <table class="table table-sm mb-0 align-middle" id="tbl-{{ gi }}" data-sort-key="ts" data-sort-dir="desc">
              <thead class="table-light">
                <tr>
                  <th class="sticky-th" style="width:40px"></th>
                  <th class="sticky-th">Nom</th>
                  <th class="sticky-th">
                    <button type="button" class="btn btn-link p-0 text-reset sort-btn" data-table="tbl-{{ gi }}" data-key="size">Taille</button>
                    <span class="sort-hint" data-for="size"></span>
                  </th>
                  <th class="sticky-th">
                    <button type="button" class="btn btn-link p-0 text-reset sort-btn" data-table="tbl-{{ gi }}" data-key="ts">Date</button>
                    <span class="sort-hint" data-for="ts">▼</span>
                  </th>
                  <th class="sticky-th">Seeding</th>
                  <th class="sticky-th">Tracker</th>
                </tr>
              </thead>
              <tbody>
                {% for it in grp['items'] %}
                <tr data-size="{{ it['size_b'] or 0 }}" data-ts="{{ it['ts'] or 0 }}">
                  <td><input type="checkbox" class="grp{{ gi }}" name="sel" value="{{ it['magnet'] }}||{{ it['tracker_host'] }}||{{ it['infohash'] }}||{{ it['json_path'] }}"></td>
                  <td class="name-cell">{{ it['name'] }}</td>
                  <td class="nowrap">{{ it['size_hr'] }}</td>
                  <td class="small nowrap">{{ it['date_hr'] }}</td>
                  <td class="small">
                    {% if it['live_seed'] %}
                      <a href="{{ it['live_client_url'] }}" target="_blank" rel="noopener" class="text-decoration-none">
                        <span class="badge bg-success">En seed</span>
                        <span class="small-muted">({{ it['live_client'] or it['sent_client'] }})</span>
                        {% if it['qbit_state'] %}<span class="small-muted">[{{ it['qbit_state'] }}]</span>{% endif %}
                      </a>
                    {% elif it['sent'] %}
                      <span class="badge bg-warning">Seedé</span> <span class="small-muted">({{ it['sent_client'] }})</span>
                    {% else %}
                      <span class="badge bg-danger">Jamais</span>
                    {% endif %}
                  </td>
                  <td class="small">{{ label }}</td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
</form>

<!-- formulaire caché dédié aux resets -->
<form id="resetForm" method="post" action="{{ url_for('reset_sent') }}" class="d-none">
  <input type="hidden" name="scope" id="resetScope" value="">
  <input type="hidden" name="label" id="resetLabel" value="">
</form>

<script>
document.getElementById('expandAllBtn')?.addEventListener('click', ()=>{
  document.querySelectorAll('#acc-scan .accordion-collapse').forEach(el=>{
    const c = bootstrap.Collapse.getOrCreateInstance(el, {toggle:false}); c.show();
  });
});
document.getElementById('collapseAllBtn')?.addEventListener('click', ()=>{
  document.querySelectorAll('#acc-scan .accordion-collapse').forEach(el=>{
    const c = bootstrap.Collapse.getOrCreateInstance(el, {toggle:false}); c.hide();
  });
});
</script>
{% endblock %}
"""

T_LOGS = """{% extends "base.html" %}
{% block content %}
<h3>Logs</h3>
<p class="small-muted">Flux en quasi temps réel (rafraîchi toutes les 2s).</p>
<pre class="logbox" id="logbox"></pre>
<script>
let cursor = 0;
async function pull(){
  try{
    const r = await fetch(`{{ url_for('logs_tail') }}?since=${cursor}`, {cache:'no-store'});
    const data = await r.json();
    if(Array.isArray(data.lines) && data.lines.length){
      const pre = document.getElementById('logbox');
      data.lines.forEach(l => pre.textContent += l + "\\n");
      pre.scrollTop = pre.scrollHeight;
      cursor = data.cursor || cursor;
    }
  }catch(e){}
}
setInterval(pull, 2000);
pull();
</script>
{% endblock %}
"""

# -------------------- App init --------------------
app = Flask(__name__)
import re as _re
app.jinja_env.filters['slug'] = lambda s: _re.sub(r'[^a-z0-9-]+', '-', (s or '').lower()).strip('-')
app.secret_key = os.environ.get("MCC_SECRET", os.urandom(24))
app.wsgi_app = ProxyFix(app.wsgi_app)
app.jinja_loader = DictLoader({
  "base.html": T_BASE,
  "login.html": T_LOGIN,
  "dashboard.html": T_DASH,
  "rules.html": T_RULES,
  "settings.html": T_SETTINGS,
  "scan.html": T_SCAN,
  "logs.html": T_LOGS,
})
app.logger.setLevel(logging.INFO)

# -------------------- qBit helpers --------------------
def qbt_client(row: dict) -> qbittorrentapi.Client:
    url = row["host"].rstrip("/")
    c = qbittorrentapi.Client(host=url, username=row.get("user") or "", password=row.get("pass") or "")
    c.auth_log_in()
    return c

def qbit_live_seed_map() -> Dict[str, dict]:
    """
    Map infohash(lower) -> {live: bool, client: str, state: str, ratio: float, category: str, url: str}
    live=True si présent et en état de seed/upload côté qBit.
    """
    out: Dict[str, dict] = {}
    for row in list_clients():
        try:
            c = qbt_client(row)
            for t in c.torrents_info():
                ih = (getattr(t, "hash", "") or "").lower()
                st = str(getattr(t, "state", "") or "")
                live = st.endswith("UP") or st in {"uploading","stalledUP","queuedUP","pausedUP","checkingUP","forcedUP"}
                out[ih] = {
                    "live": bool(live),
                    "client": row.get("name") or f"client#{row.get('id')}",
                    "state": st,
                    "ratio": float(getattr(t, "ratio", 0) or 0),
                    "category": (getattr(t, "category", "") or "").strip(),
                    "url": row.get("host") or "",
                }
        except Exception as e:
            app.logger.warning("qBit live map KO pour %s: %s", row.get("name"), e)
    return out

def qbit_live_counts_by_label(items: List[dict]) -> Dict[str, int]:
    """
    Compte les torrents ACTUELLEMENT EN SEED, uniquement parmi les JSON DecypharrSeed.
    Vérifie leur état dans les clients qBittorrent (live=True) sans jamais inclure d'autres torrents.
    """
    live_map = qbit_live_seed_map()
    out: Dict[str, int] = {}

    for it in items:
        ih = (it.get("infohash") or "").lower()
        lbl = it.get("tracker_host") or "(inconnu)"

        if not ih:
            continue

        if ih in live_map and live_map[ih].get("live"):
            out[lbl] = out.get(lbl, 0) + 1

    return out


# -------------------- Scan JSON --------------------
MAGNET_RE = re.compile(r"magnet:[^\s\"'<>]+", re.IGNORECASE)

def get_json_dirs() -> List[str]:
    v = get_setting("json_dirs")
    if isinstance(v, list) and v: return v
    return DEFAULT_JSON_DIRS

def parse_trackers_from_magnet(magnet: str) -> List[str]:
    try:
        from urllib.parse import urlsplit, parse_qs, unquote
        qs = parse_qs(urlsplit(magnet).query)
        trs = []
        for t in qs.get("tr", []):
            t = unquote(t)
            if t.startswith("http://") or t.startswith("https://"):
                host = urlsplit(t).hostname or ""
                if host: trs.append(host.lower())
        return trs
    except Exception:
        return []

def extract_infohash(data: dict, magnet: str) -> Optional[str]:
    ih = (data.get("info_hash") or data.get("infoHash") or "").lower()
    if ih: return ih
    m = re.search(r"xt=urn:btih:([a-fA-F0-9]{40})", magnet)
    if m: return m.group(1).lower()
    m = re.search(r"xt=urn:btih:([a-zA-Z0-9]{32})", magnet)  # base32
    if m: return m.group(1).lower()
    return None

def extract_size_bytes(data: dict) -> int:
    try:
        b = int(data.get("bytes") or 0)
        if b > 0: return b
    except: pass
    try:
        files = data.get("files") or {}
        if isinstance(files, dict):
            s = 0
            for v in files.values():
                sz = int(v.get("size") or 0)
                if sz > 0: s += sz
            if s>0: return s
    except: pass
    try:
        b = int(data.get("size") or 0)
        if b > 0: return b
    except: pass
    return 0

def label_for_hosts(hosts: List[str], rules: Dict[str,dict]) -> Tuple[str,str]:
    primary = hosts[0] if hosts else NO_TRACKER_LABEL
    for h in hosts:
        r = rules.get(h)
        if r and r.get("category"):
            return r.get("category"), h
    return primary, primary

def scan_jsons():
    dirs = get_json_dirs()
    files = []
    for d in dirs:
        p = Path(d)
        if not p.exists(): continue
        files.extend(p.glob("*.json"))

    rules = get_rules()
    last_hosts_set = set()

    grouped: Dict[str,dict] = {}
    global_items = []
    total_b = 0

    for jp in files:
        try:
            raw = jp.read_text(encoding="utf-8", errors="ignore")
            data = json.loads(raw)
        except Exception:
            continue

        magnet = data.get("link") or (data.get("magnet") or {}).get("link") or ""
        if not magnet or not magnet.startswith("magnet:"):
            m = MAGNET_RE.search(raw)
            magnet = m.group(0) if m else ""
        if not magnet: continue

        hosts = parse_trackers_from_magnet(magnet)
        if not hosts:
            hosts = [NO_TRACKER_LABEL]
        for h in hosts: last_hosts_set.add(h)

        label, rule_host = label_for_hosts(hosts, rules)

        name = data.get("name") or data.get("filename") or data.get("original_filename") \
               or (data.get("magnet") or {}).get("name") or "Sans nom"
        infohash = extract_infohash(data, magnet) or f"nohash_{jp.name}"
        size_b = extract_size_bytes(data)
        ts = int(Path(jp).stat().st_mtime)
        date_hr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

        item = {
            "name": name, "size_b": size_b, "size_hr": human(size_b),
            "ts": ts, "date_hr": date_hr,
            "magnet": magnet, "infohash": infohash, "json_path": str(jp),
            "tracker_host": rule_host, "tracker_label": label,
        }
        global_items.append(item)
        total_b += size_b

    set_setting("last_scan_hosts", sorted(last_hosts_set))

    s_map = sent_map()
    live_map = qbit_live_seed_map()

    for it in global_items:
        ih = it["infohash"].lower()
        if ih in s_map:
            cid, _t = s_map[ih]
            cname = next((c["name"] for c in list_clients() if c["id"]==cid), f"client#{cid}")
            it["sent"] = True; it["sent_client"] = cname
        else:
            it["sent"] = False; it["sent_client"] = ""

        if ih in live_map:
            it["live_seed"]  = bool(live_map[ih]["live"])
            it["qbit_state"] = live_map[ih]["state"]
            it["live_client"] = live_map[ih]["client"]
            it["live_client_url"] = live_map[ih].get("url") or ""
        else:
            it["live_seed"]  = False
            it["qbit_state"] = ""
            it["live_client"] = ""
            it["live_client_url"] = ""

    grouped = {}
    for it in global_items:
        lbl = it["tracker_label"]
        g = grouped.setdefault(lbl, {"items": [], "total": 0, "hosts": set(), "rule_summary": ""})
        g["items"].append(it)
        g["total"] += it["size_b"] or 0
        g["hosts"].add(it["tracker_host"])

    for lbl, g in grouped.items():
        g["total_hr"] = human(g["total"])
        r_list = [ get_rules().get(h) for h in g["hosts"] ]
        r_list = [r for r in r_list if r]
        if r_list:
            r0 = r_list[0]
            if all((r.get("ratio"), r.get("seed_days")) == (r0.get("ratio"), r0.get("seed_days")) for r in r_list):
                rtxt = []
                if r0.get("ratio") is not None: rtxt.append(f"ratio≥{r0['ratio']}")
                if r0.get("seed_days") is not None: rtxt.append(f"seed≤{r0['seed_days']}j")
                g["rule_summary"] = ("; ".join(rtxt)) if rtxt else ""

    summary = { "files": len(files), "items": len(global_items), "trackers": len(grouped), "total_b": total_b }
    return grouped, global_items, summary

# -------------------- Backup/Worker/Autosend --------------------
def purge_old_backups(outdir: Path, retention_days: int = 7):
    try:
        cutoff = time.time() - retention_days*86400
        for f in outdir.glob("magnetcc-*.sqlite"):
            try:
                if f.stat().st_mtime < cutoff: f.unlink()
            except Exception as e:
                logger.warning("BACKUP purge fail %s: %s", f, e)
    except Exception as e:
        logger.warning("BACKUP purge error: %s", e)

def do_backup():
    cfg = get_setting("backup_cfg", {"enabled": False, "dir": "/data/backup", "retention_days": 7})
    if not cfg.get("enabled"): return
    try:
        outdir = Path(cfg.get("dir") or "/data/backup"); outdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = outdir / f"magnetcc-{ts}.sqlite"
        data = Path(DB_PATH).read_bytes()
        dest.write_bytes(data)
        set_setting("backup_last", datetime.now().strftime("%Y-%m-%d %H:%M"))
        set_setting("backup_last_ts", now_ts())
        logger.info("BACKUP ✅ %s", dest)
        purge_old_backups(outdir, int(cfg.get("retention_days", 7)))
    except Exception as e:
        logger.error("BACKUP ❌ %s", e)

WORKER_THREAD = {"t": None, "stop": False}
def worker_loop():
    logger.info("WORKER ▶ start")
    while not WORKER_THREAD["stop"]:
        try:
            now = now_ts()
            bk = get_setting("backup_cfg", {"enabled": False, "dir": "/data/backup", "retention_days": 7})
            if bk.get("enabled"):
                last_bk = ensure_int(get_setting("backup_last_ts", 0), 0)
                if now - last_bk >= 86400: do_backup()
            cfg = get_setting("autoscan_cfg", {"enabled": False, "interval": 10})
            if cfg.get("enabled"):
                last_scan = ensure_int(get_setting("autoscan_last_ts", 0), 0)
                interval_sec = max(1, ensure_int(cfg.get("interval"), 10)) * 60
                if now - last_scan >= interval_sec:
                    grouped, items, summary = scan_jsons()
                    set_setting("autoscan_last", datetime.now().strftime("%Y-%m-%d %H:%M"))
                    set_setting("autoscan_last_ts", now)
                    logger.info("SCAN(auto) ✅ items=%d trackers=%d", len(items), len(grouped))
                    added = autosend_process(items)
                    if added: logger.info("AUTOSEND(auto) ✅ added=%d", added)
        except Exception as e:
            logger.error("WORKER loop error: %s", e)
        for _ in range(6):
            if WORKER_THREAD["stop"]: break
            time.sleep(10)
    logger.info("WORKER ■ stop")

def autosend_process(items: List[dict]):
    autosend = get_setting("autosend_cfg", {"global_enabled": False, "global_client": None, "map": {}})
    rules = get_rules()
    sent = sent_map()
    if not items: return 0
    added_total = 0
    global_client = autosend.get("global_client") if autosend.get("global_enabled") else None
    map_by_label = autosend.get("map") or {}
    clients = {c["id"]: c for c in list_clients()}
    def client_for(it):
        lbl = it["tracker_label"]
        cid = map_by_label.get(lbl) or global_client
        return clients.get(int(cid)) if cid else None
    for it in items:
        ih = it["infohash"].lower()
        if ih in sent: continue
        row_client = client_for(it)
        if not row_client: continue
        try:
            c = qbt_client(row_client)
        except Exception as e:
            logger.warning("Autosend: client KO %s: %s", row_client.get("name"), e)
            continue
        do_precheck = bool(int(row_client.get("precheck",1)))
        if do_precheck:
            try:
                md = c.sync.maindata()
                server_state = getattr(md,'server_state',None) or (md.get('server_state') if isinstance(md,dict) else None)
                free_b = int(getattr(server_state,'free_space_on_disk',None) or (server_state.get('free_space_on_disk') if server_state else 0))
                if (it["size_b"] or 0) > free_b:
                    logger.info("Autosend skip (space) %s need %s > free %s", ih, human(it["size_b"] or 0), human(free_b))
                    continue
            except Exception: pass
        rule = rules.get(it["tracker_host"], {})
        category = (rule.get("category") or it["tracker_label"] or it["tracker_host"].split('.')[0]).strip()
        try:
            # TAG automatique DecypharrSeed
            c.torrents_add(
                urls=it["magnet"],
                category=category,
                use_auto_torrent_management=True,
                tags="DecypharrSeed"
            )
            try:
                c.torrents_add_tags(tags="DecypharrSeed", hashes=it["infohash"].upper())
            except Exception:
                pass
            try:
                c.torrents_set_share_limits(
                    ratio_limit=float(rule.get('ratio') or 2.0),
                    seeding_time_limit=int(rule.get('seed_days') or 14)*24*60,
                    inactive_seeding_time_limit=-1,
                    hashes=it["infohash"].upper()
                )
            except Exception: pass
            record_sent(ih, row_client['id']); added_total += 1
            logger.info("Autosend ✅ %s -> %s", it["name"], row_client["name"])
        except Exception as e:
            logger.warning("Autosend ❌ %s: %s", it["name"], e)
    return added_total

# -------------------- Routes --------------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        if request.form.get("user")==LOGIN_USER and request.form.get("pass")==LOGIN_PASS:
            session["auth"]=True; return redirect(url_for('dashboard'))
        flash("Identifiants invalides", "error")
    return render_template("login.html")

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    grouped, items, summary = scan_jsons()

    # --- Agrégation par tracker d’origine (host) pour le graphe ---
    agg_scan: Dict[str, Dict[str, float]] = {}
    for it in items:
        th = it["tracker_host"] or "(inconnu)"
        d = agg_scan.setdefault(th, {"count": 0, "total": 0})
        d["count"] += 1
        d["total"] += it.get("size_b") or 0

    # Seeds ACTIFS par tracker d’origine (via infohash)
    counts_live_map = qbit_live_counts_by_label(items)  # {tracker_host: n_live}

    # Seeds GLOBAL (actif + historique) parmi les éléments du dernier scan
    counts_seed_global_map: Dict[str, int] = {}
    for it in items:
        th = it["tracker_host"] or "(inconnu)"
        if it.get("live_seed") or it.get("sent"):
            counts_seed_global_map[th] = counts_seed_global_map.get(th, 0) + 1

    # Labels et métriques
    labels = list(agg_scan.keys())
    # étend avec éventuels trackers présents côté live/global mais pas dans agg_scan
    for lbl in set(list(counts_live_map.keys()) + list(counts_seed_global_map.keys())):
        if lbl not in labels:
            labels.append(lbl)
            agg_scan.setdefault(lbl, {"count": 0, "total": 0})

    counts_scan = [ int(agg_scan[l]["count"]) for l in labels ]
    sizes_gb    = [ round((agg_scan[l]["total"] or 0)/(1024**3), 2) for l in labels ]
    counts_qbit = [ counts_live_map.get(lbl, 0) for lbl in labels ]
    counts_seed_global = [ counts_seed_global_map.get(lbl, 0) for lbl in labels ]

    # --- blocs de tableaux (inchangés dans leur logique) ---
    top_heavy = sorted(items, key=lambda x: x["size_b"] or 0, reverse=True)[:10]
    top_heavy = [{"name":t["name"], "label":t["tracker_label"], "size_hr":t["size_hr"],
                  "sent":t["sent"], "live_seed":t.get("live_seed", False)} for t in top_heavy]
    top_latest = sorted(items, key=lambda x: x["ts"] or 0, reverse=True)[:10]
    top_latest = [{"name":t["name"], "label":t["tracker_label"], "date_hr":t["date_hr"],
                   "sent":t["sent"], "live_seed":t.get("live_seed", False)} for t in top_latest]
    top3_by_tracker=[]
    for lbl,g in grouped.items():
        sub = sorted(g["items"], key=lambda x: x["ts"] or 0, reverse=True)[:3]
        for t in sub:
            top3_by_tracker.append({"label":lbl, "name":t["name"], "date_hr":t["date_hr"],
                                    "sent":t["sent"], "live_seed":t.get("live_seed", False)})
    dash = {
        "stats": {"trackers": len(grouped), "items": sum(len(g["items"]) for g in grouped.values()),
                  "total_hr": human(sum((g["total"] or 0) for g in grouped.values()))},
        "rules_count": len(get_rules()),
        "clients_count": len(list_clients()),
        "active_client": next((c["name"] for c in list_clients() if c["id"]==get_active_client_id()), None),
        "chart": {
            "labels":labels,
            "counts_scan":counts_scan,
            "counts_qbit":counts_qbit,
            "counts_seed_global": counts_seed_global,
            "sizes_gb":sizes_gb
        },
        "top_heaviest": top_heavy,
        "top_latest": top_latest,
        "top3_by_tracker": top3_by_tracker
    }
    return render_template("dashboard.html", dash=dash, json_dirs=get_json_dirs(), db_path=DB_PATH)

@app.route('/rules', methods=['GET','POST'])
@login_required
def rules():
    if request.method == 'POST':
        action = request.form.get("action","")
        if action == "add":
            host = (request.form.get("host") or "").strip().lower()
            if host:
                upsert_rule(host, request.form.get("category") or "", request.form.get("ratio") or None, request.form.get("seed_days") or None)
                flash("Règle ajoutée", "success")
            return redirect(url_for('rules'))
        elif action == "del":
            h = request.form.get("del"); 
            if h: del_rule(h)
            return redirect(url_for('rules'))
        elif action == "save":
            for k in request.form:
                if not k.startswith("host_"): continue
                idx = k.split("_",1)[1]
                h   = (request.form.get(f"host_{idx}") or "").strip().lower()
                cat = request.form.get(f"cat_{idx}") or ""
                ratio = request.form.get(f"ratio_{idx}") or None
                seed  = request.form.get(f"seed_{idx}") or None
                if h: upsert_rule(h, cat, ratio, seed)
            flash("Règles enregistrées", "success")
            return redirect(url_for('rules'))
        elif action == "add_from_scan":
            last_hosts = get_setting("last_scan_hosts", [])
            added=0
            for h in last_hosts:
                if h not in get_rules():
                    upsert_rule(h, "", None, None); added+=1
            flash(f"{added} host(s) ajoutés depuis le dernier scan.", "success")
            return redirect(url_for('rules'))
    # auto-populate depuis le dernier scan (GET)
    try:
        last_hosts = get_setting("last_scan_hosts", []) or []
        if last_hosts:
            existing = set(get_rules().keys())
            added_auto = 0
            for h in last_hosts:
                if h and h != NO_TRACKER_LABEL and h not in existing:
                    upsert_rule(h, "", None, None)
                    added_auto += 1
            if added_auto:
                logger.info("RULES auto-populate: %d host(s) ajoutés depuis le dernier scan", added_auto)
    except Exception as e:
        logger.warning("RULES auto-populate erreur: %s", e)
    rules_list = list(get_rules().values())
    last_hosts = get_setting("last_scan_hosts", [])
    return render_template("rules.html", rules=rules_list, last_hosts=last_hosts)

@app.route('/settings', methods=['GET','POST'])
@login_required
def settings():
    autoscan_cfg = {"enabled": bool(get_setting("autoscan_cfg", {}).get("enabled", False)),
                    "interval": int(get_setting("autoscan_cfg", {}).get("interval", 10)),
                    "last": get_setting("autoscan_last", None)}
    autosend_cfg = get_setting("autosend_cfg", {"global_enabled": False, "global_client": None, "map": {}})
    backup_cfg = get_setting("backup_cfg", {"enabled": False, "dir": "/data/backup", "retention_days": 7})
    if request.method == 'POST':
        action = request.form.get("action","")
        if action == "save_json_dirs":
            dirs = [ln.strip() for ln in (request.form.get("json_dirs") or "").splitlines() if ln.strip()]
            if not dirs: dirs = DEFAULT_JSON_DIRS
            set_setting("json_dirs", dirs); flash("Dossiers JSON enregistrés", "success")
        elif action == "add_qbit":
            name = request.form.get("name") or ""
            host = request.form.get("host") or ""
            user = request.form.get("user") or ""
            pw   = request.form.get("pass") or ""
            pre  = 1 if request.form.get("precheck") else 0
            with closing(db()) as con:
                con.execute("INSERT INTO clients(name,host,user,pass,precheck) VALUES (?,?,?,?,?)",(name, host, user, pw, pre))
                con.commit()
            flash("Client qBittorrent ajouté", "success")
        elif action == "del_qbit":
            cid = ensure_int(request.form.get("id"))
            with closing(db()) as con:
                con.execute("DELETE FROM clients WHERE id=?", (cid,))
                con.commit()
            flash("Client supprimé", "success")
        elif action == "toggle_precheck":
            cid = ensure_int(request.form.get("id"))
            with closing(db()) as con:
                cur = con.execute("SELECT precheck FROM clients WHERE id=?", (cid,))
                row = cur.fetchone()
                if row:
                    newv = 0 if row["precheck"] else 1
                    con.execute("UPDATE clients SET precheck=? WHERE id=?", (newv, cid))
                    con.commit()
            return redirect(url_for('settings'))
        elif action == "save_autoscan":
            enabled = bool(request.form.get("autoscan_enabled"))
            interval = ensure_int(request.form.get("autoscan_interval"), 10)
            set_setting("autoscan_cfg", {"enabled": enabled, "interval": interval})
            flash("Scan régulier enregistré", "success")
        elif action == "save_autosend_global":
            enabled = bool(request.form.get("as_global_enabled"))
            client  = ensure_int(request.form.get("as_global_client"), 0) or None
            cfg = get_setting("autosend_cfg", {"global_enabled": False, "global_client": None, "map": {}})
            cfg["global_enabled"]=enabled; cfg["global_client"]=client
            set_setting("autosend_cfg", cfg)
            flash("Auto-envoi global enregistré", "success")
        elif action == "save_autosend_trackers":
            cfg = get_setting("autosend_cfg", {"global_enabled": False, "global_client": None, "map": {}})
            newmap={}
            for k,v in request.form.items():
                if not k.startswith("map__"): continue
                label = k.split("__",1)[1]
                cid = ensure_int(v, 0)
                if cid: newmap[label]=cid
            cfg["map"]=newmap; set_setting("autosend_cfg", cfg)
            flash("Mappages tracker→client enregistrés", "success")
        elif action == "save_backup":
            enabled = bool(request.form.get("bk_enabled"))
            bk_dir  = request.form.get("bk_dir") or "/data/backup"
            set_setting("backup_cfg", {"enabled": enabled, "dir": bk_dir, "retention_days": 7})
            flash("Backup BDD: paramètres enregistrés", "success")
        elif action == "backup_now":
            do_backup(); flash("Backup déclenché", "success")
        return redirect(url_for('settings'))

    grouped, items, summary = scan_jsons()
    tracker_labels = sorted(grouped.keys())
    return render_template("settings.html",
                           json_dirs=get_json_dirs(),
                           clients=list_clients(),
                           autoscan=autoscan_cfg,
                           autosend={"global_enabled": autosend_cfg.get("global_enabled",False),
                                     "global_client": autosend_cfg.get("global_client"),
                                     "map": autosend_cfg.get("map",{})},
                           backup={"enabled": backup_cfg.get("enabled",False),
                                   "dir": backup_cfg.get("dir","/data/backup"),
                                   "last": get_setting("backup_last", None)},
                           tracker_labels=tracker_labels)

@app.route('/scan')
@login_required
def scan():
    grouped, global_items, summary = scan_jsons()
    if request.args.get("do"):
        flash(f"Scan terminé : {summary['items']} items sur {summary['trackers']} trackers.", "success")
    total_b = sum((it["size_b"] or 0) for it in global_items)
    grouped = dict(sorted(grouped.items(), key=lambda kv: kv[0]))
    return render_template("scan.html",
                           grouped=grouped,
                           global_items=global_items,
                           global_total_hr=human(total_b),
                           active_id=get_active_client_id(),
                           clients=list_clients())

@app.route('/enqueue', methods=['POST'])
@login_required
def enqueue():
    sel = request.form.getlist('sel')
    cid = ensure_int(request.form.get('client_id'))
    if not sel:
        flash("Aucune sélection","warning"); return redirect(url_for('scan'))
    client_row = next((c for c in list_clients() if c['id']==cid), None)
    if not client_row:
        flash("Client introuvable","error"); return redirect(url_for('scan'))
    try:
        c = qbt_client(client_row)
    except Exception as e:
        flash(f"Connexion qBittorrent échouée: {e}","error"); return redirect(url_for('scan'))

    do_precheck = bool(int(client_row.get('precheck',1)))
    free_b = None
    if do_precheck:
        try:
            md = c.sync.maindata()
            server_state = getattr(md,'server_state',None) or (md.get('server_state') if isinstance(md,dict) else None)
            free_b = int(getattr(server_state,'free_space_on_disk',None) or (server_state.get('free_space_on_disk') if server_state else 0))
        except Exception as e:
            app.logger.warning("precheck: free_space_on_disk KO (%s)", e)

    already = sent_map()
    total_needed = 0; unknown = 0; parsed=[]
    for packed in sel:
        try:
            magnet, host, ih, jp = packed.split("||",3)
        except ValueError:
            continue
        parsed.append((magnet, host, ih, jp))
        try:
            data = json.loads(Path(jp).read_text(encoding="utf-8", errors="ignore"))
        except:
            data = {}
        sz = extract_size_bytes(data)
        if sz>0: total_needed += sz
        else: unknown += 1

    if do_precheck and free_b is not None:
        if total_needed>free_b:
            flash(f"Espace insuffisant — Besoin: {human(total_needed)} · Libre: {human(free_b)} · Inconnues: {unknown}. Rien envoyé.", 'error')
            return redirect(url_for('scan'))
        else:
            msg=f"Pré-check espace OK — À envoyer: {human(total_needed)} ≤ Libre: {human(free_b)}"
            if unknown: msg+=f" (attention: {unknown} sans taille connue)"
            flash(msg,'success')
    elif do_precheck and free_b is None:
        flash("Pré-check espace indisponible (API qBittorrent). Envoi poursuivi.", 'warning')

    rules = get_rules()
    added = 0
    for magnet, tracker_host, ih, _jp in parsed:
        if ih.lower() in already:
            app.logger.info("skip (déjà envoyée): %s", ih)
            continue
        rule = rules.get(tracker_host, {})
        category = (rule.get("category") or tracker_host.split(".")[0]).strip()
        try:
            c.torrents_add(
                urls=magnet,
                category=category,
                use_auto_torrent_management=True,
                tags="DecypharrSeed"
            )
            try:
                c.torrents_add_tags(tags="DecypharrSeed", hashes=ih.upper())
            except Exception:
                pass
            try:
                c.torrents_set_share_limits(
                    ratio_limit=float(rule.get('ratio') or 2.0),
                    seeding_time_limit=int(rule.get('seed_days') or 14)*24*60,
                    inactive_seeding_time_limit=-1,
                    hashes=ih.upper()
                )
            except Exception: pass
            record_sent(ih, client_row['id'])
            added += 1
        except Exception as e:
            flash(f"Ajout échoué pour {ih[:8]}…: {e}", 'error')

    flash(f"Ajouts envoyés: {added}", 'success' if added else 'warning')
    return redirect(url_for('scan'))

@app.route('/reset_sent', methods=['POST'])
@login_required
def reset_sent():
    scope = request.form.get("scope")
    if scope == "global":
        n = delete_sent_all()
        flash(f"État réinitialisé pour {n} release(s) (global).", "success")
        return redirect(url_for('scan'))
    elif scope == "label":
        label = request.form.get("label") or ""
        grouped, items, summary = scan_jsons()
        target = [it["infohash"] for it in grouped.get(label, {}).get("items", [])]
        n = delete_sent_by_infohashes(target)
        flash(f"État réinitialisé pour {n} release(s) du tracker « {label} ».","success")
        return redirect(url_for('scan'))
    flash("Paramètre de réinitialisation invalide.","error")
    return redirect(url_for('scan'))

# -------------------- Logs --------------------
@app.route('/logs')
@login_required
def logs():
    return render_template("logs.html")
@app.route('/logs/tail')
@login_required
def logs_tail():
    since = ensure_int(request.args.get("since"), 0)
    lines = [f"{r['msg']}" for r in list(LOG_RING) if r["seq"]>since]
    cur = LOG_SEQ["n"]
    return jsonify({"lines": lines, "cursor": cur})

# -------------------- Run --------------------
def start_worker_once():
    if WORKER_THREAD["t"] is not None: return
    WORKER_THREAD["stop"] = False
    t = threading.Thread(target=worker_loop, daemon=True)
    WORKER_THREAD["t"] = t; t.start()
start_worker_once()

if __name__ == "__main__":
    if os.environ.get("MCC_WAITRESS"):
        from waitress import serve
        serve(app, host=APP_HOST, port=APP_PORT)
    else:
        app.run(host=APP_HOST, port=APP_PORT, debug=False)
