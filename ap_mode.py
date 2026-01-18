#!/usr/bin/env python3
"""
CEC-Sonos Bridge - Access Point Mode
Creates WiFi hotspot and serves setup wizard for:
  1. WiFi network selection
  2. Sonos speaker selection  
  3. HDMI port configuration

Uses NetworkManager (nmcli) for Bookworm compatibility.
Pre-scans WiFi networks before starting hotspot.

Hardware: Raspberry Pi Zero 2 W
"""

import os
import sys
import json
import time
import signal
import subprocess
import logging
import socket
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import re

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
SCAN_CACHE_FILE = f'{APP_DIR}/wifi_scan.json'
SONOS_CACHE_FILE = f'{APP_DIR}/sonos_scan.json'
WIFI_CREDS_FILE = f'{APP_DIR}/wifi_creds_temp.json'

HOTSPOT_SSID = "SonosBridge-Setup"
HOTSPOT_PASSWORD = "sonosbridge"
HOTSPOT_IP = "192.168.4.1"
HOTSPOT_CON_NAME = "SonosBridge-Hotspot"

AP_TIMEOUT = 600
WEB_PORT = 80


SETUP_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
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
        .step-header { display: flex; align-items: center; margin-bottom: 16px; }
        .step-number {
            width: 28px; height: 28px; background: #00d4aa; color: #1a1a2e;
            border-radius: 50%; display: flex; align-items: center; justify-content: center;
            font-weight: bold; font-size: 14px; margin-right: 12px;
        }
        .step-number.done { background: #28a745; }
        .step-title { font-size: 18px; font-weight: 600; }
        .form-group { margin-bottom: 16px; }
        label { display: block; margin-bottom: 8px; color: rgba(255,255,255,0.9); font-size: 14px; }
        select, input[type="text"], input[type="password"] {
            width: 100%; padding: 14px 16px; border: 2px solid rgba(255,255,255,0.2);
            border-radius: 10px; background: rgba(255,255,255,0.1); color: #fff; font-size: 16px;
        }
        select:focus, input:focus { outline: none; border-color: #00d4aa; }
        select option { background: #1a1a2e; color: #fff; }
        .network-list { max-height: 200px; overflow-y: auto; margin-bottom: 12px; }
        .network-item, .speaker-item {
            padding: 14px; border: 2px solid rgba(255,255,255,0.2); border-radius: 10px;
            margin-bottom: 8px; cursor: pointer; transition: all 0.2s;
        }
        .network-item:hover, .speaker-item:hover { border-color: rgba(255,255,255,0.4); background: rgba(255,255,255,0.05); }
        .network-item.selected, .speaker-item.selected { border-color: #00d4aa; background: rgba(0,212,170,0.1); }
        .network-name, .speaker-name { font-weight: 600; margin-bottom: 4px; }
        .network-signal, .speaker-info { font-size: 12px; color: rgba(255,255,255,0.6); }
        .manual-input { display: none; margin-top: 12px; }
        .manual-input.show { display: block; }
        .link-btn { background: none; border: none; color: #00d4aa; font-size: 14px; cursor: pointer; text-decoration: underline; }
        button { width: 100%; padding: 16px; border: none; border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
        .btn-primary { background: #00d4aa; color: #1a1a2e; }
        .btn-primary:hover { background: #00f5c4; }
        .btn-primary:disabled { background: rgba(255,255,255,0.2); color: rgba(255,255,255,0.5); cursor: not-allowed; }
        .btn-secondary { background: rgba(255,255,255,0.2); color: #fff; margin-top: 8px; }
        .btn-test { background: #28a745; color: #fff; margin-bottom: 12px; }
        .btn-scan { background: #007bff; color: #fff; margin-top: 8px; }
        .status { padding: 12px; border-radius: 8px; margin-top: 12px; font-size: 14px; }
        .status.success { background: rgba(40,167,69,0.2); border: 1px solid #28a745; }
        .status.error { background: rgba(220,53,69,0.2); border: 1px solid #dc3545; }
        .status.info { background: rgba(0,123,255,0.2); border: 1px solid #007bff; }
        .loading { text-align: center; padding: 20px; color: rgba(255,255,255,0.7); }
        .spinner {
            width: 24px; height: 24px; border: 3px solid rgba(255,255,255,0.2);
            border-top-color: #00d4aa; border-radius: 50%; animation: spin 1s linear infinite;
            display: inline-block; margin-right: 8px; vertical-align: middle;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .warning { background: rgba(255,193,7,0.2); border: 1px solid #ffc107; color: #fff; padding: 12px; border-radius: 8px; font-size: 13px; margin-top: 12px; }
        .success-screen { text-align: center; padding: 40px 20px; }
        .success-icon { font-size: 64px; margin-bottom: 20px; color: #28a745; }
        .countdown { font-size: 48px; font-weight: bold; color: #00d4aa; margin: 20px 0; }
        .hidden { display: none; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="logo">&#128266;</div>
            <h1>Sonos Bridge Setup</h1>
            <p class="subtitle">Control your Sonos with your TV remote<br><small style="opacity:0.6">http://sonosbridge.local</small></p>
        </div>

        <div class="card" id="step1">
            <div class="step-header">
                <div class="step-number" id="step1-num">1</div>
                <div class="step-title">Connect to WiFi</div>
            </div>
            <div class="form-group">
                <label>Select your home network:</label>
                <div class="network-list" id="networkList">
                    <div class="loading"><span class="spinner"></span>Scanning...</div>
                </div>
                <button type="button" class="link-btn" id="manualWifiBtn">Enter network name manually</button>
                <div class="manual-input" id="manualWifi">
                    <input type="text" id="manualSsid" placeholder="Network name (SSID)">
                </div>
            </div>
            <div class="form-group">
                <label for="wifiPassword">WiFi Password:</label>
                <input type="password" id="wifiPassword" placeholder="Enter password">
                <button type="button" class="link-btn" id="togglePassword" style="margin-top: 8px;">Show Password</button>
            </div>
            <button type="button" class="btn-scan" id="scanSonosBtn">Scan for Sonos Speakers</button>
            <input type="hidden" id="selectedSsid" value="">
        </div>

        <div class="card" id="step2">
            <div class="step-header">
                <div class="step-number" id="step2-num">2</div>
                <div class="step-title">Select Sonos Speaker</div>
            </div>
            <div class="form-group">
                <label>Available speakers:</label>
                <div id="speakerList">
                    <div class="status info">Enter WiFi password above and click "Scan for Sonos Speakers"</div>
                </div>
            </div>
            <button type="button" class="btn-test hidden" id="testBtn">Test Volume</button>
            <input type="hidden" id="selectedSpeakerIp" value="">
            <input type="hidden" id="selectedSpeakerName" value="">
        </div>

        <div class="card" id="step3">
            <div class="step-header">
                <div class="step-number" id="step3-num">3</div>
                <div class="step-title">HDMI Port</div>
            </div>
            <div class="form-group">
                <label>Which HDMI port will the Pi use?</label>
                <select id="hdmiPort">
                    <option value="1">HDMI 1</option>
                    <option value="2" selected>HDMI 2 (Recommended)</option>
                    <option value="3">HDMI 3</option>
                    <option value="4">HDMI 4</option>
                </select>
                <div class="warning"><strong>Warning:</strong> Avoid the ARC/eARC port (usually HDMI 3) - it may not work correctly</div>
            </div>
        </div>

        <button type="button" class="btn-primary" id="saveBtn">Complete Setup</button>
        <div id="statusMessage"></div>

        <div class="card hidden" id="successScreen">
            <div class="success-screen">
                <div class="success-icon">&#10004;</div>
                <h1>Setup Complete!</h1>
                <p>Connecting to your WiFi network...</p>
                <div class="countdown" id="countdown">5</div>
                <p style="color: rgba(255,255,255,0.7); font-size: 14px;">
                    The device will restart and begin working.<br>You can close this page.
                </p>
            </div>
        </div>
    </div>

<script>
var selectedNetwork = null;
var selectedSpeaker = null;
var wifiConnected = false;
var speakersLoaded = false;
var networkData = [];
var speakerData = [];

function getSignalBars(signal) {
    if (signal > 80) return "****";
    if (signal > 60) return "*** ";
    if (signal > 40) return "**  ";
    return "*   ";
}

function showStatus(message, type) {
    var el = document.getElementById("statusMessage");
    el.innerHTML = "<div class='status " + type + "'>" + message + "</div>";
    setTimeout(function() { el.innerHTML = ""; }, 5000);
}

function selectNetwork(index) {
    var items = document.querySelectorAll(".network-item");
    for (var i = 0; i < items.length; i++) {
        items[i].classList.remove("selected");
    }
    items[index].classList.add("selected");
    selectedNetwork = networkData[index].ssid;
    document.getElementById("selectedSsid").value = selectedNetwork;
    document.getElementById("wifiPassword").focus();
}

function selectSpeaker(index) {
    var items = document.querySelectorAll(".speaker-item");
    for (var i = 0; i < items.length; i++) {
        items[i].classList.remove("selected");
    }
    items[index].classList.add("selected");
    selectedSpeaker = speakerData[index];
    document.getElementById("selectedSpeakerIp").value = selectedSpeaker.ip;
    document.getElementById("selectedSpeakerName").value = selectedSpeaker.name;
    document.getElementById("testBtn").classList.remove("hidden");
    document.getElementById("step2-num").classList.add("done");
    document.getElementById("step2-num").textContent = "OK";
}

function renderSpeakers(speakers) {
    speakerData = speakers;
    var speakerList = document.getElementById("speakerList");
    
    if (!speakers || speakers.length === 0) {
        speakerList.innerHTML = "<div class='status error'>No Sonos speakers found. Make sure they are on the same network.</div>";
        return;
    }
    
    var html = "";
    for (var i = 0; i < speakers.length; i++) {
        var s = speakers[i];
        html += "<div class='speaker-item' data-index='" + i + "'>";
        html += "<div class='speaker-name'>" + s.name + "</div>";
        html += "<div class='speaker-info'>" + s.model + " - " + s.ip + "</div>";
        html += "</div>";
    }
    speakerList.innerHTML = html;
    speakersLoaded = true;
    
    var items = document.querySelectorAll(".speaker-item");
    for (var j = 0; j < items.length; j++) {
        items[j].addEventListener("click", function() {
            var idx = parseInt(this.getAttribute("data-index"));
            selectSpeaker(idx);
        });
    }
    
    document.getElementById("step1-num").classList.add("done");
    document.getElementById("step1-num").textContent = "OK";
}

function loadNetworks() {
    fetch("/api/networks")
        .then(function(res) { return res.json(); })
        .then(function(networks) {
            networkData = networks;
            var list = document.getElementById("networkList");
            
            if (!networks || networks.length === 0) {
                list.innerHTML = "<div class='status info'>No networks found. Enter manually below.</div>";
                document.getElementById("manualWifi").classList.add("show");
                return;
            }
            
            var html = "";
            for (var i = 0; i < networks.length; i++) {
                var n = networks[i];
                var sec = n.security || "Open";
                html += "<div class='network-item' data-index='" + i + "'>";
                html += "<div class='network-name'>" + n.ssid + "</div>";
                html += "<div class='network-signal'>" + getSignalBars(n.signal) + " " + sec + "</div>";
                html += "</div>";
            }
            list.innerHTML = html;
            
            var items = document.querySelectorAll(".network-item");
            for (var j = 0; j < items.length; j++) {
                items[j].addEventListener("click", function() {
                    var idx = parseInt(this.getAttribute("data-index"));
                    selectNetwork(idx);
                });
            }
        })
        .catch(function(e) {
            document.getElementById("networkList").innerHTML = "<div class='status error'>Failed to load networks</div>";
        });
}

function loadCachedSpeakers() {
    console.log("Loading cached speakers...");
    fetch("/api/speakers")
        .then(function(res) { return res.json(); })
        .then(function(speakers) {
            console.log("Cached speakers:", speakers);
            if (speakers && speakers.length > 0) {
                renderSpeakers(speakers);
            } else {
                console.log("No cached speakers found");
            }
        })
        .catch(function(e) {
            console.log("Error loading cached speakers:", e);
        });
}

function loadCachedWifiCreds() {
    fetch("/api/wifi-creds")
        .then(function(res) { return res.json(); })
        .then(function(creds) {
            if (creds && creds.ssid) {
                document.getElementById("selectedSsid").value = creds.ssid;
                document.getElementById("wifiPassword").value = creds.password || "";
                
                // Highlight the matching network in the list
                setTimeout(function() {
                    var items = document.querySelectorAll(".network-item");
                    for (var i = 0; i < items.length; i++) {
                        if (networkData[i] && networkData[i].ssid === creds.ssid) {
                            items[i].classList.add("selected");
                            selectedNetwork = creds.ssid;
                            break;
                        }
                    }
                    // Mark step 1 as done if we have creds
                    document.getElementById("step1-num").classList.add("done");
                    document.getElementById("step1-num").textContent = "OK";
                }, 500);
            }
        })
        .catch(function(e) {
            // No cached creds, that's fine
        });
}

function testWifiAndLoadSpeakers() {
    var ssid = document.getElementById("manualSsid").value || document.getElementById("selectedSsid").value;
    var password = document.getElementById("wifiPassword").value;
    
    if (!ssid) {
        showStatus("Please select a WiFi network first", "error");
        return;
    }
    
    if (!password) {
        showStatus("Please enter your WiFi password", "error");
        return;
    }
    
    var speakerList = document.getElementById("speakerList");
    var countdown = 60;
    speakerList.innerHTML = "<div class='loading'><span class='spinner'></span>Scanning for Sonos speakers...<br><br><strong>Reconnect to SonosBridge-Setup in: <span id='countdown'>" + countdown + "</span> seconds</strong><br><br>The hotspot will disconnect while scanning.<br>Wait for the countdown, then reconnect and visit:<br><strong>http://sonosbridge.local</strong></div>";
    
    document.getElementById("scanSonosBtn").disabled = true;
    document.getElementById("scanSonosBtn").textContent = "Scanning...";
    
    // Start countdown timer
    var countdownEl = document.getElementById("countdown");
    var timer = setInterval(function() {
        countdown--;
        if (countdownEl) {
            countdownEl.textContent = countdown;
        }
        if (countdown <= 0) {
            clearInterval(timer);
            speakerList.innerHTML = "<div class='status info'><strong>Scan complete!</strong><br><br>1. Reconnect to <strong>SonosBridge-Setup</strong> WiFi<br>2. Refresh this page<br><br>Your Sonos speakers should appear.</div>";
            document.getElementById("scanSonosBtn").disabled = false;
            document.getElementById("scanSonosBtn").textContent = "Scan Again";
        }
    }, 1000);
    
    fetch("/api/test-wifi-and-scan", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ssid: ssid, password: password})
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        clearInterval(timer);
        document.getElementById("scanSonosBtn").disabled = false;
        document.getElementById("scanSonosBtn").textContent = "Scan for Sonos Speakers";
        
        if (!data.wifi_success) {
            speakerList.innerHTML = "<div class='status error'>WiFi connection failed: " + (data.error || "Check password") + "</div>";
            return;
        }
        
        wifiConnected = true;
        renderSpeakers(data.speakers);
    })
    .catch(function(e) {
        // Connection lost during scan - this is expected
        // Timer will show reconnect message
    });
}

function testVolume() {
    if (!selectedSpeaker) {
        showStatus("Please select a speaker first", "error");
        return;
    }
    
    fetch("/api/test-volume", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ip: selectedSpeaker.ip, name: selectedSpeaker.name})
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.success) {
            showStatus("Volume test successful! Did you hear it?", "success");
        } else {
            showStatus("Volume test failed: " + (data.error || "Unknown error"), "error");
        }
    })
    .catch(function(e) {
        showStatus("Test failed. Check connection.", "error");
    });
}

function saveConfig() {
    var ssid = document.getElementById("manualSsid").value || document.getElementById("selectedSsid").value;
    var password = document.getElementById("wifiPassword").value;
    var speakerIp = document.getElementById("selectedSpeakerIp").value;
    var speakerName = document.getElementById("selectedSpeakerName").value;
    var hdmiPort = document.getElementById("hdmiPort").value;
    
    if (!ssid) {
        showStatus("Please select a WiFi network", "error");
        return;
    }
    
    if (!speakerIp) {
        showStatus("Please select a Sonos speaker first", "error");
        return;
    }
    
    document.getElementById("saveBtn").disabled = true;
    document.getElementById("saveBtn").textContent = "Saving...";
    
    fetch("/api/save", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            ssid: ssid,
            password: password,
            speaker_ip: speakerIp,
            speaker_name: speakerName,
            hdmi_port: hdmiPort
        })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.success) {
            var cards = document.querySelectorAll(".card");
            for (var i = 0; i < cards.length; i++) {
                cards[i].classList.add("hidden");
            }
            document.getElementById("saveBtn").classList.add("hidden");
            document.getElementById("successScreen").classList.remove("hidden");
            
            var count = 5;
            var countdown = document.getElementById("countdown");
            var timer = setInterval(function() {
                count--;
                countdown.textContent = count;
                if (count <= 0) {
                    clearInterval(timer);
                    countdown.textContent = "Restarting...";
                }
            }, 1000);
        } else {
            showStatus("Save failed: " + (data.error || "Unknown error"), "error");
            document.getElementById("saveBtn").disabled = false;
            document.getElementById("saveBtn").textContent = "Complete Setup";
        }
    })
    .catch(function(e) {
        showStatus("Save failed. Please try again.", "error");
        document.getElementById("saveBtn").disabled = false;
        document.getElementById("saveBtn").textContent = "Complete Setup";
    });
}

document.getElementById("manualWifiBtn").addEventListener("click", function() {
    var manual = document.getElementById("manualWifi");
    if (manual.classList.contains("show")) {
        manual.classList.remove("show");
    } else {
        manual.classList.add("show");
        document.getElementById("manualSsid").focus();
    }
});

document.getElementById("togglePassword").addEventListener("click", function() {
    var pwd = document.getElementById("wifiPassword");
    if (pwd.type === "password") {
        pwd.type = "text";
        this.textContent = "Hide Password";
    } else {
        pwd.type = "password";
        this.textContent = "Show Password";
    }
});

document.getElementById("scanSonosBtn").addEventListener("click", testWifiAndLoadSpeakers);
document.getElementById("testBtn").addEventListener("click", testVolume);
document.getElementById("saveBtn").addEventListener("click", saveConfig);

// Load networks and any cached speakers on page load
loadNetworks();
loadCachedSpeakers();
loadCachedWifiCreds();
</script>
</body>
</html>
"""


def run_cmd(cmd, timeout=30):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def prescan_wifi_networks():
    log.info("Pre-scanning WiFi networks...")
    run_cmd(f'nmcli connection down "{HOTSPOT_CON_NAME}"')
    time.sleep(1)
    run_cmd('nmcli device wifi rescan')
    time.sleep(3)
    
    success, output = run_cmd('nmcli -t -f SSID,SIGNAL,SECURITY device wifi list')
    
    networks = []
    seen_ssids = set()
    
    if success and output:
        for line in output.split('\n'):
            parts = line.split(':')
            if len(parts) >= 2 and parts[0]:
                ssid = parts[0].strip()
                if ssid and ssid not in seen_ssids:
                    seen_ssids.add(ssid)
                    try:
                        signal = int(parts[1]) if parts[1] else 0
                    except:
                        signal = 0
                    security = parts[2] if len(parts) > 2 else ''
                    networks.append({'ssid': ssid, 'signal': signal, 'security': security})
    
    networks.sort(key=lambda x: x['signal'], reverse=True)
    
    os.makedirs(APP_DIR, exist_ok=True)
    with open(SCAN_CACHE_FILE, 'w') as f:
        json.dump(networks, f)
    
    log.info(f"Found {len(networks)} WiFi networks")
    return networks


def get_cached_networks():
    try:
        with open(SCAN_CACHE_FILE) as f:
            return json.load(f)
    except:
        return []


def get_cached_speakers():
    try:
        log.info(f"Reading cached speakers from {SONOS_CACHE_FILE}")
        with open(SONOS_CACHE_FILE) as f:
            speakers = json.load(f)
            log.info(f"Loaded {len(speakers)} cached speakers")
            return speakers
    except FileNotFoundError:
        log.info("No speaker cache file found")
        return []
    except Exception as e:
        log.error(f"Error reading speaker cache: {e}")
        return []


def save_cached_speakers(speakers):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        log.info(f"Saving {len(speakers)} speakers to {SONOS_CACHE_FILE}")
        with open(SONOS_CACHE_FILE, 'w') as f:
            json.dump(speakers, f)
        log.info("Speaker cache saved successfully")
    except Exception as e:
        log.error(f"Failed to cache speakers: {e}")


def get_cached_wifi_creds():
    try:
        with open(WIFI_CREDS_FILE) as f:
            return json.load(f)
    except:
        return {}


def save_cached_wifi_creds(ssid, password):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(WIFI_CREDS_FILE, 'w') as f:
            json.dump({'ssid': ssid, 'password': password}, f)
    except Exception as e:
        log.error(f"Failed to cache wifi creds: {e}")


def create_hotspot():
    log.info(f"Creating hotspot: {HOTSPOT_SSID}")
    
    run_cmd(f'nmcli connection delete "{HOTSPOT_CON_NAME}"')
    time.sleep(1)
    run_cmd('nmcli device disconnect wlan0')
    time.sleep(1)
    
    cmd = f'''nmcli connection add type wifi ifname wlan0 mode ap con-name "{HOTSPOT_CON_NAME}" ssid "{HOTSPOT_SSID}" autoconnect no wifi.band bg wifi.channel 6 ipv4.method shared ipv4.addresses {HOTSPOT_IP}/24 ipv6.method disabled wifi-sec.key-mgmt wpa-psk wifi-sec.psk "{HOTSPOT_PASSWORD}"'''
    
    success, output = run_cmd(cmd)
    if not success:
        log.error(f"Failed to create hotspot connection: {output}")
        return False
    
    time.sleep(1)
    
    success, output = run_cmd(f'nmcli connection up "{HOTSPOT_CON_NAME}"')
    if not success:
        log.error(f"Failed to activate hotspot: {output}")
        return False
    
    # Restart avahi to advertise on the new interface
    run_cmd('systemctl restart avahi-daemon')
    
    log.info(f"Hotspot active: {HOTSPOT_SSID} (password: {HOTSPOT_PASSWORD})")
    log.info(f"Access at: http://sonosbridge.local or http://{HOTSPOT_IP}")
    return True


def stop_hotspot():
    log.info("Stopping hotspot...")
    run_cmd(f'nmcli connection down "{HOTSPOT_CON_NAME}"')
    run_cmd(f'nmcli connection delete "{HOTSPOT_CON_NAME}"')


def test_wifi_connection(ssid, password):
    log.info(f"Testing WiFi connection to: {ssid}")
    log.info(f"Password length: {len(password)} characters")
    
    stop_hotspot()
    time.sleep(2)
    
    # Delete ALL existing connections for this SSID to avoid conflicts
    log.info("Cleaning up old connections...")
    run_cmd(f'nmcli connection delete "{ssid}"')
    run_cmd(f'nmcli connection delete "SonosBridge-WiFi"')
    run_cmd(f'nmcli connection delete "SonosBridge-Temp"')
    time.sleep(1)
    
    # Create connection with explicit WPA-PSK security type
    try:
        log.info(f"Creating WiFi connection for {ssid}...")
        if password:
            result = subprocess.run(
                ['nmcli', 'connection', 'add',
                 'type', 'wifi',
                 'con-name', 'SonosBridge-Temp',
                 'ssid', ssid,
                 'wifi-sec.key-mgmt', 'wpa-psk',
                 'wifi-sec.psk', password],
                capture_output=True, text=True, timeout=30
            )
        else:
            result = subprocess.run(
                ['nmcli', 'connection', 'add',
                 'type', 'wifi',
                 'con-name', 'SonosBridge-Temp',
                 'ssid', ssid],
                capture_output=True, text=True, timeout=30
            )
        
        log.info(f"Connection add: {result.returncode} - {result.stdout} {result.stderr}")
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Failed to create connection"
            log.error(f"Failed to create connection: {error_msg}")
            create_hotspot()
            return False, error_msg
        
        # Now activate the connection
        log.info("Activating connection...")
        result = subprocess.run(
            ['nmcli', 'connection', 'up', 'SonosBridge-Temp'],
            capture_output=True, text=True, timeout=60
        )
        
        log.info(f"Connection up: {result.returncode} - {result.stdout} {result.stderr}")
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Connection failed"
            log.error(f"WiFi connection failed: {error_msg}")
            create_hotspot()
            return False, error_msg
            
    except subprocess.TimeoutExpired:
        log.error("WiFi connection timed out")
        create_hotspot()
        return False, "Connection timed out"
    except Exception as e:
        log.error(f"WiFi connection error: {e}")
        create_hotspot()
        return False, str(e)
    
    # Wait for IP address
    log.info("WiFi connected, waiting for IP address...")
    for attempt in range(15):
        success, ip_output = run_cmd('hostname -I')
        log.info(f"hostname -I returned: '{ip_output}'")
        if ip_output and ip_output.strip():
            ip = ip_output.split()[0]
            if ip.startswith('192.168') or ip.startswith('10.') or ip.startswith('172.'):
                log.info(f"Got IP address: {ip}")
                return True, None
        
        log.info(f"Waiting for IP address... attempt {attempt + 1}/15")
        time.sleep(2)
    
    log.error("WiFi connected but no IP after 30 seconds")
    create_hotspot()
    return False, "Connected but no IP address"


def scan_sonos_speakers():
    log.info("Scanning for Sonos speakers...")
    speakers = []
    
    try:
        import soco
        log.info("SoCo imported successfully, starting discovery...")
        discovered = soco.discover(timeout=10) or []
        log.info(f"Discovery returned {len(discovered)} devices")
        
        for speaker in discovered:
            try:
                info = speaker.get_speaker_info()
                speaker_info = {
                    'name': speaker.player_name,
                    'ip': speaker.ip_address,
                    'model': info.get('model_name', 'Sonos'),
                    'is_coordinator': speaker.is_coordinator
                }
                speakers.append(speaker_info)
                log.info(f"Found speaker: {speaker_info}")
            except Exception as e:
                log.warning(f"Error getting speaker info: {e}")
        
        speakers.sort(key=lambda x: (not x.get('is_coordinator', False), x['name']))
        
        # Cache the speakers
        log.info(f"Caching {len(speakers)} speakers...")
        save_cached_speakers(speakers)
        
        # Verify cache was written
        cached = get_cached_speakers()
        log.info(f"Verified cache contains {len(cached)} speakers")
        
    except ImportError:
        log.error("SoCo library not installed")
    except Exception as e:
        log.error(f"Sonos discovery error: {e}")
    
    log.info(f"Returning {len(speakers)} Sonos speakers")
    return speakers


def test_sonos_volume(ip):
    try:
        import soco
        speaker = soco.SoCo(ip)
        original = speaker.volume
        speaker.volume = min(original + 5, 100)
        time.sleep(0.3)
        speaker.volume = original
        return True, None
    except Exception as e:
        return False, str(e)


def save_configuration(ssid, password, speaker_ip, speaker_name, hdmi_port):
    log.info("Saving configuration...")
    
    config = {
        'speaker_name': speaker_name,
        'speaker_ip': speaker_ip,
        'hdmi_port': hdmi_port,
        'wifi_ssid': ssid
    }
    
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    
    # Delete old WiFi connections
    run_cmd('nmcli connection delete "SonosBridge-WiFi"')
    run_cmd('nmcli connection delete "SonosBridge-Temp"')
    
    # Create permanent WiFi connection with explicit WPA-PSK
    if password:
        subprocess.run(
            ['nmcli', 'connection', 'add',
             'type', 'wifi',
             'con-name', 'SonosBridge-WiFi',
             'ssid', ssid,
             'wifi-sec.key-mgmt', 'wpa-psk',
             'wifi-sec.psk', password,
             'connection.autoconnect', 'yes'],
            capture_output=True, text=True
        )
    else:
        subprocess.run(
            ['nmcli', 'connection', 'add',
             'type', 'wifi',
             'con-name', 'SonosBridge-WiFi',
             'ssid', ssid,
             'connection.autoconnect', 'yes'],
            capture_output=True, text=True
        )
    
    log.info("Configuration saved!")
    return True


class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.info(f"HTTP: {args[0]}")
    
    def send_html(self, html, status=200):
        content = html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(content))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(content)
    
    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)
    
    def do_GET(self):
        path = urlparse(self.path).path
        
        if path == '/' or path == '/index.html':
            self.send_html(SETUP_PAGE_HTML)
            return
        
        # iOS captive portal detection
        if path in ['/hotspot-detect.html', '/library/test/success.html']:
            # Return something other than "Success" to trigger portal
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><head><meta http-equiv="refresh" content="0;url=http://192.168.4.1/"></head></html>')
            return
        
        # Android captive portal detection
        if path in ['/generate_204', '/gen_204']:
            self.send_response(302)
            self.send_header('Location', 'http://192.168.4.1/')
            self.end_headers()
            return
        
        # Windows/Firefox/other captive portal detection
        if path in ['/ncsi.txt', '/connecttest.txt', '/success.txt', '/redirect', '/canonical.html']:
            self.send_response(302)
            self.send_header('Location', 'http://192.168.4.1/')
            self.end_headers()
            return
        
        if path == '/api/networks':
            networks = get_cached_networks()
            self.send_json(networks)
            return
        
        if path == '/api/speakers':
            speakers = get_cached_speakers()
            self.send_json(speakers)
            return
        
        if path == '/api/wifi-creds':
            creds = get_cached_wifi_creds()
            self.send_json(creds)
            return
        
        if path == '/api/logs':
            try:
                with open(LOG_FILE, 'r') as f:
                    lines = f.readlines()
                    last_lines = lines[-50:] if len(lines) > 50 else lines
                    self.send_html('<html><head><meta name="viewport" content="width=device-width"></head><body style="background:#1a1a2e;color:#0f0;font-family:monospace;font-size:12px;padding:10px;white-space:pre-wrap;">' + ''.join(last_lines) + '</body></html>')
            except Exception as e:
                self.send_html(f'<html><body>Error reading log: {e}</body></html>')
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
        
        if path == '/api/test-wifi-and-scan':
            ssid = data.get('ssid', '')
            password = data.get('password', '')
            
            # Save credentials so they persist across page refresh
            save_cached_wifi_creds(ssid, password)
            
            wifi_success, error = test_wifi_connection(ssid, password)
            
            if wifi_success:
                speakers = scan_sonos_speakers()
                create_hotspot()
                self.send_json({'wifi_success': True, 'speakers': speakers})
            else:
                self.send_json({'wifi_success': False, 'error': error})
            return
        
        if path == '/api/test-volume':
            ip = data.get('ip', '')
            if ip:
                success, error = test_sonos_volume(ip)
                self.send_json({'success': success, 'error': error})
            else:
                self.send_json({'success': False, 'error': 'No IP provided'})
            return
        
        if path == '/api/save':
            ssid = data.get('ssid', '')
            password = data.get('password', '')
            speaker_ip = data.get('speaker_ip', '')
            speaker_name = data.get('speaker_name', '')
            hdmi_port = data.get('hdmi_port', '2')
            
            if not ssid or not speaker_ip:
                self.send_json({'success': False, 'error': 'Missing required fields'})
                return
            
            success = save_configuration(ssid, password, speaker_ip, speaker_name, hdmi_port)
            
            if success:
                self.send_json({'success': True})
                Thread(target=delayed_reboot, daemon=True).start()
            else:
                self.send_json({'success': False, 'error': 'Failed to save'})
            return
        
        self.send_response(404)
        self.end_headers()


def delayed_reboot():
    log.info("Rebooting in 5 seconds...")
    time.sleep(5)
    os.system('reboot')


def main():
    log.info("=" * 50)
    log.info("CEC-Sonos Bridge - AP Mode Starting")
    log.info("=" * 50)
    
    prescan_wifi_networks()
    
    if not create_hotspot():
        log.error("Failed to create hotspot!")
        sys.exit(1)
    
    log.info("")
    log.info("=" * 50)
    log.info(f"Connect to WiFi: {HOTSPOT_SSID}")
    log.info(f"Password: {HOTSPOT_PASSWORD}")
    log.info(f"Then open: http://{HOTSPOT_IP}")
    log.info("=" * 50)
    log.info("")
    
    def signal_handler(sig, frame):
        log.info("Shutting down...")
        stop_hotspot()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        server = HTTPServer(('0.0.0.0', WEB_PORT), SetupHandler)
        log.info(f"Web server running on port {WEB_PORT}")
        server.serve_forever()
    except Exception as e:
        log.exception(f"Web server error: {e}")
        stop_hotspot()
        sys.exit(1)


if __name__ == '__main__':
    main()
