#!/usr/bin/env python3
"""
CEC-Sonos Bridge - Web Server
Runs in both AP mode and Bridge mode:
  - AP Mode: Setup wizard for WiFi and Sonos configuration
  - Bridge Mode: Admin panel for updates, settings, and control

Access at: http://sonosbridge.local

Hardware: Raspberry Pi Zero 2 W
"""

import os
import sys
import json
import time
import signal
import subprocess
import logging
import shutil
import urllib.request
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from datetime import datetime

# Setup logging
LOG_FILE = '/var/log/cec-sonos-bridge.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Configuration
APP_DIR = '/opt/cec-sonos-bridge'
CONFIG_FILE = f'{APP_DIR}/config.json'
VERSION_FILE = f'{APP_DIR}/version.json'
BACKUP_DIR = f'{APP_DIR}/backups'
SCAN_CACHE_FILE = f'{APP_DIR}/wifi_scan.json'
SONOS_CACHE_FILE = f'{APP_DIR}/sonos_scan.json'
WIFI_CREDS_FILE = f'{APP_DIR}/wifi_creds_temp.json'

# GitHub configuration !
GITHUB_REPO = 'rhetoricdesigns/cec-sonos-bridge'
GITHUB_RAW = f'https://raw.githubusercontent.com/{GITHUB_REPO}'
GITHUB_API = f'https://api.github.com/repos/{GITHUB_REPO}'

UPDATE_FILES = ['startup.py', 'ap_mode.py', 'cec_bridge.py', 'web_server.py']

WEB_PORT = 80

# Current mode - set by whoever starts this server
CURRENT_MODE = 'bridge'  # 'ap' or 'bridge'


# ============================================================
# VERSION / UPDATE FUNCTIONS
# ============================================================

def get_current_version():
    try:
        with open(VERSION_FILE) as f:
            data = json.load(f)
            return data.get('version', '1.0.0')
    except:
        return '1.0.0'


def get_version_info():
    try:
        with open(VERSION_FILE) as f:
            return json.load(f)
    except:
        return {'version': '1.0.0', 'updated': 'unknown'}


def save_version_info(version, changelog=''):
    os.makedirs(APP_DIR, exist_ok=True)
    info = {
        'version': version,
        'updated': datetime.now().isoformat(),
        'changelog': changelog
    }
    with open(VERSION_FILE, 'w') as f:
        json.dump(info, f, indent=2)


def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'CEC-Sonos-Bridge'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        log.error(f"Error fetching {url}: {e}")
        return None


def get_latest_release():
    content = fetch_url(f'{GITHUB_API}/releases/latest')
    if content:
        try:
            data = json.loads(content)
            return {
                'version': data.get('tag_name', '').lstrip('v'),
                'changelog': data.get('body', ''),
                'url': data.get('html_url', '')
            }
        except:
            pass
    return None


def get_available_versions():
    content = fetch_url(f'{GITHUB_API}/releases')
    if content:
        try:
            releases = json.loads(content)
            return [{'version': r.get('tag_name', '').lstrip('v'), 
                     'name': r.get('name', ''),
                     'date': r.get('published_at', '')[:10]} for r in releases[:10]]
        except:
            pass
    return []


def get_backups():
    if not os.path.exists(BACKUP_DIR):
        return []
    backups = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True)[:10]:
        version_file = f'{BACKUP_DIR}/{name}/version.txt'
        version = 'unknown'
        if os.path.exists(version_file):
            with open(version_file) as f:
                version = f.read().strip()
        backups.append({'name': name, 'version': version})
    return backups


def backup_current():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f'{BACKUP_DIR}/{timestamp}'
    os.makedirs(backup_path, exist_ok=True)
    
    for filename in UPDATE_FILES:
        src = f'{APP_DIR}/{filename}'
        if os.path.exists(src):
            shutil.copy2(src, f'{backup_path}/{filename}')
    
    version = get_current_version()
    with open(f'{backup_path}/version.txt', 'w') as f:
        f.write(version)
    
    log.info(f"Backup created: {backup_path}")
    return backup_path


def download_version(version):
    log.info(f"Downloading version {version}...")
    branch = f'v{version}' if version != 'main' else 'main'
    
    for filename in UPDATE_FILES:
        url = f'{GITHUB_RAW}/{branch}/{filename}'
        content = fetch_url(url)
        if content:
            filepath = f'{APP_DIR}/{filename}'
            with open(filepath, 'w') as f:
                f.write(content)
            log.info(f"Downloaded: {filename}")
        else:
            log.error(f"Failed to download: {filename}")
            return False
    return True


def do_update(target_version=None):
    current = get_current_version()
    
    if target_version:
        version = target_version.lstrip('v')
    else:
        latest = get_latest_release()
        if not latest:
            return False, "Could not fetch latest version"
        version = latest['version']
    
    if version == current:
        return True, f"Already at version {version}"
    
    backup_current()
    
    if download_version(version):
        save_version_info(version)
        return True, f"Updated to version {version}. Restarting..."
    else:
        return False, "Update failed"


def do_rollback(backup_name=None):
    if not os.path.exists(BACKUP_DIR):
        return False, "No backups found"
    
    backups = sorted(os.listdir(BACKUP_DIR), reverse=True)
    if not backups:
        return False, "No backups found"
    
    if backup_name:
        if backup_name not in backups:
            return False, f"Backup {backup_name} not found"
        backup_path = f'{BACKUP_DIR}/{backup_name}'
    else:
        backup_path = f'{BACKUP_DIR}/{backups[0]}'
    
    for filename in UPDATE_FILES:
        src = f'{backup_path}/{filename}'
        if os.path.exists(src):
            shutil.copy2(src, f'{APP_DIR}/{filename}')
    
    version_file = f'{backup_path}/version.txt'
    if os.path.exists(version_file):
        with open(version_file) as f:
            version = f.read().strip()
        save_version_info(version, 'Rolled back')
    
    return True, "Rollback complete. Restarting..."


# ============================================================
# CONFIG FUNCTIONS
# ============================================================

def get_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except:
        return {}


def save_config(config):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def get_service_status():
    try:
        result = subprocess.run(['systemctl', 'is-active', 'cec-sonos-bridge'],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except:
        return 'unknown'


def restart_service():
    subprocess.Popen(['systemctl', 'restart', 'cec-sonos-bridge'])


# ============================================================
# HTML TEMPLATES
# ============================================================

ADMIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sonos Bridge Admin</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            padding: 20px;
            color: #fff;
        }
        .container { max-width: 500px; margin: 0 auto; }
        .card {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 16px;
        }
        .logo { text-align: center; font-size: 48px; margin-bottom: 8px; }
        h1 { text-align: center; font-size: 24px; margin-bottom: 8px; }
        h2 { font-size: 18px; margin-bottom: 16px; color: #00d4aa; }
        .subtitle { text-align: center; color: rgba(255,255,255,0.7); font-size: 14px; margin-bottom: 20px; }
        .status-row { display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.1); }
        .status-label { color: rgba(255,255,255,0.7); }
        .status-value { font-weight: 600; }
        .status-active { color: #28a745; }
        .status-inactive { color: #dc3545; }
        button {
            width: 100%; padding: 14px; border: none; border-radius: 10px;
            font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 12px;
        }
        .btn-primary { background: #00d4aa; color: #1a1a2e; }
        .btn-primary:hover { background: #00f5c4; }
        .btn-secondary { background: rgba(255,255,255,0.2); color: #fff; }
        .btn-danger { background: #dc3545; color: #fff; }
        .btn-warning { background: #ffc107; color: #1a1a2e; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .version-list { max-height: 200px; overflow-y: auto; }
        .version-item {
            padding: 12px; border: 1px solid rgba(255,255,255,0.2); border-radius: 8px;
            margin-bottom: 8px; cursor: pointer;
        }
        .version-item:hover { border-color: #00d4aa; }
        .version-item.current { border-color: #28a745; background: rgba(40,167,69,0.2); }
        .version-name { font-weight: 600; }
        .version-date { font-size: 12px; color: rgba(255,255,255,0.6); }
        .alert { padding: 12px; border-radius: 8px; margin-bottom: 16px; }
        .alert-success { background: rgba(40,167,69,0.2); border: 1px solid #28a745; }
        .alert-error { background: rgba(220,53,69,0.2); border: 1px solid #dc3545; }
        .alert-info { background: rgba(0,123,255,0.2); border: 1px solid #007bff; }
        .hidden { display: none; }
        .tabs { display: flex; margin-bottom: 16px; }
        .tab { flex: 1; padding: 12px; text-align: center; cursor: pointer; border-bottom: 2px solid transparent; }
        .tab.active { border-bottom-color: #00d4aa; color: #00d4aa; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        select {
            width: 100%; padding: 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.2);
            background: rgba(255,255,255,0.1); color: #fff; font-size: 16px;
        }
        select option { background: #1a1a2e; }
        .spinner {
            width: 20px; height: 20px; border: 2px solid rgba(255,255,255,0.2);
            border-top-color: #00d4aa; border-radius: 50%; animation: spin 1s linear infinite;
            display: inline-block; margin-right: 8px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="logo"></div>
            <h1>Sonos Bridge</h1>
            <p class="subtitle">Admin Panel</p>
        </div>

        <div id="alertBox" class="hidden"></div>

        <div class="card">
            <h2>Status</h2>
            <div class="status-row">
                <span class="status-label">Service</span>
                <span class="status-value" id="serviceStatus">Loading...</span>
            </div>
            <div class="status-row">
                <span class="status-label">Speaker</span>
                <span class="status-value" id="speakerName">Loading...</span>
            </div>
            <div class="status-row">
                <span class="status-label">IP Address</span>
                <span class="status-value" id="speakerIp">Loading...</span>
            </div>
            <div class="status-row">
                <span class="status-label">HDMI Port</span>
                <span class="status-value" id="hdmiPort">Loading...</span>
            </div>
            <div class="status-row">
                <span class="status-label">Version</span>
                <span class="status-value" id="currentVersion">Loading...</span>
            </div>
            <button class="btn-secondary" onclick="testVolume()">Test Volume</button>
            <button class="btn-secondary" onclick="restartService()">Restart Service</button>
        </div>

        <div class="card">
            <div class="tabs">
                <div class="tab active" onclick="showTab('updates')">Updates</div>
                <div class="tab" onclick="showTab('rollback')">Rollback</div>
                <div class="tab" onclick="showTab('settings')">Settings</div>
            </div>

            <div id="updates" class="tab-content active">
                <div id="updateStatus" class="alert alert-info hidden"></div>
                <div id="updateAvailable" class="hidden">
                    <p style="margin-bottom:12px">New version available: <strong id="latestVersion"></strong></p>
                    <button class="btn-primary" onclick="doUpdate()">Update Now</button>
                </div>
                <div id="upToDate" class="hidden">
                    <p style="color: #28a745;">&#10004; You're running the latest version</p>
                </div>
                <button class="btn-secondary" onclick="checkUpdates()">Check for Updates</button>
            </div>

            <div id="rollback" class="tab-content">
                <p style="margin-bottom:12px; color: rgba(255,255,255,0.7);">Restore a previous version:</p>
                <select id="backupSelect">
                    <option value="">Select a backup...</option>
                </select>
                <button class="btn-warning" onclick="doRollback()">Rollback</button>
            </div>

            <div id="settings" class="tab-content">
                <button class="btn-secondary" onclick="window.location.href='/setup'">Run Setup Wizard</button>
                <button class="btn-secondary" onclick="viewLogs()">View Logs</button>
                <button class="btn-danger" onclick="factoryReset()">Factory Reset</button>
            </div>
        </div>
    </div>

<script>
function showTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('.tab-content#' + name).classList.add('active');
    event.target.classList.add('active');
}

function showAlert(message, type) {
    var box = document.getElementById('alertBox');
    box.className = 'alert alert-' + type;
    box.textContent = message;
    box.classList.remove('hidden');
    setTimeout(function() { box.classList.add('hidden'); }, 5000);
}

function loadStatus() {
    fetch('/api/admin/status')
        .then(r => r.json())
        .then(data => {
            document.getElementById('serviceStatus').textContent = data.service_status || 'unknown';
            document.getElementById('serviceStatus').className = 'status-value ' + 
                (data.service_status === 'active' ? 'status-active' : 'status-inactive');
            document.getElementById('speakerName').textContent = data.speaker_name || 'Not configured';
            document.getElementById('speakerIp').textContent = data.speaker_ip || '-';
            document.getElementById('hdmiPort').textContent = data.hdmi_port ? 'HDMI ' + data.hdmi_port : '-';
            document.getElementById('currentVersion').textContent = 'v' + (data.version || '1.0.0');
        });
}

function checkUpdates() {
    document.getElementById('updateStatus').textContent = 'Checking for updates...';
    document.getElementById('updateStatus').classList.remove('hidden');
    
    fetch('/api/admin/check-updates')
        .then(r => r.json())
        .then(data => {
            document.getElementById('updateStatus').classList.add('hidden');
            if (data.update_available) {
                document.getElementById('latestVersion').textContent = 'v' + data.latest_version;
                document.getElementById('updateAvailable').classList.remove('hidden');
                document.getElementById('upToDate').classList.add('hidden');
            } else {
                document.getElementById('updateAvailable').classList.add('hidden');
                document.getElementById('upToDate').classList.remove('hidden');
            }
        })
        .catch(e => {
            document.getElementById('updateStatus').textContent = 'Could not check for updates';
            document.getElementById('updateStatus').className = 'alert alert-error';
        });
}

function doUpdate() {
    if (!confirm('Update to the latest version? The service will restart.')) return;
    
    showAlert('Updating...', 'info');
    fetch('/api/admin/update', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showAlert(data.message, 'success');
                setTimeout(() => location.reload(), 5000);
            } else {
                showAlert(data.message || 'Update failed', 'error');
            }
        });
}

function loadBackups() {
    fetch('/api/admin/backups')
        .then(r => r.json())
        .then(data => {
            var select = document.getElementById('backupSelect');
            select.innerHTML = '<option value="">Select a backup...</option>';
            data.forEach(b => {
                select.innerHTML += '<option value="' + b.name + '">' + b.name + ' (v' + b.version + ')</option>';
            });
        });
}

function doRollback() {
    var backup = document.getElementById('backupSelect').value;
    if (!backup) {
        showAlert('Please select a backup', 'error');
        return;
    }
    if (!confirm('Rollback to this version? The service will restart.')) return;
    
    fetch('/api/admin/rollback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({backup: backup})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
            setTimeout(() => location.reload(), 5000);
        } else {
            showAlert(data.message || 'Rollback failed', 'error');
        }
    });
}

function testVolume() {
    fetch('/api/admin/test-volume', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showAlert('Volume test sent!', 'success');
            } else {
                showAlert('Test failed: ' + (data.error || 'Unknown error'), 'error');
            }
        });
}

function restartService() {
    if (!confirm('Restart the CEC bridge service?')) return;
    fetch('/api/admin/restart', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            showAlert('Service restarting...', 'info');
            setTimeout(loadStatus, 3000);
        });
}

function viewLogs() {
    window.open('/api/logs', '_blank');
}

function factoryReset() {
    if (!confirm('This will erase all settings and restart in setup mode. Continue?')) return;
    if (!confirm('Are you sure? This cannot be undone!')) return;
    
    fetch('/api/admin/factory-reset', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            showAlert('Factory reset complete. Rebooting...', 'info');
        });
}

loadStatus();
loadBackups();
</script>
</body>
</html>
"""

# ============================================================
# SETUP PAGE (reuse from ap_mode or simplified version)
# ============================================================

SETUP_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>Sonos Bridge Setup</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            padding: 20px;
            color: #fff;
        }
        .container { max-width: 420px; margin: 0 auto; }
        .card {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 16px;
        }
        .logo { text-align: center; font-size: 48px; margin-bottom: 8px; }
        h1 { text-align: center; font-size: 24px; margin-bottom: 8px; }
        .subtitle { text-align: center; color: rgba(255,255,255,0.7); font-size: 14px; margin-bottom: 20px; }
        .form-group { margin-bottom: 16px; }
        label { display: block; margin-bottom: 8px; color: rgba(255,255,255,0.9); font-size: 14px; }
        select, input {
            width: 100%; padding: 14px 16px; border: 2px solid rgba(255,255,255,0.2);
            border-radius: 10px; background: rgba(255,255,255,0.1); color: #fff; font-size: 16px;
        }
        select option { background: #1a1a2e; color: #fff; }
        button {
            width: 100%; padding: 16px; border: none; border-radius: 10px;
            font-size: 16px; font-weight: 600; cursor: pointer;
        }
        .btn-primary { background: #00d4aa; color: #1a1a2e; }
        .btn-secondary { background: rgba(255,255,255,0.2); color: #fff; margin-top: 12px; }
        .status { padding: 12px; border-radius: 8px; margin-top: 12px; font-size: 14px; }
        .status.success { background: rgba(40,167,69,0.2); border: 1px solid #28a745; }
        .status.error { background: rgba(220,53,69,0.2); border: 1px solid #dc3545; }
        .speaker-item {
            padding: 14px; border: 2px solid rgba(255,255,255,0.2); border-radius: 10px;
            margin-bottom: 8px; cursor: pointer;
        }
        .speaker-item:hover { border-color: rgba(255,255,255,0.4); }
        .speaker-item.selected { border-color: #00d4aa; background: rgba(0,212,170,0.1); }
        .speaker-name { font-weight: 600; }
        .speaker-info { font-size: 12px; color: rgba(255,255,255,0.6); }
        .hidden { display: none; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="logo"></div>
            <h1>Sonos Bridge Setup</h1>
            <p class="subtitle">Select your Sonos speaker</p>
        </div>

        <div class="card">
            <div class="form-group">
                <label>Available Speakers:</label>
                <div id="speakerList">Loading...</div>
            </div>

            <div class="form-group">
                <label>HDMI Port:</label>
                <select id="hdmiPort">
                    <option value="1">HDMI 1</option>
                    <option value="2" selected>HDMI 2 (Recommended)</option>
                    <option value="3">HDMI 3</option>
                    <option value="4">HDMI 4</option>
                </select>
            </div>

            <button class="btn-primary" onclick="saveSettings()">Save Settings</button>
            <button class="btn-secondary" onclick="window.location.href='/'">Cancel</button>
            
            <div id="statusMsg"></div>
        </div>
    </div>

<script>
var selectedSpeaker = null;
var speakers = [];

function loadSpeakers() {
    fetch('/api/admin/scan-speakers', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            speakers = data.speakers || [];
            var list = document.getElementById('speakerList');
            if (speakers.length === 0) {
                list.innerHTML = '<div class="status error">No speakers found</div>';
                return;
            }
            var html = '';
            for (var i = 0; i < speakers.length; i++) {
                var s = speakers[i];
                html += '<div class="speaker-item" data-index="' + i + '">';
                html += '<div class="speaker-name">' + s.name + '</div>';
                html += '<div class="speaker-info">' + s.model + ' - ' + s.ip + '</div>';
                html += '</div>';
            }
            list.innerHTML = html;
            
            document.querySelectorAll('.speaker-item').forEach(item => {
                item.addEventListener('click', function() {
                    document.querySelectorAll('.speaker-item').forEach(i => i.classList.remove('selected'));
                    this.classList.add('selected');
                    selectedSpeaker = speakers[parseInt(this.dataset.index)];
                });
            });
        });
}

function saveSettings() {
    if (!selectedSpeaker) {
        document.getElementById('statusMsg').innerHTML = '<div class="status error">Please select a speaker</div>';
        return;
    }
    
    fetch('/api/admin/save-settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            speaker_ip: selectedSpeaker.ip,
            speaker_name: selectedSpeaker.name,
            hdmi_port: document.getElementById('hdmiPort').value
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            document.getElementById('statusMsg').innerHTML = '<div class="status success">Settings saved! Redirecting...</div>';
            setTimeout(() => window.location.href = '/', 2000);
        } else {
            document.getElementById('statusMsg').innerHTML = '<div class="status error">Save failed</div>';
        }
    });
}

loadSpeakers();
</script>
</body>
</html>
"""


# ============================================================
# WEB SERVER
# ============================================================

class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.info(f"HTTP: {args[0]}")
    
    def send_html(self, html, status=200):
        content = html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)
    
    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def do_GET(self):
        path = urlparse(self.path).path
        
        # Main page - show admin panel in bridge mode
        if path == '/' or path == '/index.html':
            self.send_html(ADMIN_PAGE_HTML)
            return
        
        # Setup page
        if path == '/setup':
            self.send_html(SETUP_PAGE_HTML)
            return
        
        # API: Get status
        if path == '/api/admin/status':
            config = get_config()
            self.send_json({
                'service_status': get_service_status(),
                'speaker_name': config.get('speaker_name'),
                'speaker_ip': config.get('speaker_ip'),
                'hdmi_port': config.get('hdmi_port'),
                'version': get_current_version(),
                'mode': CURRENT_MODE
            })
            return
        
        # API: Check for updates
        if path == '/api/admin/check-updates':
            current = get_current_version()
            latest = get_latest_release()
            if latest:
                self.send_json({
                    'current_version': current,
                    'latest_version': latest['version'],
                    'update_available': latest['version'] > current,
                    'changelog': latest.get('changelog', '')
                })
            else:
                self.send_json({
                    'current_version': current,
                    'update_available': False,
                    'error': 'Could not check for updates'
                })
            return
        
        # API: Get backups
        if path == '/api/admin/backups':
            self.send_json(get_backups())
            return
        
        # API: View logs
        if path == '/api/logs':
            try:
                with open(LOG_FILE, 'r') as f:
                    lines = f.readlines()[-100:]
                self.send_html('<html><head><meta name="viewport" content="width=device-width"><title>Logs</title></head><body style="background:#1a1a2e;color:#0f0;font-family:monospace;font-size:12px;padding:10px;white-space:pre-wrap;">' + ''.join(lines) + '</body></html>')
            except Exception as e:
                self.send_html(f'Error: {e}')
            return
        
        self.send_response(404)
        self.end_headers()
    
    def do_POST(self):
        path = urlparse(self.path).path
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
        try:
            data = json.loads(body)
        except:
            data = {}
        
        # API: Update
        if path == '/api/admin/update':
            success, message = do_update(data.get('version'))
            self.send_json({'success': success, 'message': message})
            if success:
                Thread(target=lambda: (time.sleep(2), restart_service()), daemon=True).start()
            return
        
        # API: Rollback
        if path == '/api/admin/rollback':
            success, message = do_rollback(data.get('backup'))
            self.send_json({'success': success, 'message': message})
            if success:
                Thread(target=lambda: (time.sleep(2), restart_service()), daemon=True).start()
            return
        
        # API: Restart service
        if path == '/api/admin/restart':
            Thread(target=restart_service, daemon=True).start()
            self.send_json({'success': True})
            return
        
        # API: Test volume
        if path == '/api/admin/test-volume':
            config = get_config()
            ip = config.get('speaker_ip')
            if ip:
                try:
                    import soco
                    speaker = soco.SoCo(ip)
                    original = speaker.volume
                    speaker.volume = min(original + 5, 100)
                    time.sleep(0.3)
                    speaker.volume = original
                    self.send_json({'success': True})
                except Exception as e:
                    self.send_json({'success': False, 'error': str(e)})
            else:
                self.send_json({'success': False, 'error': 'No speaker configured'})
            return
        
        # API: Scan speakers
        if path == '/api/admin/scan-speakers':
            try:
                import soco
                discovered = soco.discover(timeout=10) or []
                speakers = []
                for s in discovered:
                    try:
                        info = s.get_speaker_info()
                        speakers.append({
                            'name': s.player_name,
                            'ip': s.ip_address,
                            'model': info.get('model_name', 'Sonos')
                        })
                    except:
                        pass
                self.send_json({'speakers': speakers})
            except Exception as e:
                self.send_json({'speakers': [], 'error': str(e)})
            return
        
        # API: Save settings
        if path == '/api/admin/save-settings':
            config = get_config()
            config['speaker_ip'] = data.get('speaker_ip')
            config['speaker_name'] = data.get('speaker_name')
            config['hdmi_port'] = data.get('hdmi_port')
            save_config(config)
            self.send_json({'success': True})
            return
        
        # API: Factory reset
        if path == '/api/admin/factory-reset':
            try:
                os.remove(CONFIG_FILE)
            except:
                pass
            self.send_json({'success': True})
            Thread(target=lambda: (time.sleep(2), os.system('reboot')), daemon=True).start()
            return
        
        self.send_response(404)
        self.end_headers()


def run_server(port=WEB_PORT):
    """Run the web server."""
    server = HTTPServer(('0.0.0.0', port), WebHandler)
    log.info(f"Admin web server running on port {port}")
    log.info(f"Access at: http://sonosbridge.local")
    server.serve_forever()


if __name__ == '__main__':
    run_server()
