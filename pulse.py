#!/usr/bin/env python3
"""
SINDRI v3 - Energy • Thermal • Systems (ex-PULSE)
http://localhost:7890
Requirements: pip install psutil pystray Pillow
Optional:
  - NVIDIA GPU      : nvidia-smi in PATH
  - All temps+fans  : LibreHardwareMonitor → Options > Remote Web Server (port 8085)
  - NVMe temp       : smartmontools (smartctl) in PATH
"""
import hashlib, json, queue, re, socket, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import psutil
except ImportError:
    print("pip install psutil"); sys.exit(1)

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    _SYSTRAY_OK = True
except ImportError:
    _SYSTRAY_OK = False

PORT             = 7890
INTERVAL         = 2.0    # secondes entre 2 collectes (modifiable en live via l'UI)
INTERVAL_MIN     = 0.1    # ultra rapide autorisé (charge CPU visible pendant benchs)
INTERVAL_MAX     = 30
C_WIN    = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == 'win32' else 0

import os as _os
_HERE   = _os.path.dirname(_os.path.abspath(__file__))
LHM_EXE = _os.path.join(_HERE, 'LibreHardwareMonitor', 'LibreHardwareMonitor.exe')
LHM_CFG = _os.path.join(_HERE, 'LibreHardwareMonitor', 'LibreHardwareMonitor.config')
LHM_PORT = 8085

SETTINGS_FILE  = _os.path.join(_HERE, 'settings.json')
HISTORY_FILE   = _os.path.join(_HERE, 'history.jsonl')
HISTORY_MAX_DAYS = 30       # on garde 30 jours max sur disque
HISTORY_WRITE_S  = 30       # écriture toutes les 30 secondes

# Windows Power Plan GUIDs
POWER_PLANS = {
    'balanced':    '381b4222-f694-41f0-9685-ff5bb260df2e',
    'highperf':    '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c',
    'powersaver':  'a1841308-3541-4fab-bc81-f71556f20b4a',
    'ultimate':    'e9a42b02-d5df-448d-aa00-03f14749eb61',
}

# Presets : power_plan + GPU power_limit_pct (100 = default, <100 = reduced)
PRESETS = {
    'gaming':  {'plan': 'highperf',   'gpu_pl_pct': 100, 'name': 'GAMING'},
    'silence': {'plan': 'powersaver', 'gpu_pl_pct': 60,  'name': 'SILENCE'},
    'office':  {'plan': 'balanced',   'gpu_pl_pct': 85,  'name': 'OFFICE'},
}

_state = {
    'stats': {},
    'prev_disk': None, 'prev_net': None, 'prev_time': None,
}
_lock  = threading.Lock()
_ping_ms         = None
_energy_wh       = 0.0   # accumulated watt-hours since start
_energy_t        = None
_energy_last_save = 0.0  # dernier save disque des compteurs cumulés (throttling)
_session_start   = time.time()
_events_cache    = []
_events_cache_ts = 0.0

# Historique persistant (JSONL)
_hist_last_write = 0
_hist_lock       = threading.Lock()

# Auto-monitoring : ressources utilisées par le PULSE lui-même
try:
    _self_proc = psutil.Process()
    _self_proc.cpu_percent(interval=None)   # premier appel = 0, initialise le compteur
except Exception:
    _self_proc = None

# Direct LHM library access (pythonnet) — no GUI/web server needed
_lhm_computer    = None
_lhm_ST          = None
_lhm_lib_lock    = threading.Lock()

# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(cmd, timeout=4):
    try:
        kw = {'creationflags': C_WIN} if sys.platform == 'win32' else {}
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''

def _fv(v):
    try: return float(str(v).strip())
    except: return None

# ── LibreHardwareMonitor — DIRECT via pythonnet (pas de GUI, pas de web server) ─

def _init_lhm_lib():
    """
    Load LibreHardwareMonitorLib.dll directly via pythonnet.
    No LHM GUI, no web server, no config, no menu automation.
    """
    global _lhm_computer, _lhm_ST
    lhm_dir = _os.path.join(_HERE, 'LibreHardwareMonitor')
    dll     = _os.path.join(lhm_dir, 'LibreHardwareMonitorLib.dll')
    if not _os.path.exists(dll):
        print(f'! LibreHardwareMonitorLib.dll introuvable: {dll}')
        return False
    try:
        if lhm_dir not in sys.path:
            sys.path.insert(0, lhm_dir)
        try:
            from pythonnet import load as _pn_load
            _pn_load('coreclr')
        except Exception:
            pass  # already loaded, or default runtime works
        import clr
        clr.AddReference(dll)
        from LibreHardwareMonitor.Hardware import Computer, SensorType
        c = Computer()
        c.IsCpuEnabled         = True
        c.IsGpuEnabled         = True
        c.IsMemoryEnabled      = True
        c.IsStorageEnabled     = True
        c.IsMotherboardEnabled = True
        c.IsControllerEnabled  = True
        c.IsNetworkEnabled     = True
        c.Open()
        _lhm_computer = c
        _lhm_ST       = SensorType
        n = len(list(c.Hardware))
        print(f'✓ LibreHardwareMonitor lib chargée — {n} composants détectés')
        return True
    except Exception as e:
        print(f'[lhm-lib] {e}')
        return False


def _lhm_data():
    """Read all sensor values directly from the LHM library."""
    if _lhm_computer is None:
        return {}, {}, {}
    temps, fans, powers = {}, {}, {}
    ST = _lhm_ST
    try:
        with _lhm_lib_lock:
            for hw in _lhm_computer.Hardware:
                try:
                    hw.Update()
                except Exception:
                    continue
                hw_name = str(hw.Name)
                # Sub-hardware (ex: CPU cores)
                for sub in hw.SubHardware:
                    try:
                        sub.Update()
                    except Exception:
                        continue
                    _extract_sensors(sub, temps, fans, powers,
                                     f'{hw_name}/{sub.Name}', ST)
                _extract_sensors(hw, temps, fans, powers, hw_name, ST)
    except Exception as e:
        print(f'[lhm-read] {e}')
    return temps, fans, powers


# ── Fan control : registry des capteurs contrôlables via IControl ─────────────

_lhm_controls = {}   # {ctrl_id: {sensor_key, control_obj, hw_name, sensor_name, min, max}}

def _lhm_scan_controls():
    """Scan récursif de tous les capteurs LHM avec `.Control` non-null. Construit
       un registry {stable_id: IControl}. À appeler après _init_lhm_lib et périodiquement."""
    global _lhm_controls
    if _lhm_computer is None:
        return {}
    reg = {}
    try:
        with _lhm_lib_lock:
            for hw in _lhm_computer.Hardware:
                try: hw.Update()
                except Exception: pass
                _scan_hw_controls(hw, str(hw.Name), reg)
                for sub in hw.SubHardware:
                    try: sub.Update()
                    except Exception: pass
                    _scan_hw_controls(sub, f'{str(hw.Name)}/{str(sub.Name)}', reg)
    except Exception as e:
        print(f'[lhm-controls-scan] {e}')
    _lhm_controls = reg
    return reg

def _scan_hw_controls(hw, hw_name, reg):
    for s in hw.Sensors:
        try:
            ctrl = s.Control
        except Exception:
            ctrl = None
        if ctrl is None:
            continue
        try:
            ctrl_id = f'{hw_name}/{s.Name}'
            try:    lo = float(ctrl.MinSoftwareValue)
            except Exception: lo = 0.0
            try:    hi = float(ctrl.MaxSoftwareValue)
            except Exception: hi = 100.0
            reg[ctrl_id] = {
                'ctrl_obj':    ctrl,
                'hw_name':     hw_name,
                'sensor_name': str(s.Name),
                'min':         lo,
                'max':         hi,
            }
        except Exception as e:
            print(f'[lhm-ctrl] {e}')

def _lhm_controls_snapshot():
    """Retourne un dict sérialisable {ctrl_id: {...état courant...}} pour l'UI."""
    out = {}
    for cid, meta in list(_lhm_controls.items()):
        c = meta['ctrl_obj']
        try:    mode = int(c.ControlMode)     # 0=Undef 1=Software 2=Default
        except Exception: mode = 0
        try:    sw   = float(c.SoftwareValue)
        except Exception: sw = None
        out[cid] = {
            'id':          cid,
            'hw':          meta['hw_name'],
            'sensor':      meta['sensor_name'],
            'min':         meta['min'],
            'max':         meta['max'],
            'mode':        mode,        # 0/1/2
            'mode_label':  ('undef', 'software', 'default')[mode] if 0 <= mode <= 2 else 'unknown',
            'software_pct': sw,
        }
    return out

def _lhm_control_set(ctrl_id, percent):
    """Applique un PWM % à un contrôle. Retourne (ok, msg)."""
    meta = _lhm_controls.get(ctrl_id)
    if not meta:
        return False, f'contrôle inconnu: {ctrl_id}'
    try:
        pct = max(float(meta['min']), min(float(meta['max']), float(percent)))
        with _lhm_lib_lock:
            meta['ctrl_obj'].SetSoftware(pct)
        return True, f'{ctrl_id} → {pct:.0f}%'
    except Exception as e:
        return False, str(e)

def _lhm_control_default(ctrl_id):
    """Rend le contrôle au BIOS/mobo (SetDefault). Retourne (ok, msg)."""
    meta = _lhm_controls.get(ctrl_id)
    if not meta:
        return False, f'contrôle inconnu: {ctrl_id}'
    try:
        with _lhm_lib_lock:
            meta['ctrl_obj'].SetDefault()
        return True, f'{ctrl_id} → BIOS'
    except Exception as e:
        return False, str(e)

def _interp_curve(temp, points):
    """Interpole une courbe temp→PWM à partir de points [[t, pwm], ...] triés.
       Sous t0 → pwm0, au-dessus tN → pwmN, sinon linéaire."""
    if not points: return None
    pts = sorted(points, key=lambda p: p[0])
    if temp <= pts[0][0]:  return pts[0][1]
    if temp >= pts[-1][0]: return pts[-1][1]
    for i in range(len(pts) - 1):
        t0, p0 = pts[i]
        t1, p1 = pts[i+1]
        if t0 <= temp <= t1:
            if t1 == t0: return p1
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return pts[-1][1]

def _apply_fan_curves(stats):
    """Applique les courbes fan_curves activées : lit la source de température
       (cpu, gpu, ou 'hottest'), interpole le PWM cible, applique via SetSoftware.
       Appelé à chaque tick de collecte."""
    curves = _settings.get('fan_curves') or {}
    if not curves: return
    # Sources de température disponibles
    cpu_t = (stats.get('cpu') or {}).get('temp')
    gpu_t = ((stats.get('gpus') or [{}])[0]).get('temp')
    all_temps = [v for v in (stats.get('temps') or {}).values() if isinstance(v, (int, float))]
    hottest   = max(all_temps) if all_temps else None
    src_map = {'cpu': cpu_t, 'gpu': gpu_t, 'hottest': hottest}
    for cid, cfg in curves.items():
        if not cfg.get('enabled'): continue
        if cid not in _lhm_controls:  continue    # canal disparu ou pas encore scanné
        src   = cfg.get('source', 'cpu')
        temp  = src_map.get(src)
        if temp is None: continue
        pwm = _interp_curve(temp, cfg.get('points') or [])
        if pwm is None: continue
        try:
            _lhm_control_set(cid, pwm)
        except Exception as e:
            print(f'[fan-curve] {cid}: {e}')


# ── Historique persistant (JSONL) ─────────────────────────────────────────────

def _write_history_point(stats, now):
    global _hist_last_write
    if now - _hist_last_write < HISTORY_WRITE_S:
        return
    _hist_last_write = now
    gpu = (stats.get('gpus') or [{}])[0]
    p = {
        'ts':       int(now),
        'cpu_pct':  stats.get('cpu', {}).get('pct'),
        'cpu_temp': stats.get('cpu', {}).get('temp'),
        'cpu_w':    stats.get('power', {}).get('cpu_w'),
        'gpu_pct':  gpu.get('util'),
        'gpu_temp': gpu.get('temp'),
        'gpu_w':    gpu.get('power_w'),
        'ac_total_w': stats.get('power', {}).get('ac_total_w'),
        'ram_pct':  stats.get('mem', {}).get('pct'),
        'disk_r':   stats.get('io', {}).get('disk_r'),
        'disk_w':   stats.get('io', {}).get('disk_w'),
        'net_r':    stats.get('io', {}).get('net_r'),
        'net_s':    stats.get('io', {}).get('net_s'),
        'ping':     stats.get('ping_ms'),
    }
    with _hist_lock:
        try:
            with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(p, separators=(',',':')) + '\n')
        except Exception as e:
            print(f'[history] {e}')

def _read_history(since_ts, max_points=500):
    """Lit l'historique depuis un timestamp. Sous-échantillonne à max_points."""
    if not _os.path.exists(HISTORY_FILE):
        return []
    points = []
    with _hist_lock:
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        p = json.loads(line)
                        if p.get('ts', 0) >= since_ts:
                            points.append(p)
                    except Exception:
                        pass
        except Exception as e:
            print(f'[history-read] {e}')
    # Sous-échantillonnage
    if len(points) > max_points:
        step = len(points) / max_points
        points = [points[int(i*step)] for i in range(max_points)]
    return points

def _cleanup_history():
    """Supprime les points plus anciens que HISTORY_MAX_DAYS. Appelé au démarrage."""
    if not _os.path.exists(HISTORY_FILE):
        return
    cutoff = time.time() - HISTORY_MAX_DAYS * 86400
    kept = []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    p = json.loads(line)
                    if p.get('ts', 0) >= cutoff:
                        kept.append(line)
                except Exception:
                    pass
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            f.writelines(kept)
        print(f'✓ Historique : {len(kept)} points conservés')
    except Exception as e:
        print(f'[history-cleanup] {e}')


def _self_stats():
    """Ressources utilisées par le process du PULSE lui-même."""
    if _self_proc is None:
        return {'cpu': 0, 'ram_mb': 0, 'threads': 0}
    try:
        return {
            'cpu':      round(_self_proc.cpu_percent(interval=None) / psutil.cpu_count(True), 2),
            'ram_mb':   round(_self_proc.memory_info().rss / 1048576, 1),
            'threads':  _self_proc.num_threads(),
        }
    except Exception:
        return {'cpu': 0, 'ram_mb': 0, 'threads': 0}

_cpu_name_cache  = None
_cpu_tdp_cache   = None
_bios_info_cache = None    # {vendor, version, date, mobo}

def _cpu_name():
    """Get CPU model name via LHM lib or WMI."""
    global _cpu_name_cache
    if _cpu_name_cache is not None:
        return _cpu_name_cache
    # Try LHM first (already opened)
    try:
        if _lhm_computer:
            for hw in _lhm_computer.Hardware:
                if 'Cpu' in str(hw.HardwareType):
                    _cpu_name_cache = str(hw.Name); return _cpu_name_cache
    except Exception:
        pass
    # Fallback: registry
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0')
        _cpu_name_cache, _ = winreg.QueryValueEx(k, 'ProcessorNameString')
        winreg.CloseKey(k); return _cpu_name_cache
    except Exception:
        pass
    _cpu_name_cache = 'Unknown CPU'
    return _cpu_name_cache

_displays_cache = None
_displays_cache_ts = 0

def _displays_info():
    """Liste des écrans connectés : nom, résolution, Hz. Cache 30s pour ne pas
       spammer WMI (les changements sont rares)."""
    global _displays_cache, _displays_cache_ts
    if _displays_cache is not None and (time.time() - _displays_cache_ts) < 30:
        return _displays_cache
    displays = []
    if sys.platform == 'win32':
        try:
            # Win32_VideoController donne : Name (GPU), CurrentHorizontalResolution,
            # CurrentVerticalResolution, CurrentRefreshRate. Il y a une entrée par
            # sortie active (donc typiquement 1 par écran connecté).
            ps = ('Get-CimInstance Win32_VideoController | '
                  'Where-Object {$_.CurrentHorizontalResolution -gt 0} | '
                  'Select-Object Name,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate | '
                  'ConvertTo-Json -Compress')
            out = _run(['powershell', '-NoProfile', '-Command', ps], timeout=6)
            if out:
                data = json.loads(out)
                if not isinstance(data, list): data = [data]
                for d in data:
                    w = d.get('CurrentHorizontalResolution') or 0
                    h = d.get('CurrentVerticalResolution')   or 0
                    hz = d.get('CurrentRefreshRate') or 0
                    if w and h:
                        displays.append({
                            'name':  (d.get('Name') or 'Écran')[:40],
                            'width':  int(w),
                            'height': int(h),
                            'hz':     int(hz),
                            'label':  f'{w}×{h} @ {hz}Hz' if hz else f'{w}×{h}',
                        })
        except Exception as e:
            print(f'[displays] {e}')
    _displays_cache = displays
    _displays_cache_ts = time.time()
    return displays

def _bios_info():
    """Récupère vendor, version, release date du BIOS + modèle carte mère via WMI.
       Mise en cache (les infos ne changent qu'après un flash BIOS)."""
    global _bios_info_cache
    if _bios_info_cache is not None:
        return _bios_info_cache
    info = {'vendor': None, 'version': None, 'date': None, 'mobo': None}
    if sys.platform != 'win32':
        _bios_info_cache = info; return info
    try:
        # PowerShell + CIM (plus rapide que wmic legacy)
        ps = ('Get-CimInstance Win32_BIOS | '
              'Select-Object Manufacturer,SMBIOSBIOSVersion,ReleaseDate | ConvertTo-Json -Compress')
        out = _run(['powershell', '-NoProfile', '-Command', ps], timeout=6)
        if out:
            b = json.loads(out)
            info['vendor']  = b.get('Manufacturer')
            info['version'] = b.get('SMBIOSBIOSVersion')
            rd = b.get('ReleaseDate') or ''
            # Format WMI: 20240215000000.000000+000 → 2024-02-15
            if isinstance(rd, str) and len(rd) >= 8 and rd[:8].isdigit():
                info['date'] = f'{rd[:4]}-{rd[4:6]}-{rd[6:8]}'
        ps2 = ('Get-CimInstance Win32_BaseBoard | '
               'Select-Object Manufacturer,Product | ConvertTo-Json -Compress')
        out2 = _run(['powershell', '-NoProfile', '-Command', ps2], timeout=6)
        if out2:
            bb = json.loads(out2)
            m = bb.get('Manufacturer') or ''
            p = bb.get('Product') or ''
            info['mobo'] = (m + ' ' + p).strip() or None
    except Exception as e:
        print(f'[bios] {e}')
    _bios_info_cache = info
    return info

# TDP par famille/modèle (regex → watts). Ordre = priorité (spécifique avant général).
_TDP_RULES = [
    # ── Intel HEDT / Enthusiast ──
    (r'i9-1[3-4]\d{3}(K|KS)',   253),  # i9-13/14 K/KS turbo
    (r'i7-1[3-4]\d{3}K',        253),
    (r'i9-1[2]\d{3}(K|KS)',     241),  # i9-12 K
    (r'i7-1[2]\d{3}K',          190),
    (r'i5-1[3-4]\d{3}K',        181),
    (r'i5-1[2]\d{3}K',          150),
    (r'i9-10\d{3}(X|XE)',       165),
    (r'i9-9\d{3}(X|XE)',        165),
    (r'i7-5820K|i7-5930K',      140),
    (r'i7-6800K|i7-6850K',      140),
    (r'i9-\d{4,5}X',            140),
    # ── Intel Desktop mainstream ──
    (r'i9-\d{4,5}K',            125),
    (r'i7-\d{4,5}K',            125),
    (r'i5-\d{4,5}K',            95),
    (r'i9-\d{4,5}',             65),
    (r'i7-\d{4,5}',             65),
    (r'i5-\d{4,5}',             65),
    (r'i3-\d{4,5}',             65),
    # ── Intel laptop ("H", "HX", "U") ──
    (r'i[579]-\d{4,5}HX',       55),
    (r'i[579]-\d{4,5}H',        45),
    (r'i[579]-\d{4,5}U',        15),
    # ── AMD Ryzen desktop ──
    (r'Ryzen\s+9\s+7950X',      170),
    (r'Ryzen\s+9\s+7900X',      170),
    (r'Ryzen\s+9\s+5950X',      105),
    (r'Ryzen\s+9\s+5900X',      105),
    (r'Ryzen\s+7\s+7800X',      120),
    (r'Ryzen\s+7\s+7700X',      105),
    (r'Ryzen\s+7\s+5800X3D',    105),
    (r'Ryzen\s+7\s+5800X',      105),
    (r'Ryzen\s+7\s+5700X',      65),
    (r'Ryzen\s+5\s+7600X',      105),
    (r'Ryzen\s+5\s+5600X',      65),
    (r'Ryzen\s+5\s+5600G',      65),
    (r'Ryzen\s+3',              65),
    (r'Threadripper',           280),
    # ── Fallback génériques ──
    (r'Xeon|EPYC',              140),
]

def _cpu_tdp_estimate():
    """Détecte le TDP du CPU depuis son nom (regex). Cache après premier appel."""
    global _cpu_tdp_cache
    if _cpu_tdp_cache is not None:
        return _cpu_tdp_cache
    name = _cpu_name()
    for pat, tdp in _TDP_RULES:
        if re.search(pat, name, re.I):
            print(f'✓ CPU: {name} → TDP {tdp}W')
            _cpu_tdp_cache = tdp
            return tdp
    print(f'? CPU: {name} → TDP défaut 95W')
    _cpu_tdp_cache = 95   # défaut raisonnable
    return _cpu_tdp_cache


def _extract_sensors(hw, temps, fans, powers, prefix, ST):
    for s in hw.Sensors:
        v = s.Value
        if v is None:
            continue
        try:
            v = float(v)
        except Exception:
            continue
        name_lc = s.Name.lower()
        # Filtre : les capteurs "Warning/Critical/Threshold Temperature" des SSD
        # sont des SEUILS CONSTRUCTEUR statiques (ex: 84°C, 117°C), pas la temp actuelle
        if any(kw in name_lc for kw in ('warning', 'critical', 'threshold', 'shutdown')):
            continue
        key = f'{prefix}/{s.Name}'
        st  = s.SensorType
        # Températures : range plausible 5-110°C (élimine capteurs fantômes
        # non branchés qui rapportent 4°C ou 119.5°C)
        if st == ST.Temperature and 5 < v < 110:
            temps[key] = round(v, 1)
        elif st == ST.Fan and v > 0:
            fans[key] = int(v)
        elif st == ST.Power and v >= 0:
            powers[key] = round(v, 1)

# ── Ping (background) ──────────────────────────────────────────────────────────

def _ping_loop():
    global _ping_ms
    while True:
        try:
            if sys.platform == 'win32':
                out = _run(['ping', '-n', '1', '-w', '2000', '8.8.8.8'], timeout=5)
                m = re.search(r'Average = (\d+)ms', out)
                _ping_ms = int(m.group(1)) if m else None
            else:
                out = _run(['ping', '-c', '1', '-W', '2', '8.8.8.8'], timeout=5)
                m = re.search(r'time=(\d+\.?\d*)', out)
                _ping_ms = round(float(m.group(1))) if m else None
        except Exception:
            _ping_ms = None
        time.sleep(5)

# ── NVIDIA GPU ─────────────────────────────────────────────────────────────────

_gpu_meta_cache = None   # {name, vbios, driver} — statique, mis en cache

def _nvidia_meta():
    """Récupère nom + VBIOS + driver_version (données statiques, mise en cache)."""
    global _gpu_meta_cache
    if _gpu_meta_cache is not None:
        return _gpu_meta_cache
    out = _run(['nvidia-smi',
                '--query-gpu=name,vbios_version,driver_version',
                '--format=csv,noheader,nounits'])
    meta = []
    if out:
        for line in out.splitlines():
            p = [x.strip() for x in line.split(',')]
            if len(p) < 3: continue
            meta.append({'name': p[0], 'vbios': p[1], 'driver': p[2]})
    _gpu_meta_cache = meta
    return meta

def _nvidia():
    fields = ('name,temperature.gpu,utilization.gpu,memory.used,memory.total,'
              'power.draw,power.limit,fan.speed,clocks.current.graphics,clocks.current.memory')
    out = _run(['nvidia-smi', f'--query-gpu={fields}', '--format=csv,noheader,nounits'])
    if not out: return []
    meta = _nvidia_meta()
    gpus = []
    for i, line in enumerate(out.splitlines()):
        p = [x.strip() for x in line.split(',')]
        if len(p) < 10: continue
        m = meta[i] if i < len(meta) else {}
        gpus.append({
            'name': p[0], 'temp': _fv(p[1]), 'util': _fv(p[2]),
            'mem_used_mb': _fv(p[3]), 'mem_total_mb': _fv(p[4]),
            'power_w': _fv(p[5]), 'power_limit_w': _fv(p[6]),
            'fan_pct': _fv(p[7]), 'clock_mhz': _fv(p[8]), 'mem_clock_mhz': _fv(p[9]),
            'vbios': m.get('vbios'), 'driver': m.get('driver'),
        })
    return gpus

# ── NVMe via smartctl ──────────────────────────────────────────────────────────

def _smartctl_temps():
    temps = {}
    for i in range(6):
        path = f'\\\\.\\PhysicalDrive{i}' if sys.platform == 'win32' else f'/dev/nvme{i}'
        out = _run(['smartctl', '--json', '-a', path])
        if not out: continue
        try:
            data = json.loads(out)
            t = data.get('temperature', {}).get('current')
            model = data.get('model_name', f'Drive{i}')[:22]
            if t: temps[f'Storage/{model}'] = int(t)
        except: pass
    return temps


# Attributs SMART qu'on remonte (SATA/HDD) — reste discret, pas de bruit
_SMART_KEY_ATTRS = {
    'Reallocated_Sector_Ct', 'Current_Pending_Sector', 'Offline_Uncorrectable',
    'Wear_Leveling_Count', 'Power_On_Hours', 'Media_Wearout_Indicator',
    'Total_LBAs_Written', 'SSD_Life_Left', 'Percent_Lifetime_Used',
    'Reported_Uncorrect', 'CRC_Error_Count', 'Temperature_Celsius',
}

def _smartctl_available():
    """Vérifie que smartctl.exe est trouvable dans le PATH. Cache."""
    global _smartctl_ok
    if _smartctl_ok is not None: return _smartctl_ok
    try:
        r = subprocess.run(['smartctl', '--version'], capture_output=True, text=True,
                           timeout=3, creationflags=C_WIN if sys.platform == 'win32' else 0)
        _smartctl_ok = r.returncode == 0
    except Exception:
        _smartctl_ok = False
    return _smartctl_ok

_smartctl_ok = None

def _smart_report():
    """Rapport SMART complet pour tous les disques détectés. Peut prendre 2-10s (bloquant).
       Retourne aussi les erreurs pour que le client puisse afficher un message clair."""
    if not _smartctl_available():
        return {'error': 'smartctl_missing',
                'msg':   'smartctl.exe non trouvé. Installe smartmontools : winget install smartmontools',
                'disks': []}
    reports = []
    for i in range(8):
        path = f'\\\\.\\PhysicalDrive{i}' if sys.platform == 'win32' else f'/dev/nvme{i}'
        out = _run(['smartctl', '--json', '-a', path])
        if not out: continue
        try:
            d = json.loads(out)
        except Exception:
            continue
        # exit_status : bit 0=command failed, bit 2=SMART unavailable → on skip vraiment cassé
        exit_st = d.get('smartctl', {}).get('exit_status', 1)
        if exit_st & 0b101:  # bit 0 ou bit 2
            continue
        model = (d.get('model_name') or d.get('device', {}).get('name') or f'Drive{i}')[:40]
        passed = d.get('smart_status', {}).get('passed')      # True/False/None
        temp   = d.get('temperature', {}).get('current')
        cap    = d.get('user_capacity', {}).get('bytes') or 0

        attrs = {}
        # SATA/HDD attributes
        for a in d.get('ata_smart_attributes', {}).get('table', []) or []:
            name = a.get('name', '')
            if name in _SMART_KEY_ATTRS:
                attrs[name] = a.get('raw', {}).get('value')
        # NVMe log
        nvme = d.get('nvme_smart_health_information_log', {}) or {}
        if nvme:
            for k in ('power_on_hours', 'percentage_used', 'media_errors',
                      'data_units_written', 'available_spare', 'unsafe_shutdowns',
                      'controller_busy_time'):
                if k in nvme:
                    attrs[k] = nvme[k]

        # Statut synthétique
        status = 'unknown'
        if passed is True:
            pct_used = attrs.get('percentage_used') or attrs.get('Percent_Lifetime_Used') or 0
            realloc  = attrs.get('Reallocated_Sector_Ct') or 0
            pending  = attrs.get('Current_Pending_Sector') or 0
            errors   = attrs.get('media_errors') or 0
            spare    = attrs.get('available_spare')
            if pct_used > 80 or realloc > 0 or pending > 0 or errors > 0 or (spare is not None and spare < 20):
                status = 'warn'
            else:
                status = 'ok'
        elif passed is False:
            status = 'fail'

        reports.append({
            'device':  path,
            'model':   model,
            'size_gb': round(cap / (1024**3), 1) if cap else None,
            'type':    d.get('device', {}).get('type') or 'unknown',
            'temp':    temp,
            'status':  status,
            'attrs':   attrs,
        })
    if not reports:
        return {'error': 'no_disks',
                'msg':   'Aucun disque lisible via SMART. Vérifie que SINDRI tourne en admin (les IOCTL PhysicalDrive nécessitent des droits élevés).',
                'disks': []}
    return {'error': None, 'msg': None, 'disks': reports}

# ── WMI fallback temps ─────────────────────────────────────────────────────────

def _wmi_temps():
    results = {}
    try:
        import wmi
        for i, z in enumerate(wmi.WMI(namespace='root\\wmi').MSAcpi_ThermalZoneTemperature()):
            t = round(z.CurrentTemperature / 10.0 - 273.15, 1)
            if 0 < t < 120: results[f'System/ThermalZone{i}'] = t
    except Exception: pass
    return results

# ── Top processes ─────────────────────────────────────────────────────────────

# Process système Windows dont le kill = crash immédiat ou instabilité majeure
SYSTEM_CRITICAL = {
    'system', 'system idle process', 'registry', 'memory compression', 'secure system',
    'smss.exe', 'csrss.exe', 'wininit.exe', 'services.exe', 'lsass.exe', 'lsm.exe',
    'winlogon.exe', 'fontdrvhost.exe', 'dwm.exe', 'sihost.exe',
    'svchost.exe', 'searchindexer.exe', 'searchhost.exe', 'runtimebroker.exe',
    'audiodg.exe', 'ctfmon.exe', 'wudfhost.exe', 'spoolsv.exe',
    # Notre propre process
    'python.exe', 'pythonw.exe',
}
SYSTEM_USERS = {'SYSTEM', 'LOCAL SERVICE', 'NETWORK SERVICE'}

def _is_critical(name, username):
    if not name: return False
    n = name.lower()
    if n in SYSTEM_CRITICAL: return True
    if username and username.split('\\')[-1].upper() in SYSTEM_USERS: return True
    return False

# Cache des Process psutil pour avoir un delta CPU% correct entre 2 collectes
_proc_cache = {}

def _top_processes():
    """
    Renvoie les processus, avec CPU% NORMALISÉ par le nombre de threads
    (comme le Task Manager Windows : 0-100% max, pas cumul multi-core).
    """
    global _proc_cache
    cpu_count = psutil.cpu_count(True) or 1
    active = set()

    # Recense les process actuels + initialise ceux qu'on découvre
    for p in psutil.process_iter(['pid']):
        pid = p.info['pid']
        active.add(pid)
        if pid not in _proc_cache:
            try:
                _proc_cache[pid] = psutil.Process(pid)
                _proc_cache[pid].cpu_percent(interval=None)   # amorce le compteur (retourne 0)
            except Exception:
                pass

    # Purge les process disparus
    _proc_cache = {pid: obj for pid, obj in _proc_cache.items() if pid in active}

    procs = []
    for pid, obj in _proc_cache.items():
        try:
            with obj.oneshot():
                name    = obj.name()
                cpu_raw = obj.cpu_percent(interval=None)     # cumul multi-core
                cpu     = cpu_raw / cpu_count                 # normalisé 0-100
                mem_mb  = obj.memory_info().rss / 1048576
                try: user = obj.username() or ''
                except Exception: user = ''
            if cpu > 0.1 or mem_mb > 30:
                procs.append({
                    'pid':      pid,
                    'name':     name,
                    'cpu':      round(cpu, 1),
                    'mem_mb':   round(mem_mb, 1),
                    'user':     user.split('\\')[-1],
                    'critical': _is_critical(name, user),
                })
        except Exception:
            pass
    procs.sort(key=lambda x: x['cpu'], reverse=True)
    return procs[:30]

# ── Network processes ─────────────────────────────────────────────────────────

_net_proc_cache    = []
_net_proc_cache_ts = 0.0

def _kill_process_connections(pid):
    """Ferme TOUTES les connexions TCP appartenant à un PID sans tuer le process
       (Get-NetTCPConnection | Close-NetTCPConnection). Retourne (ok, nb_closed, msg)."""
    if sys.platform != 'win32':
        return False, 0, 'non supporté hors Windows'
    try:
        ps = (f'$c = Get-NetTCPConnection -OwningProcess {int(pid)} -ErrorAction SilentlyContinue; '
              f'if ($c) {{ $n = @($c).Count; $c | Close-NetTCPConnection -Confirm:$false -ErrorAction SilentlyContinue; '
              f'Write-Output "closed=$n" }} else {{ Write-Output "closed=0" }}')
        r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                           capture_output=True, text=True, timeout=10, creationflags=C_WIN)
        out = (r.stdout or '').strip()
        n = 0
        if 'closed=' in out:
            try: n = int(out.split('closed=')[1].split()[0])
            except Exception: n = 0
        return r.returncode == 0, n, out or (r.stderr or '').strip()
    except Exception as e:
        return False, 0, str(e)

def _net_processes():
    global _net_proc_cache, _net_proc_cache_ts
    now = time.time()
    if now - _net_proc_cache_ts < 4:
        return _net_proc_cache

    from collections import defaultdict
    pid_data = defaultdict(lambda: {'conns': 0, 'remotes': set()})
    try:
        for c in psutil.net_connections(kind='inet'):
            if not c.pid: continue
            if c.status not in ('ESTABLISHED', 'SYN_SENT', 'CLOSE_WAIT'): continue
            d = pid_data[c.pid]
            d['conns'] += 1
            if c.raddr:
                d['remotes'].add(f"{c.raddr.ip}:{c.raddr.port}")
    except Exception:
        try:
            for c in psutil.net_connections():
                if c.pid and c.raddr:
                    pid_data[c.pid]['conns'] += 1
                    pid_data[c.pid]['remotes'].add(f"{c.raddr.ip}:{c.raddr.port}")
        except Exception:
            _net_proc_cache_ts = now
            return _net_proc_cache

    result = []
    for pid, d in pid_data.items():
        try:
            p    = psutil.Process(pid)
            name = p.name()
            result.append({
                'pid':     pid,
                'name':    name,
                'conns':   d['conns'],
                'remotes': sorted(d['remotes'])[:3],
            })
        except Exception:
            pass

    result.sort(key=lambda x: x['conns'], reverse=True)
    _net_proc_cache    = result[:20]
    _net_proc_cache_ts = now
    return _net_proc_cache

# ── Windows System Events ──────────────────────────────────────────────────────

def _win_events():
    global _events_cache, _events_cache_ts
    now = time.time()
    if now - _events_cache_ts < 60:  # refresh every 60s
        return _events_cache
    events = []
    try:
        ps_cmd = (
            "Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2,3;"
            "StartTime=(Get-Date).AddHours(-24)} -MaxEvents 8 -EA SilentlyContinue"
            " | Select-Object TimeCreated,LevelDisplayName,ProviderName,"
            "@{N='Msg';E={$_.Message.Split(\"`n\")[0].Trim()}} | ConvertTo-Json -Compress"
        )
        out = _run(['powershell', '-NoProfile', '-Command', ps_cmd], timeout=8)
        if out:
            raw = json.loads(out)
            if isinstance(raw, dict): raw = [raw]
            for e in raw:
                events.append({
                    'time':   str(e.get('TimeCreated', ''))[:19],
                    'level':  str(e.get('LevelDisplayName', '')),
                    'source': str(e.get('ProviderName', ''))[:30],
                    'msg':    str(e.get('Msg', ''))[:100],
                })
    except Exception as ex:
        pass
    _events_cache    = events
    _events_cache_ts = now
    return events

# ── Collect all sensors ────────────────────────────────────────────────────────

def _get_sensors():
    lhm_temps, fans, powers = _lhm_data()
    if lhm_temps:
        nvme = _smartctl_temps()
        return {**lhm_temps, **nvme}, fans, powers

    nvme = _smartctl_temps()
    try:
        raw = psutil.sensors_temperatures() or {}
        ps_temps = {}
        for name, entries in raw.items():
            for e in entries:
                if e.current and 0 < e.current < 120:
                    ps_temps[f'{name}/{e.label or "temp"}'] = round(e.current, 1)
    except Exception:
        ps_temps = {}

    wmi_t = _wmi_temps()
    temps = {**ps_temps, **wmi_t, **nvme}
    return temps, fans, powers

# ── Main collector ─────────────────────────────────────────────────────────────

def _collect():
    now = time.time()
    with _lock:
        prev_d = _state['prev_disk']
        prev_n = _state['prev_net']
        prev_t = _state['prev_time']
    dt = (now - prev_t) if prev_t else 1.0

    cpu_pct = psutil.cpu_percent(interval=None)
    cpu_per = [round(x, 1) for x in psutil.cpu_percent(interval=None, percpu=True)]
    freq    = psutil.cpu_freq()
    vm      = psutil.virtual_memory()
    sw      = psutil.swap_memory()

    parts = []
    for p in psutil.disk_partitions(all=False):
        if not p.fstype or 'loop' in p.device.lower(): continue
        try:
            u = psutil.disk_usage(p.mountpoint)
            parts.append({'device': p.device, 'mountpoint': p.mountpoint,
                          'total': u.total, 'used': u.used, 'free': u.free, 'pct': u.percent})
        except: pass

    cd = psutil.disk_io_counters()
    cn = psutil.net_io_counters()

    def spd(cur, prev, attr):
        return max(0.0, (getattr(cur, attr) - getattr(prev, attr)) / dt) if prev and dt > 0 else 0.0

    gpus              = _nvidia()
    temps, fans, pows = _get_sensors()

    cpu_temp = None
    for k, v in temps.items():
        lk = k.lower()
        if any(x in lk for x in ('cpu package','core max','tctl','tdie','cpu temp','processor')):
            cpu_temp = v; break
    if cpu_temp is None:
        for k, v in temps.items():
            if any(x in k.lower() for x in ('cpu','core')): cpu_temp = v; break

    cpu_power = None
    for k, v in pows.items():
        if any(x in k.lower() for x in ('cpu package','cpu power')): cpu_power = v; break

    # Sanity check : LHM lib peut mal lire les RAPL MSRs sur certaines générations
    # (ex: Haswell-E) et retourner 0.5-2W en permanence. Si irréaliste, on estime.
    cpu_power_estimated = False
    tdp = _cpu_tdp_estimate()
    CPU_IDLE_W = max(10, int(tdp * 0.11))   # ~11% du TDP à l'idle (heuristique)
    if cpu_power is None or cpu_power < 8:
        cpu_power = round(CPU_IDLE_W + (tdp - CPU_IDLE_W) * (cpu_pct / 100), 1)
        cpu_power_estimated = True

    gpu_pwr = sum(g['power_w'] for g in gpus if g.get('power_w')) or 0
    gpu_pct = (gpus[0].get('util') or 0) if gpus else 0

    # Estimation complète de la conso à la prise (wall power)
    disk_r = _state.get('stats', {}).get('io', {}).get('disk_r', 0)
    disk_w = _state.get('stats', {}).get('io', {}).get('disk_w', 0)
    net_r  = _state.get('stats', {}).get('io', {}).get('net_r', 0)
    net_s  = _state.get('stats', {}).get('io', {}).get('net_s', 0)
    power_breakdown = _estimate_total_power(
        cpu_power, gpu_pwr, cpu_pct, gpu_pct,
        disk_r, disk_w, net_r, net_s,
    )

    # Energy accumulation = conso réelle à la prise (AC total)
    global _energy_wh, _energy_t
    if _energy_t:
        _energy_wh += power_breakdown['ac_total_w'] * dt / 3600
        # Cumul jour/mois/année persistant
        _update_energy_totals(power_breakdown['ac_total_w'], dt, now)
    _energy_t = now

    # Top processes (every 4s)
    old_procs = _state.get('stats', {}).get('processes', [])
    procs = _top_processes() if not old_procs or int(now) % 4 == 0 else old_procs
    events = _win_events()

    # Détection thermal/power throttle : CPU sous charge soutenue mais fréquence bien
    # en-dessous du max. Heuristique : load ≥ 60% et freq < 85% de freq_max
    freq_cur = round(freq.current) if freq else 0
    freq_max = round(freq.max) if freq else 0
    throttle = False
    if freq_cur and freq_max and cpu_pct >= 60 and freq_cur < freq_max * 0.85:
        throttle = True
    stats = {
        'cpu': {
            'pct': round(cpu_pct, 1), 'cores': cpu_per,
            'freq_mhz': freq_cur, 'freq_max_mhz': freq_max,
            'logical': psutil.cpu_count(True), 'physical': psutil.cpu_count(False),
            'temp': cpu_temp, 'power_w': cpu_power, 'power_est': cpu_power_estimated,
            'throttle': throttle,
            'name': _cpu_name(),
        },
        'system': {
            'bios':     _bios_info(),
            'displays': _displays_info(),
        },
        'mem': {
            'total': vm.total, 'used': vm.used, 'free': vm.available,
            'pct': round(vm.percent, 1),
            'swap_total': sw.total, 'swap_used': sw.used, 'swap_pct': round(sw.percent, 1),
        },
        'disks': parts[:4],
        'io': {
            'disk_r': spd(cd, prev_d, 'read_bytes'), 'disk_w': spd(cd, prev_d, 'write_bytes'),
            'net_r': spd(cn, prev_n, 'bytes_recv'),  'net_s': spd(cn, prev_n, 'bytes_sent'),
        },
        'gpus': gpus, 'temps': temps, 'fans': fans, 'powers': pows,
        'fan_controls': _lhm_controls_snapshot(),
        'net_procs': _net_processes(),
        'power': {'gpu_w': gpu_pwr, 'cpu_w': cpu_power, **power_breakdown},
        'energy_wh': round(_energy_wh, 4),
        'energy_totals': {
            'daily_wh':   round(_settings.get('energy_daily_wh', 0.0), 3),
            'monthly_wh': round(_settings.get('energy_monthly_wh', 0.0), 3),
            'yearly_wh':  round(_settings.get('energy_yearly_wh', 0.0), 3),
            'kwh_price':  _settings.get('kwh_price', 0.0937),
            'currency':   _settings.get('kwh_currency', '€'),
        },
        # Auto-heal : état + événements récents (< 30s) + diag live par règle + throttles actifs
        'heal': {
            'auto_enabled': bool(_settings.get('heal_auto_enabled')),
            'events':       list(_heal_events),
            'diag':         dict(_heal_diag),
            'active':       _heal_active_snapshot(),
        },
        'session_start': _session_start,
        'ping_ms': _ping_ms,
        'processes': procs,
        'events': events,
        'self': _self_stats(),
    }

    with _lock:
        _state.update(stats=stats, prev_disk=cd, prev_net=cn, prev_time=now)

    # Historique persistant (écriture toutes les HISTORY_WRITE_S secondes)
    try:
        _write_history_point(stats, now)
    except Exception as e:
        print(f'[history-write] {e}')

    # Webhook alerts (rate-limited, non-blocking)
    try:
        _check_and_notify(stats)
    except Exception as e:
        print(f'[webhook-check] {e}')

    # Auto-heal (si activé + règles cochées)
    try:
        _run_heal_rules(stats)
    except Exception as e:
        print(f'[auto-heal] {e}')

    # Courbes fans automatiques
    try:
        _apply_fan_curves(stats)
    except Exception as e:
        print(f'[fan-curves] {e}')


def _loop():
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)
    while True:
        try: _collect()
        except Exception as e: print(f'[collect] {e}')
        # Intervalle configurable en direct (borné par INTERVAL_MIN/MAX)
        iv = float(_settings.get('refresh_interval_s', INTERVAL))
        time.sleep(max(INTERVAL_MIN, min(INTERVAL_MAX, iv)))

# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>SINDRI — Energy • Thermal • Systems</title>
<style>
/* ── THEMES ────────────────────────────────────────────────────────────────── */
:root, [data-theme="neon"] {
  --a1: #00fff9; --a1g: rgba(0,255,249,.2);
  --a2: #b44aff; --a2g: rgba(180,74,255,.2);
  --a3: #ff2d78; --a3g: rgba(255,45,120,.2);
  --bg: #000; --bg2: #07070e; --card: #0b0b17;
  --border: #1a1a30; --text: #c0d0f0; --dim: #383858;
}
[data-theme="matrix"] {
  --a1: #00ff41; --a1g: rgba(0,255,65,.2);
  --a2: #00cc33; --a2g: rgba(0,204,51,.2);
  --a3: #39ff14; --a3g: rgba(57,255,20,.2);
  --bg: #000; --bg2: #020d02; --card: #030f03;
  --border: #0a280a; --text: #90e890; --dim: #284828;
}
[data-theme="fire"] {
  --a1: #ff8c00; --a1g: rgba(255,140,0,.2);
  --a2: #ff4500; --a2g: rgba(255,69,0,.2);
  --a3: #ff0040; --a3g: rgba(255,0,64,.2);
  --bg: #000; --bg2: #0f0700; --card: #130800;
  --border: #281200; --text: #f0c880; --dim: #583810;
}
[data-theme="ice"] {
  --a1: #a8e6ff; --a1g: rgba(168,230,255,.22);
  --a2: #4fc3f7; --a2g: rgba(79,195,247,.22);
  --a3: #ff5090; --a3g: rgba(255,80,144,.22);
  --bg: #000; --bg2: #030812; --card: #050d1a;
  --border: #0d1e33; --text: #cfe8ff; --dim: #3a5a7a;
}
[data-theme="gold"] {
  --a1: #ffcc00; --a1g: rgba(255,204,0,.22);
  --a2: #ff9500; --a2g: rgba(255,149,0,.22);
  --a3: #d63384; --a3g: rgba(214,51,132,.22);
  --bg: #000; --bg2: #0f0a02; --card: #140e04;
  --border: #2a1e0a; --text: #f5e4b5; --dim: #705a2c;
}
[data-theme="void"] {
  --a1: #c084fc; --a1g: rgba(192,132,252,.24);
  --a2: #6c2bd9; --a2g: rgba(108,43,217,.24);
  --a3: #ff2d78; --a3g: rgba(255,45,120,.24);
  --bg: #000; --bg2: #06030f; --card: #0a0616;
  --border: #201640; --text: #d0c0f0; --dim: #4a3670;
}

/* ── BASE ──────────────────────────────────────────────────────────────────── */
* { box-sizing:border-box; margin:0; padding:0; }
body {
  background: #000;
  background-image: radial-gradient(ellipse 80% 60% at 50% 0%, var(--a2g) 0%, transparent 70%);
  color: var(--text);
  font-family: 'Courier New',Courier,monospace;
  min-height: 100vh; overflow-x: hidden;
  transition: background .4s, color .4s;
}
body::after {
  content:''; position:fixed; inset:0;
  background: repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,0,0,.04) 3px,rgba(0,0,0,.04) 4px);
  pointer-events:none; z-index:9999;
}

/* ── HEADER ────────────────────────────────────────────────────────────────── */
.hdr {
  display:flex; align-items:center; justify-content:space-between;
  padding:8px 18px;
  border-bottom:1px solid var(--a1);
  box-shadow:0 0 25px var(--a1g);
  background:linear-gradient(90deg,var(--bg),var(--bg2),var(--bg));
  position:sticky; top:0; z-index:200;
}
/* ═══ SINDRI LOGO — Forge Nordique ═══════════════════════════════════════ */
.hdr-logo {
  display:flex; align-items:center; gap:14px;
  font-family:'Consolas', 'Courier New', monospace;
  position:relative;
  padding:2px 0;
}

/* ── Emblème hexagonal avec marteau + éclair ── */
.hdr-emblem {
  width:52px; height:58px;
  position:relative;
  flex-shrink:0;
}
.hdr-emblem svg { width:100%; height:100%; overflow:visible; }
.hex-outer {
  fill:none; stroke:var(--a1); stroke-width:1.5;
  filter:drop-shadow(0 0 6px var(--a1));
  transform-origin:center;
  animation:hex-rotate 12s linear infinite;
}
.hex-inner {
  fill:rgba(0,0,0,.6); stroke:var(--a2); stroke-width:.8;
  opacity:.9;
}
@keyframes hex-rotate {
  0%   { transform:rotate(0deg); }
  100% { transform:rotate(360deg); }
}
/* Marteau + éclair au centre */
.emblem-hammer {
  fill:var(--a1); stroke:var(--a1); stroke-width:.5;
  filter:drop-shadow(0 0 4px var(--a1));
  animation:hammer-strike 1.8s ease-in-out infinite;
  transform-origin:20px 32px;
}
@keyframes hammer-strike {
  0%, 60%, 100% { transform:rotate(0deg); }
  70%           { transform:rotate(-18deg); }
  80%           { transform:rotate(3deg); }
  85%           { transform:rotate(-2deg); }
  90%           { transform:rotate(0deg); }
}
.emblem-bolt {
  fill:var(--a3);
  filter:drop-shadow(0 0 5px var(--a3));
  animation:bolt-flash 1.8s ease-in-out infinite;
  opacity:0;
}
@keyframes bolt-flash {
  0%, 65%, 100% { opacity:0; }
  70%, 78%      { opacity:1; }
  82%           { opacity:.4; }
}
/* Étincelles qui giclent au moment du coup */
.emblem-spark {
  fill:var(--a1);
  opacity:0;
  transform-origin:20px 42px;
  animation:spark-fly 1.8s ease-out infinite;
}
.emblem-spark.s2 { animation-delay:.02s; fill:var(--a2); }
.emblem-spark.s3 { animation-delay:.04s; fill:var(--a3); }
.emblem-spark.s4 { animation-delay:.03s; }
@keyframes spark-fly {
  0%, 70%  { opacity:0; transform:translate(0,0) scale(1); }
  75%      { opacity:1; transform:translate(0,0) scale(1); }
  100%     { opacity:0; transform:translate(var(--sx,10px), var(--sy,-14px)) scale(.3); }
}

/* ── Colonne texte : SINDRI + baseline runique ── */
.brand-stack {
  display:flex; flex-direction:column; gap:2px;
  line-height:1;
}
.brand-line {
  font-size:1.3em; font-weight:900; letter-spacing:.35em;
  position:relative; height:1em;
}
.brand-main {
  background:linear-gradient(180deg,
    #ffffff 0%,
    var(--a1) 30%,
    var(--a2) 55%,
    var(--a3) 78%,
    var(--a1) 100%);
  background-size:100% 200%;
  -webkit-background-clip:text;
          background-clip:text;
  color:transparent;
  animation:metal-flow 3.5s ease-in-out infinite;
  filter:drop-shadow(0 0 4px var(--a1g)) drop-shadow(0 0 12px var(--a1g));
  position:relative;
  z-index:2;
}
@keyframes metal-flow {
  0%,100% { background-position:0% 0%; }
  50%     { background-position:0% 100%; }
}
/* Aberration chromatique : ghost rouge et cyan légèrement décalés */
.brand-line::before,
.brand-line::after {
  content:attr(data-txt);
  position:absolute; left:0; top:0;
  font-weight:900; letter-spacing:.35em;
  pointer-events:none; z-index:1;
  animation:chroma-shift 3s ease-in-out infinite;
}
.brand-line::before {
  color:var(--a3); mix-blend-mode:screen; opacity:.55;
  transform:translate(-1.5px,0);
  filter:blur(.4px);
}
.brand-line::after {
  color:var(--a1); mix-blend-mode:screen; opacity:.55;
  transform:translate(1.5px,0);
  filter:blur(.4px);
}
@keyframes chroma-shift {
  0%,100% { transform:translate(-1.5px,0); }
  50%     { transform:translate(1.5px,0); }
}
/* Baseline runique : ligne de métal en fusion sous SINDRI */
.brand-molten {
  width:100%; height:3px; margin-top:1px;
  background:linear-gradient(90deg,
    transparent 0%,
    var(--a3) 15%,
    #ffcc00 35%,
    #ffffff 50%,
    #ffcc00 65%,
    var(--a3) 85%,
    transparent 100%);
  background-size:200% auto;
  animation:molten-flow 3s linear infinite;
  filter:drop-shadow(0 0 4px var(--a3)) blur(.3px);
  border-radius:3px;
}
@keyframes molten-flow {
  0%   { background-position:0% center; }
  100% { background-position:200% center; }
}
/* Sous-titre avec runes Elder Futhark */
.brand-subtitle {
  font-size:.5em; letter-spacing:.4em;
  color:var(--dim);
  margin-top:4px;
  display:flex; gap:8px; align-items:center;
  text-shadow:0 0 4px var(--bg);
}
.brand-subtitle .rune {
  color:var(--a1);
  font-size:1.2em;
  text-shadow:0 0 4px var(--a1);
  animation:rune-pulse 2s ease-in-out infinite;
}
.brand-subtitle .rune.r2 { color:var(--a2); text-shadow:0 0 4px var(--a2); animation-delay:.4s; }
.brand-subtitle .rune.r3 { color:var(--a3); text-shadow:0 0 4px var(--a3); animation-delay:.8s; }
@keyframes rune-pulse {
  0%,100% { opacity:.55; }
  50%     { opacity:1; }
}

/* ── Badge v3 hexagonal ── */
.hdr-version {
  width:32px; height:36px;
  position:relative;
  flex-shrink:0;
}
.hdr-version svg { width:100%; height:100%; overflow:visible; }
.v-hex-border {
  fill:rgba(255,0,68,.08); stroke:var(--a3); stroke-width:1.5;
  filter:drop-shadow(0 0 6px var(--a3));
  animation:v-hex-glow 2.4s ease-in-out infinite;
}
@keyframes v-hex-glow {
  0%,100% { filter:drop-shadow(0 0 3px var(--a3)); }
  50%     { filter:drop-shadow(0 0 10px var(--a3)) drop-shadow(0 0 18px var(--a3g)); }
}
.hdr-version .v-label {
  position:absolute; inset:0;
  display:flex; align-items:center; justify-content:center;
  font-size:.7em; font-weight:900; letter-spacing:.1em;
  color:var(--a3); text-shadow:0 0 6px var(--a3);
}
.hdr-clock { font-size:1.25em; color:var(--a2); text-shadow:0 0 10px var(--a2); letter-spacing:.1em; }
.dot { display:inline-block; width:7px; height:7px; border-radius:50%;
  background:var(--a1); box-shadow:0 0 8px var(--a1);
  animation:blink 1.2s ease-in-out infinite; margin-right:5px; }
.dot.warn { background:#ff8c00; box-shadow:0 0 8px #ff8c00; }
.dot.crit { background:var(--a3); box-shadow:0 0 8px var(--a3); animation:blink .4s ease-in-out infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.15} }

/* ── TOOLBAR ───────────────────────────────────────────────────────────────── */
.toolbar {
  display:flex; align-items:center; gap:6px; flex-wrap:wrap;
  padding:6px 18px;
  border-bottom:1px solid var(--border);
  background:var(--bg2);
}
.tbtn {
  background:transparent; border:1px solid var(--dim); color:var(--dim);
  font-family:inherit; font-size:.62em; letter-spacing:.1em;
  padding:3px 10px; border-radius:3px; cursor:pointer;
  transition:all .2s;
}
.tbtn:hover, .tbtn.active {
  border-color:var(--a1); color:var(--a1); box-shadow:0 0 8px var(--a1g);
}
.tbtn.t-matrix.active { border-color:#00ff41; color:#00ff41; box-shadow:0 0 8px rgba(0,255,65,.3); }
.tbtn.t-fire.active   { border-color:#ff8c00; color:#ff8c00; box-shadow:0 0 8px rgba(255,140,0,.3); }
.tbtn.danger { border-color:var(--a3); color:var(--a3); }
.tbtn.danger:hover { box-shadow:0 0 8px var(--a3g); }
.tbtn.export { border-color:var(--a2); color:var(--a2); }
.tbtn.export:hover { box-shadow:0 0 8px var(--a2g); }
.toolbar-sep { width:1px; height:18px; background:var(--border); }

/* ── CONTROL BAR (forge) ─────────────────────────────────────────────────── */
.ctrl-bar {
  display:flex; align-items:center; gap:6px; flex-wrap:wrap;
  padding:6px 18px;
  border-bottom:1px solid var(--border);
  background:linear-gradient(90deg,rgba(255,45,120,.04),rgba(0,255,249,.04),rgba(180,74,255,.04));
}
.ctrl-lbl {
  font-size:.58em; color:var(--dim); letter-spacing:.18em;
  text-transform:uppercase; padding:0 4px;
}
.ctrl-btn {
  background:transparent; border:1px solid var(--dim); color:var(--text);
  font-family:inherit; font-size:.65em; letter-spacing:.06em;
  padding:4px 10px; border-radius:3px; cursor:pointer;
  transition:all .15s;
}
.ctrl-btn:hover { border-color:var(--a1); color:var(--a1); box-shadow:0 0 6px var(--a1g); }
.ctrl-btn.preset { font-weight:bold; }
.ctrl-btn.preset-gaming  { border-color:var(--a3); color:var(--a3); }
.ctrl-btn.preset-office  { border-color:var(--a1); color:var(--a1); }
.ctrl-btn.preset-silence { border-color:var(--a2); color:var(--a2); }
.ctrl-btn.preset.active  {
  background:currentColor;
  color:#000 !important;
  box-shadow:0 0 10px currentColor;
}
.ctrl-btn.preset-gaming.active  { background:var(--a3); }
.ctrl-btn.preset-office.active  { background:var(--a1); }
.ctrl-btn.preset-silence.active { background:var(--a2); }
.ctrl-btn.danger { border-color:#ff8c00; color:#ff8c00; }
.ctrl-btn.danger:hover { border-color:var(--a3); color:var(--a3); box-shadow:0 0 8px var(--a3g); }

/* ── MODAL ────────────────────────────────────────────────────────────────── */
.modal {
  display:none; position:fixed; inset:0;
  background:rgba(0,0,0,.75); backdrop-filter:blur(4px);
  z-index:1000; align-items:center; justify-content:center;
}
.modal.open { display:flex; }
.modal-box {
  background:var(--card); border:1px solid var(--a1);
  box-shadow:0 0 40px var(--a1g);
  border-radius:6px; padding:0; min-width:480px; max-width:90vw;
  animation:modal-in .2s ease;
}
@keyframes modal-in { from{opacity:0;transform:scale(.9);} to{opacity:1;transform:scale(1);} }
.modal-head {
  font-size:.75em; letter-spacing:.15em; color:var(--a1);
  padding:12px 16px; border-bottom:1px solid var(--border);
  text-shadow:0 0 8px var(--a1);
}
.modal-body { padding:16px; }
.mrow { margin-bottom:12px; }
.mrow label {
  display:block; font-size:.58em; color:var(--dim);
  letter-spacing:.12em; margin-bottom:4px; text-transform:uppercase;
}
.mrow input[type=text], .mrow select {
  width:100%; background:#000; border:1px solid var(--border);
  color:var(--text); font-family:inherit; font-size:.75em;
  padding:6px 8px; border-radius:3px;
}
.mrow input[type=text]:focus, .mrow select:focus {
  outline:none; border-color:var(--a1); box-shadow:0 0 6px var(--a1g);
}

/* ── Kill safety : badge SYS + row critique + warning ─────────────────────── */
/* ── Certification MESURÉ / ESTIMÉ ─────────────────────────────────────────── */
.tag-real, .tag-est {
  display:inline-block; margin-left:6px;
  font-size:.55em; font-weight:bold;
  padding:0 4px; border-radius:2px;
  letter-spacing:.1em; vertical-align:middle;
}
.tag-real { background:rgba(0,255,120,.2); color:#00ff88; border:1px solid #00ff88; }
.tag-est  { background:rgba(255,140,0,.2); color:#ff8c00; border:1px solid #ff8c00; }

.crit-tag {
  display:inline-block; margin-left:6px;
  font-size:.8em; font-weight:bold;
  padding:0 4px; border-radius:2px;
  background:rgba(255,0,68,.2); color:#ff5090;
  border:1px solid #ff0044; letter-spacing:.1em;
}
.proc-critical .proc-name { color:#ff8090; }
.kill-sys { border-color:#ff0044 !important; color:#ff0044 !important; }
.kill-sys:hover { background:rgba(255,0,68,.15) !important; }
#kill-warn {
  display:none; padding:10px; margin:8px 0;
  background:rgba(255,0,68,.15); border:1px solid #ff0044;
  color:#ff5090; font-size:.7em; border-radius:4px;
  animation:blink 1s ease-in-out infinite;
}
#proc-search:focus {
  outline:none; border-color:var(--a1); box-shadow:0 0 6px var(--a1g);
}
.sound-toggle { font-size:.75em; cursor:pointer; color:var(--dim); padding:3px 6px; }
.sound-toggle:hover { color:var(--a1); }

/* ── ALERT BANNER ──────────────────────────────────────────────────────────── */
.alert-banner {
  flex:1; min-width:200px; padding:3px 10px; border-radius:3px;
  font-size:.68em; letter-spacing:.08em; font-weight:bold;
  display:none; text-align:center;
}
.alert-banner.warn { display:block; background:rgba(255,140,0,.15); border:1px solid #ff8c00; color:#ff8c00; }
.alert-banner.crit { display:block; background:rgba(255,0,64,.15); border:1px solid var(--a3); color:var(--a3);
  animation:blink .6s ease-in-out infinite; }

/* ── GRIDS ─────────────────────────────────────────────────────────────────── */
.grid-main   { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; padding:12px 12px 0; }
.grid-mid    { display:grid; grid-template-columns:2fr 1fr 1fr; gap:10px; padding:10px 12px 0; }
.grid-bottom { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; padding:10px 12px 12px; }

/* ── CARD ──────────────────────────────────────────────────────────────────── */
.card {
  background:var(--card); border:1px solid var(--border);
  border-radius:6px; padding:12px; position:relative; overflow:hidden;
  transition:border-color .3s;
}
.card::before { content:''; position:absolute; top:0; left:10%; right:10%; height:1px; }
.card.c1::before { background:linear-gradient(90deg,transparent,var(--a1),transparent); }
.card.c2::before { background:linear-gradient(90deg,transparent,var(--a2),transparent); }
.card.c3::before { background:linear-gradient(90deg,transparent,var(--a3),transparent); }
.card::after { content:''; position:absolute; bottom:4px; right:4px;
  width:8px; height:8px; border-bottom:1px solid var(--dim); border-right:1px solid var(--dim); }
.card-title { font-size:.58em; letter-spacing:.22em; color:var(--dim); text-transform:uppercase; margin-bottom:10px; }
.card.alert-card { border-color:var(--a3); box-shadow:0 0 12px var(--a3g); }

/* ── Card alerts par catégorie (warn = orange, crit = rouge pulsant) ─────── */
.card.warn {
  border-color:#ff8c00 !important;
  animation:card-warn 1.5s ease-in-out infinite;
}
.card.warn .card-title { color:#ff8c00 !important; }
.card.warn::before { background:linear-gradient(90deg,transparent,#ff8c00,transparent) !important; }

.card.crit {
  border-color:#ff0044 !important;
  animation:card-crit .7s ease-in-out infinite;
  z-index:5;
}
.card.crit .card-title {
  color:#ff0044 !important;
  text-shadow:0 0 8px #ff0044;
  animation:blink .5s ease-in-out infinite;
}
.card.crit::before { background:linear-gradient(90deg,transparent,#ff0044,transparent) !important; }
.card.crit::after { border-color:#ff0044 !important; box-shadow:0 0 6px #ff0044; }

@keyframes card-warn {
  0%,100% { box-shadow:0 0 10px rgba(255,140,0,.35), inset 0 0 15px rgba(255,140,0,.05); }
  50%     { box-shadow:0 0 22px rgba(255,140,0,.7),  inset 0 0 25px rgba(255,140,0,.15); }
}
@keyframes card-crit {
  0%,100% {
    box-shadow:0 0 15px rgba(255,0,68,.6),  inset 0 0 20px rgba(255,0,68,.15);
    border-color:#ff0044;
  }
  50%     {
    box-shadow:0 0 35px rgba(255,0,68,1),   inset 0 0 30px rgba(255,0,68,.4);
    border-color:#ff5090;
  }
}

/* Badge "!" en haut à droite quand carte alerte */
.card.crit::after,
.card.warn::after { display:none; }
.card.warn > .card-alert-badge,
.card.crit > .card-alert-badge {
  position:absolute; top:6px; right:8px;
  font-size:.9em; font-weight:bold;
  padding:1px 6px; border-radius:3px;
  animation:blink .6s ease-in-out infinite;
  z-index:10;
}
.card.warn > .card-alert-badge {
  background:rgba(255,140,0,.2); color:#ff8c00; border:1px solid #ff8c00;
}
.card.crit > .card-alert-badge {
  background:rgba(255,0,68,.25); color:#ff5090;
  border:1px solid #ff0044; text-shadow:0 0 6px #ff0044;
}

/* ── HEAL "EAU FROIDE" : cadre bleu glacé qui override warn/crit ────────── */
@keyframes heal-pulse {
  0%, 100% {
    box-shadow: 0 0 14px rgba(125,215,255,.6), inset 0 0 18px rgba(125,215,255,.15);
    border-color: #7dd7ff;
  }
  50% {
    box-shadow: 0 0 40px rgba(168,230,255,1), inset 0 0 34px rgba(168,230,255,.4);
    border-color: #a8e6ff;
  }
}
/* Gouttes d'eau qui coulent depuis le haut */
@keyframes drip {
  0%   { top:-10px; opacity:0; }
  10%  { opacity:1; }
  100% { top:calc(100% + 4px); opacity:0; }
}
.card.healing {
  border-color: #7dd7ff !important;
  animation: heal-pulse .55s ease-in-out infinite !important;
  position:relative;
  overflow:hidden;
}
.card.healing > .card-alert-badge { display:none !important; }
.card.healing::after {
  content: '💧 EAU FROIDE';
  position:absolute; top:6px; right:8px;
  font-size:.62em; letter-spacing:.15em; font-weight:bold;
  padding:2px 8px; border-radius:3px;
  background:rgba(125,215,255,.18); color:#a8e6ff;
  border:1px solid #7dd7ff; text-shadow:0 0 6px #7dd7ff;
  animation:blink .55s ease-in-out infinite;
  z-index:11;
}
/* Gouttes de vapeur simulées via ::before */
.card.healing::before {
  content: '';
  position:absolute; left:20%; top:-10px;
  width:6px; height:14px;
  background:linear-gradient(180deg, transparent, #a8e6ff);
  border-radius:50% 50% 40% 40%;
  filter:blur(1px);
  animation:drip 1.4s ease-in infinite;
  pointer-events:none;
  box-shadow: 40px -20px 0 -1px rgba(168,230,255,.6),
              90px 10px 0 -2px rgba(168,230,255,.5),
             140px -30px 0 -1px rgba(168,230,255,.7);
}
.heal-btn {
  cursor:pointer; font-size:.7em; color:var(--dim); padding:0 6px;
  transition:color .2s, text-shadow .2s;
  user-select:none;
}
.heal-btn:hover { color:#00ff88; text-shadow:0 0 6px #00ff88; }
.heal-mega {
  background:linear-gradient(135deg,#1a0f10,#0e0a0b);
  border:1px solid #55666a; color:#8ea3a8; font-family:inherit;
  padding:6px 16px; font-size:.72em; letter-spacing:.2em; font-weight:bold;
  border-radius:4px; cursor:pointer; text-shadow:none;
  box-shadow:none;
  transition:all .25s;
}
.heal-mega:hover {
  border-color:#00ff88; color:#00ff88;
  box-shadow:0 0 10px rgba(0,255,136,.4);
  transform:translateY(-1px);
}
.heal-mega.on {
  background:linear-gradient(135deg,#00332f,#00201d);
  border-color:#00ff88; color:#00ff88; text-shadow:0 0 6px #00ff88;
  box-shadow:0 0 14px rgba(0,255,136,.55), inset 0 0 10px rgba(0,255,136,.25);
  animation:heal-toggle-pulse 2s ease-in-out infinite;
}
@keyframes heal-toggle-pulse {
  0%,100% { box-shadow:0 0 10px rgba(0,255,136,.4),  inset 0 0 8px  rgba(0,255,136,.2); }
  50%     { box-shadow:0 0 22px rgba(0,255,136,.85), inset 0 0 14px rgba(0,255,136,.4); }
}
.heal-mega.on::before {
  content:'●'; margin-right:6px; color:#00ff88;
  animation:blink 1.2s ease-in-out infinite;
}
.heal-mega:active { transform:translateY(0); }

/* ── 270° GAUGE ─────────────────────────────────────────────────────────────── */
/* r=50 in 120×120 viewBox → C=314.2, 270° arc=235.6, dasharray "235.6 314.2" */
.gauge-wrap { display:flex; flex-direction:column; align-items:center; margin-bottom:8px; }
.gauge-svg  { width:118px; height:118px; overflow:visible; }
.g-track { fill:none; stroke:#141428; stroke-width:9; }
.g-glow  {
  fill:none; stroke-width:9; stroke-linecap:round; opacity:.2;
  stroke-dasharray:235.6 314.2; stroke-dashoffset:235.6;
  transform:rotate(135deg); transform-origin:50% 50%;
  transition:stroke-dashoffset .5s ease; filter:blur(5px);
}
.g-fill  {
  fill:none; stroke-width:9; stroke-linecap:round;
  stroke-dasharray:235.6 314.2; stroke-dashoffset:235.6;
  transform:rotate(135deg); transform-origin:50% 50%;
  transition:stroke-dashoffset .5s cubic-bezier(.4,0,.2,1), stroke .3s ease;
}
.gauge-outer { position:relative; display:inline-block; }
.gauge-center { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; }
.gauge-val  { font-size:1.55em; font-weight:bold; line-height:1; }
.gauge-unit { font-size:.5em; color:var(--dim); letter-spacing:.12em; margin-top:1px; }
.gauge-sub  { font-size:.52em; letter-spacing:.06em; margin-top:3px; }

/* ── STAT ROWS ──────────────────────────────────────────────────────────────── */
.srow { display:flex; justify-content:space-between; align-items:center;
  padding:3px 0; font-size:.7em; border-bottom:1px solid #0e0e20; }
.srow:last-child { border-bottom:none; }
.slabel { color:var(--dim); }
.sval   { font-weight:bold; }

/* ── BARS ───────────────────────────────────────────────────────────────────── */
.bar-head { display:flex; justify-content:space-between; font-size:.6em; color:var(--dim); margin-bottom:2px; }
.bar-track { height:4px; background:#141428; border-radius:2px; overflow:hidden; }
.bar-fill  { height:100%; border-radius:2px; transition:width .5s ease; }

/* ── CORE GRID ───────────────────────────────────────────────────────────────── */
.core-grid { display:grid; gap:3px; margin-top:4px; }
.core-cell {
  height:16px; border-radius:2px;
  display:flex; align-items:center; justify-content:center;
  font-size:.5em; font-weight:bold;
  transition:background .4s; color:rgba(0,0,0,.75);
}

/* ── SPARKLINE ───────────────────────────────────────────────────────────────── */
.spark { width:100%; height:40px; margin-top:6px; cursor:crosshair; }
.spark-wrap { position:relative; margin-top:6px; }
.spark-wrap .spark { margin-top:0; }
.spark-info {
  position:absolute; top:2px; right:4px; font-size:.55em;
  color:var(--dim); letter-spacing:.08em; pointer-events:none;
  text-shadow:0 0 4px var(--bg);
}
.spark-info .spark-max { color:var(--a3); }
.spark-info .spark-now { color:var(--a1); }
.spark-tip {
  position:absolute; pointer-events:none; display:none;
  background:rgba(0,0,0,.85); border:1px solid var(--a1);
  color:var(--a1); font-size:.6em; padding:2px 6px;
  border-radius:3px; white-space:nowrap; z-index:20;
  transform:translate(-50%, -100%); margin-top:-4px;
}
.spark-cursor {
  stroke:var(--a1); stroke-width:1; stroke-dasharray:2,2;
  opacity:0; pointer-events:none;
}

/* ── FAN GRID ────────────────────────────────────────────────────────────────── */
.fan-item { display:flex; justify-content:space-between; align-items:center; padding:2px 0; font-size:.65em; border-bottom:1px solid #0e0e20; }
.fan-name  { color:var(--dim); flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fan-rpm   { font-weight:bold; flex-shrink:0; margin-left:6px; }

/* ── POWER ───────────────────────────────────────────────────────────────────── */
.big-num { font-size:2em; font-weight:bold; line-height:1; }
.power-footer {
  background:linear-gradient(90deg,var(--bg),var(--bg2),var(--bg));
  border-top:1px solid var(--a3); box-shadow:0 -4px 20px var(--a3g);
  padding:8px 18px; display:flex; gap:20px; align-items:center; flex-wrap:wrap;
  font-size:.72em;
}
.pf-label { color:var(--a3); letter-spacing:.2em; text-shadow:0 0 8px var(--a3); }
.pf-val b { color:var(--a3); text-shadow:0 0 6px var(--a3); }
.pf-ping b { color:var(--a1); text-shadow:0 0 6px var(--a1); }

/* ── TEMP LIST ───────────────────────────────────────────────────────────────── */
.temp-list { max-height:160px; overflow-y:auto; }
.temp-list::-webkit-scrollbar { width:3px; }
.temp-list::-webkit-scrollbar-thumb { background:var(--a2); border-radius:2px; }
.temp-item { display:flex; justify-content:space-between; padding:2px 0; font-size:.63em; border-bottom:1px solid #0e0e20; }
.temp-name { color:var(--dim); flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.temp-deg  { font-weight:bold; flex-shrink:0; margin-left:6px; }

/* ── COLORS ──────────────────────────────────────────────────────────────────── */
.c1  { color:var(--a1); text-shadow:0 0 8px var(--a1g); }
.c2  { color:var(--a2); text-shadow:0 0 8px var(--a2g); }
.c3  { color:var(--a3); text-shadow:0 0 8px var(--a3g); }
.cd  { color:var(--dim); }
.co  { color:#ff8c00;  text-shadow:0 0 6px rgba(255,140,0,.4); }

/* ── HUD OVERLAY ─────────────────────────────────────────────────────────────── */
#hud {
  position:fixed; top:60px; right:16px; z-index:500;
  background:rgba(4,4,8,.88); border:1px solid var(--a1);
  border-radius:8px; padding:14px 18px;
  box-shadow:0 0 20px var(--a1g); backdrop-filter:blur(4px);
  min-width:200px; display:none;
  flex-direction:column; gap:8px;
  cursor:move; user-select:none;
}
#hud.visible { display:flex; }
.hud-row { display:flex; justify-content:space-between; align-items:center; gap:16px; font-size:.85em; }
.hud-lbl  { color:var(--dim); letter-spacing:.1em; font-size:.75em; }
.hud-val  { font-weight:bold; font-size:1.2em; }
.hud-title { font-size:.55em; letter-spacing:.3em; color:var(--dim); border-bottom:1px solid var(--border); padding-bottom:4px; margin-bottom:2px; }
#hud-close { position:absolute; top:6px; right:8px; cursor:pointer; font-size:.7em; color:var(--dim); }
#hud-close:hover { color:var(--a3); }

@keyframes pulse { 0%,100%{opacity:1}50%{opacity:.55} }
.pulse { animation:pulse 2s ease-in-out infinite; }

/* Scrollbar */
::-webkit-scrollbar { width:4px; }
::-webkit-scrollbar-thumb { background:var(--dim); border-radius:2px; }

/* Process list */
.proc-row {
  display:grid; grid-template-columns:44px 1fr 56px 68px 36px;
  align-items:center; gap:6px;
  padding:3px 12px; font-size:.66em;
  border-bottom:1px solid #0e0e20;
  transition:background .15s;
}
.proc-row:hover { background:rgba(255,255,255,.03); }
.proc-pid  { color:var(--dim); text-align:right; }
.proc-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.proc-cpu  { text-align:right; font-weight:bold; }
.proc-ram  { text-align:right; color:var(--a2); }
.kill-btn  {
  background:transparent; border:1px solid var(--a3); color:var(--a3);
  font-size:.85em; padding:1px 6px; border-radius:3px; cursor:pointer;
  font-family:inherit; transition:all .15s;
}
.kill-btn:hover { background:var(--a3); color:#000; }

/* Net process row */
.net-row {
  display:grid; grid-template-columns:44px 1fr 48px 1fr;
  align-items:center; gap:6px;
  padding:4px 12px; font-size:.64em;
  border-bottom:1px solid #0e0e20;
  transition:background .15s;
}
.net-row:hover { background:rgba(255,255,255,.03); }
.net-pid   { color:var(--dim); text-align:right; }
.net-name  { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:bold; }
.net-conns { text-align:center; font-weight:bold; }
.net-remotes { color:var(--dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.88em; }
.net-bar-wrap { grid-column:1/-1; padding:0 0 3px; }
.net-bar-mini { height:2px; background:var(--border); border-radius:1px; margin-top:2px; }
.net-bar-fill { height:100%; border-radius:1px; transition:width .4s ease; }

/* Event row */
.evt-row { display:flex; gap:8px; padding:4px 0; font-size:.63em; border-bottom:1px solid #0e0e20; align-items:flex-start; }
.evt-row:last-child { border-bottom:none; }
.evt-lvl { flex-shrink:0; font-weight:bold; padding:1px 6px; border-radius:2px; font-size:.9em; }
.evt-lvl.crit  { background:rgba(255,0,64,.2);   color:var(--a3); }
.evt-lvl.error { background:rgba(255,45,120,.2);  color:#ff6b9d; }
.evt-lvl.warn  { background:rgba(255,140,0,.2);   color:#ff8c00; }
.evt-body { flex:1; min-width:0; }
.evt-src  { color:var(--a1); }
.evt-msg  { color:var(--dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.evt-time { flex-shrink:0; color:var(--dim); font-size:.85em; }
</style>
</head>
<body data-theme="neon">

<!-- HEADER -->
<div class="hdr">
  <div class="hdr-logo">

    <!-- Emblème hexagonal : marteau qui frappe + éclair + étincelles -->
    <div class="hdr-emblem">
      <svg viewBox="-4 -4 48 54">
        <!-- Anneaux hexagonaux -->
        <polygon class="hex-outer" points="20,0 40,10 40,32 20,42 0,32 0,10"/>
        <polygon class="hex-inner" points="20,3 37,12 37,30 20,39 3,30 3,12"/>
        <!-- Enclume (base solide) -->
        <path d="M 8,32 L 32,32 L 30,28 L 24,28 L 22,24 L 18,24 L 16,28 L 10,28 Z" fill="var(--a2)" opacity=".7" stroke="var(--a2)" stroke-width=".4"/>
        <!-- Marteau qui frappe (pivot bas gauche) -->
        <g class="emblem-hammer">
          <!-- Manche -->
          <rect x="19" y="7" width="2" height="18" rx=".5"/>
          <!-- Tête du marteau -->
          <rect x="14" y="4" width="12" height="6" rx="1"/>
        </g>
        <!-- Éclair qui frappe l'enclume -->
        <polygon class="emblem-bolt" points="19,10 22,18 20,18 23,26 17,17 20,17"/>
        <!-- Étincelles (positions randomisées via --sx/--sy) -->
        <circle class="emblem-spark s1" cx="20" cy="30" r="1.1" style="--sx:-9px; --sy:-11px;"/>
        <circle class="emblem-spark s2" cx="20" cy="30" r="0.9" style="--sx:9px; --sy:-13px;"/>
        <circle class="emblem-spark s3" cx="20" cy="30" r="1"   style="--sx:-4px; --sy:-16px;"/>
        <circle class="emblem-spark s4" cx="20" cy="30" r="0.7" style="--sx:6px; --sy:-10px;"/>
      </svg>
    </div>

    <!-- Bloc texte SINDRI + baseline métal en fusion + runes ENERGY THERMAL SYSTEMS -->
    <div class="brand-stack">
      <div class="brand-line" data-txt="SINDRI">
        <span class="brand-main">SINDRI</span>
      </div>
      <div class="brand-molten"></div>
      <div class="brand-subtitle">
        <span class="rune">ᛖ</span>ENERGY
        <span class="rune r2">ᛏ</span>THERMAL
        <span class="rune r3">ᛊ</span>SYSTEMS
      </div>
    </div>

    <!-- Badge v3 hexagonal -->
    <div class="hdr-version">
      <svg viewBox="-3 -3 46 42">
        <polygon class="v-hex-border" points="20,0 37,9 37,27 20,36 3,27 3,9"/>
      </svg>
      <span class="v-label">v3</span>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;font-size:.65em;">
    <span class="dot" id="status-dot"></span>
    <span id="status-txt" class="cd">CONNECTING...</span>
    <span id="self-usage" class="cd" style="color:var(--dim);border-left:1px solid var(--border);padding-left:8px;margin-left:2px;" title="Ressources utilisées par le PULSE lui-même">
      ▸ DASH · <span id="self-cpu">--</span>% · <span id="self-ram">--</span>MB
    </span>
  </div>
  <div class="hdr-clock" id="clock">--:--:--</div>
</div>

<!-- TOOLBAR -->
<div class="toolbar">
  <button class="tbtn danger" onclick="toggleHUD()">HUD [G]</button>
  <button class="tbtn export" onclick="exportCSV()">⬇ CSV</button>
  <div class="toolbar-sep"></div>
  <button class="heal-mega" id="heal-toggle-btn" onclick="toggleAutoHeal()" title="Auto-heal : quand activé, les règles cochées dans ⚙ se déclenchent automatiquement">✚ HEAL: OFF</button>
  <span id="heal-active-strip" style="display:none;font-size:.62em;color:#7dd7ff;padding:4px 10px;border:1px solid #7dd7ff;border-radius:3px;background:rgba(125,215,255,.08);letter-spacing:.1em;text-shadow:0 0 4px #7dd7ff;box-shadow:0 0 8px rgba(125,215,255,.35);"></span>
  <div class="toolbar-sep"></div>
  <span class="sound-toggle" id="sound-btn" onclick="toggleSound()" title="Alertes sonores">🔔</span>
  <div class="alert-banner" id="alert-banner"></div>
</div>

<!-- ── CONTROL BAR (presets + actions + settings) ─────────────────────────── -->
<div class="ctrl-bar">
  <span class="ctrl-lbl">// FORGE</span>
  <button class="ctrl-btn preset preset-gaming"  onclick="applyPreset('gaming')" title="Plan haute perf + GPU 100%">⚡ GAMING</button>
  <button class="ctrl-btn preset preset-office"  onclick="applyPreset('office')" title="Plan équilibré + GPU 85%">📊 OFFICE</button>
  <button class="ctrl-btn preset preset-silence" onclick="applyPreset('silence')" title="Économie + GPU 60%">🌙 SILENCE</button>
  <span id="preset-cur" class="ctrl-lbl" style="margin-left:6px;"></span>

  <div class="toolbar-sep"></div>

  <span class="ctrl-lbl">GPU PL</span>
  <input type="range" id="gpu-pl-slider" min="100" max="300" value="200"
         style="width:110px;accent-color:var(--a3);cursor:pointer;"
         oninput="document.getElementById('gpu-pl-val').textContent=this.value+'W'"
         onchange="setGpuPowerLimit(this.value)">
  <span id="gpu-pl-val" style="font-size:.65em;color:var(--a3);min-width:38px;">--W</span>

  <div class="toolbar-sep"></div>

  <button class="ctrl-btn" onclick="doAction('empty_recycle','Vider la Corbeille ?')" title="Vider la Corbeille">🗑</button>
  <button class="ctrl-btn" onclick="doAction('clean_temp','Nettoyer %TEMP% ?')" title="Nettoyer %TEMP%">🧹</button>
  <button class="ctrl-btn" onclick="doAction('flush_dns',null)" title="Vider le cache DNS">📡 DNS</button>
  <button class="ctrl-btn danger" onclick="doAction('reboot_uefi','Redémarrer vers UEFI/BIOS dans 10s ?')" title="Redémarrer dans le BIOS UEFI">⚙ UEFI</button>

  <button class="ctrl-btn" style="margin-left:auto;" onclick="openSettings()" title="Paramètres (refresh, thème, alertes…)">⚙ OPTIONS</button>
</div>

<!-- ── POWER CONFIG MODAL ─────────────────────────────────────────────────── -->
<div id="power-config-modal" class="modal">
  <div class="modal-box" style="min-width:520px;">
    <div class="modal-head">⚙ Configuration hardware — Conso PC certifiée</div>
    <div class="modal-body">
      <div style="font-size:.62em;color:var(--dim);margin-bottom:10px;line-height:1.5;">
        Ajuste les composants de ton PC pour obtenir une estimation précise de la conso
        électrique à la prise. Plus c'est précis, plus le kWh est fiable.
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
        <div class="mrow">
          <label>Efficacité PSU (%)</label>
          <select id="pc-eff" style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
            <option value="82">80+ White (82%)</option>
            <option value="85">80+ Bronze (85%)</option>
            <option value="87">80+ Silver (87%)</option>
            <option value="90">80+ Gold (90%)</option>
            <option value="92">80+ Platinum (92%)</option>
            <option value="94">80+ Titanium (94%)</option>
          </select>
        </div>
        <div class="mrow">
          <label>Pertes idle PSU (W)</label>
          <input id="pc-idle" type="number" min="0" max="30" step="1" style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
        </div>
        <div class="mrow">
          <label>RAM totale (GB)</label>
          <input id="pc-ram" type="number" min="4" max="512" step="4" style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
        </div>
        <div class="mrow">
          <label>Ventilateurs (nombre)</label>
          <input id="pc-fans" type="number" min="0" max="20" step="1" style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
        </div>
        <div class="mrow">
          <label>SSD NVMe (nombre)</label>
          <input id="pc-nvme" type="number" min="0" max="10" step="1" style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
        </div>
        <div class="mrow">
          <label>SSD SATA (nombre)</label>
          <input id="pc-sata" type="number" min="0" max="10" step="1" style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
        </div>
        <div class="mrow">
          <label>HDD (disques mécaniques)</label>
          <input id="pc-hdd" type="number" min="0" max="20" step="1" style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
        </div>
        <div class="mrow">
          <label>Périphériques RGB/USB (W)</label>
          <input id="pc-extra" type="number" min="0" max="100" step="1" style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
        </div>
        <div class="mrow" style="grid-column:1/-1;">
          <label>🖥 Écrans — override manuel (W, vide = auto depuis résolution/Hz)</label>
          <input id="pc-screens" type="number" min="0" max="500" step="1" placeholder="Auto"
                 style="width:100%;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
          <div id="pc-screens-info" style="font-size:.6em;color:var(--dim);margin-top:4px;">Auto : --</div>
        </div>
      </div>
      <div style="border-top:1px solid var(--border);margin:12px 0 8px;"></div>
      <div class="mrow" style="display:flex;gap:8px;">
        <button class="ctrl-btn preset preset-gaming" onclick="savePowerConfig()">✓ Enregistrer</button>
        <button class="ctrl-btn" onclick="closePowerConfig()" style="margin-left:auto;">Fermer</button>
      </div>
    </div>
  </div>
</div>

<!-- ── SMART MODAL ─────────────────────────────────────────────────────────── -->
<div id="smart-modal" class="modal">
  <div class="modal-box" style="min-width:600px;max-width:800px;max-height:85vh;overflow-y:auto;">
    <div class="modal-head" style="display:flex;justify-content:space-between;align-items:center;">
      <span>🩺 SMART — Santé des disques</span>
      <span onclick="closeSmartModal()" style="cursor:pointer;color:var(--dim);font-size:1.2em;">✕</span>
    </div>
    <div class="modal-body" id="smart-body">
      <div style="text-align:center;color:var(--dim);padding:20px;">Chargement...</div>
    </div>
  </div>
</div>

<!-- ── SETTINGS MODAL ─────────────────────────────────────────────────────── -->
<div id="settings-modal" class="modal">
  <div class="modal-box">
    <div class="modal-head">⚙ Paramètres — SINDRI v3</div>
    <div class="modal-body">

      <!-- SECTION INTERFACE : thème + refresh -->
      <div class="modal-head" style="border:none;padding:0 0 8px 0;">🎨 Interface</div>
      <div class="mrow" style="display:flex;gap:8px;align-items:center;">
        <label style="flex:1;">Thème</label>
        <select id="s-theme" onchange="setTheme(this.value)"
                style="width:180px;">
          <option value="neon">⚡ NEON (cyan/violet/rouge)</option>
          <option value="matrix">🟢 MATRIX (vert)</option>
          <option value="fire">🔥 FIRE (orange/rouge)</option>
          <option value="ice">🧊 ICE (bleu/blanc)</option>
          <option value="gold">🏆 GOLD (or/ambre)</option>
          <option value="void">🌑 VOID (violet foncé)</option>
        </select>
      </div>
      <div class="mrow" style="display:flex;gap:8px;align-items:center;">
        <label style="flex:1;">Intervalle de rafraîchissement</label>
        <select id="s-refresh" onchange="setInterval2(this.value)"
                style="width:180px;">
          <option value="0.1">0.1 s ⚡ ultra rapide (charge CPU)</option>
          <option value="0.25">0.25 s ⚡ très rapide</option>
          <option value="0.5">0.5 s (rapide)</option>
          <option value="1">1 s</option>
          <option value="2" selected>2 s (défaut)</option>
          <option value="5">5 s (économe)</option>
        </select>
      </div>
      <div style="font-size:.6em;color:var(--dim);margin-bottom:8px;line-height:1.5;">
        ⚠ 0.1s = collecte 10×/s → hausse notable du CPU consommé par le dashboard (visible dans "▸ DASH"). À utiliser pendant les benchs.
      </div>

      <div style="border-top:1px solid var(--border);margin:12px 0;"></div>
      <div class="modal-head" style="border:none;padding:0 0 8px 0;">🔔 Webhooks</div>

      <div class="mrow">
        <label>URL Webhook (Discord / Telegram / autre)</label>
        <input id="s-webhook-url" type="text"
               placeholder="https://discord.com/api/webhooks/... ou https://api.telegram.org/bot.../sendMessage?chat_id=...">
      </div>
      <div class="mrow" style="display:flex;gap:8px;align-items:center;">
        <label style="flex:1;">Niveau minimum d'alerte</label>
        <select id="s-webhook-level">
          <option value="crit">Critique uniquement</option>
          <option value="warn">Warning + Critique</option>
        </select>
      </div>
      <div style="font-size:.65em;color:var(--dim);margin:6px 0;">
        Discord : créer un webhook via Server → Integrations → Webhooks<br>
        Telegram : créer un bot (@BotFather) et récupérer chat_id via @userinfobot
      </div>
      <div class="mrow" style="display:flex;gap:8px;">
        <button class="ctrl-btn" onclick="testWebhook()">🧪 Test webhook</button>
      </div>

      <div style="border-top:1px solid var(--border);margin:12px 0;"></div>
      <div class="modal-head" style="border:none;padding:0 0 8px 0;color:#00ff88;text-shadow:0 0 6px #00ff88;">✚ AUTO-HEAL — Règles</div>
      <div style="font-size:.65em;color:var(--dim);margin-bottom:8px;line-height:1.5;">
        Coche les règles à appliquer quand le bouton <b style="color:#00ff88">✚ HEAL</b> du dashboard est ON.<br>
        Chaque règle attend <b>15s</b> de condition soutenue avant de se déclencher, puis a un cooldown propre pour éviter le spam.
      </div>
      <div id="s-heal-rules" style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px;">
        <!-- rempli dynamiquement par loadSettings() -->
      </div>
      <div class="mrow" style="display:flex;gap:8px;">
        <button class="ctrl-btn" onclick="saveHealRules()" style="border-color:#00ff88;color:#00ff88;">✓ Enregistrer règles HEAL</button>
      </div>

      <div style="border-top:1px solid var(--border);margin:12px 0;"></div>
      <div class="modal-head" style="border:none;padding:0 0 8px 0;">🔔 Notifications Windows</div>
      <div class="mrow" style="display:flex;gap:8px;align-items:center;">
        <label style="flex:1;">Toast Windows natif sur alerte critique</label>
        <input type="checkbox" id="s-toast-enabled" style="width:20px;height:20px;accent-color:var(--a1);cursor:pointer;">
      </div>
      <div class="mrow" style="display:flex;gap:8px;">
        <button class="ctrl-btn" onclick="testToast()">🧪 Test toast</button>
      </div>

      <div style="border-top:1px solid var(--border);margin:12px 0;"></div>
      <div class="modal-head" style="border:none;padding:0 0 8px 0;">📱 Accès réseau + PIN</div>

      <div class="mrow" style="display:flex;gap:8px;align-items:center;">
        <label style="flex:1;">Accessible sur le réseau local (WiFi)</label>
        <input type="checkbox" id="s-lan-access" style="width:20px;height:20px;accent-color:var(--a1);cursor:pointer;"
               onchange="toggleLanAccess(this.checked)">
      </div>
      <div id="s-lan-info" style="font-size:.65em;color:var(--dim);margin-bottom:8px;"></div>

      <div class="mrow">
        <label>PIN de sécurité (4-8 chiffres, protège RESTART/SHUTDOWN/etc.)</label>
        <div id="s-pin-status" style="font-size:.65em;color:var(--dim);margin:4px 0;">--</div>
        <div style="display:flex;gap:6px;">
          <input id="s-pin-old" type="password" placeholder="Ancien PIN (si existant)"
                 style="flex:1;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
          <input id="s-pin-new" type="password" placeholder="Nouveau PIN (vide = désactiver)"
                 style="flex:1;background:#000;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:3px;font-family:inherit;font-size:.75em;">
          <button class="ctrl-btn" onclick="changePin()">✓</button>
        </div>
      </div>

      <div style="border-top:1px solid var(--border);margin:12px 0;"></div>
      <div class="mrow" style="display:flex;gap:8px;">
        <button class="ctrl-btn preset preset-gaming" onclick="saveSettings()">✓ Enregistrer webhook + toast</button>
        <button class="ctrl-btn" onclick="closeSettings()" style="margin-left:auto;">Fermer</button>
      </div>

      <!-- SECTION À PROPOS + RECAP FEATURES -->
      <div style="border-top:1px solid var(--border);margin:16px 0 8px;"></div>
      <details style="margin-top:8px;">
        <summary style="cursor:pointer;font-size:.7em;letter-spacing:.15em;color:var(--a1);padding:6px 0;">▸ ℹ À PROPOS DE SINDRI v3</summary>
        <div style="font-size:.7em;line-height:1.6;color:var(--text);padding:8px 4px;">
          <div style="color:var(--a1);font-weight:bold;letter-spacing:.15em;margin-bottom:4px;">SINDRI — Energy • Thermal • Systems</div>
          <div style="color:var(--dim);font-size:.85em;margin-bottom:10px;">v3 · Dashboard mono-fichier Python · monitoring temps réel + contrôle hardware</div>

          <div style="color:var(--a2);font-weight:bold;letter-spacing:.1em;margin:8px 0 4px;">📊 Monitoring</div>
          <ul style="padding-left:20px;">
            <li>CPU, GPU (nvidia), RAM, cœurs individuels, températures LHM</li>
            <li>Disques (I/O, SMART, occupation), réseau (bandes + processus par connexions)</li>
            <li>Historique persistant JSONL (30 jours, sous-échantillonnage 500 pts)</li>
            <li>Détection thermal throttle CPU (badge ⚠ THROTTLE)</li>
            <li>Infos système : modèle CPU, VBIOS/driver GPU, BIOS + mobo, écrans</li>
          </ul>

          <div style="color:var(--a2);font-weight:bold;letter-spacing:.1em;margin:8px 0 4px;">⚡ Énergie</div>
          <ul style="padding-left:20px;">
            <li>Conso murale certifiée : CPU/GPU mesurés + estimations mobo/RAM/SSD/HDD/fans/écrans</li>
            <li>Écrans détectés auto (résolution + Hz) ou override manuel</li>
            <li>Coût électrique persistant : jour / mois / année (rollover auto)</li>
          </ul>

          <div style="color:var(--a2);font-weight:bold;letter-spacing:.1em;margin:8px 0 4px;">🔧 Contrôle</div>
          <ul style="padding-left:20px;">
            <li>Presets FORGE : GAMING / OFFICE / SILENCE (powercfg + GPU power limit)</li>
            <li>Slider GPU Power Limit direct (nvidia-smi -pl)</li>
            <li>Contrôle ventilateurs PWM par canal + courbes automatiques (temp→PWM)</li>
            <li>Reboot vers UEFI en 1 clic (shutdown /r /fw)</li>
          </ul>

          <div style="color:var(--a2);font-weight:bold;letter-spacing:.1em;margin:8px 0 4px;">✚ HEAL Forge</div>
          <ul style="padding-left:20px;">
            <li>Bouton toggle ON/OFF auto-heal : règles cochées se déclenchent seules</li>
            <li>Actions curatives par cible (CPU/RAM/DISK/NET/GPU), jamais de kill user</li>
            <li>Effet visuel : cadre carte clignote vert pendant le soin</li>
          </ul>

          <div style="color:var(--a2);font-weight:bold;letter-spacing:.1em;margin:8px 0 4px;">🚨 Alertes</div>
          <ul style="padding-left:20px;">
            <li>Bips audio sur transitions warn/crit (conservés par choix UX)</li>
            <li>Toast Windows natif + webhook Discord/Telegram (durée-based configurable)</li>
            <li>HUD flottant Tk always-on-top, draggable, position persistée</li>
            <li>Icône systray NEON qui bat au rythme du CPU</li>
          </ul>

          <div style="color:var(--a2);font-weight:bold;letter-spacing:.1em;margin:8px 0 4px;">🔒 Sécurité</div>
          <ul style="padding-left:20px;">
            <li>PIN 4-8 chiffres (SHA256) sur actions destructives</li>
            <li>Accès LAN optionnel (bind 0.0.0.0) pour contrôle depuis mobile</li>
            <li>Blacklist processus critiques (kill safety)</li>
          </ul>

          <div style="border-top:1px dashed var(--border);margin-top:10px;padding-top:8px;color:var(--dim);font-size:.8em;">
            Serveur HTTP localhost:7890 · anti double-lancement · autostart Windows (HKCU\\...\\Run)<br>
            LibreHardwareMonitorLib.dll via pythonnet (capteurs + fan control)
          </div>
        </div>
      </details>
    </div>
  </div>
</div>

<!-- HUD OVERLAY -->
<div id="hud">
  <div id="hud-close" onclick="toggleHUD()">✕</div>
  <div class="hud-title">// LIVE STATS</div>
  <div class="hud-row"><span class="hud-lbl">CPU</span><span class="hud-val c1" id="h-cpu">--%</span><span class="hud-val" id="h-cpu-t" style="font-size:.85em">--°C</span></div>
  <div class="hud-row"><span class="hud-lbl">GPU</span><span class="hud-val c3" id="h-gpu">--%</span><span class="hud-val" id="h-gpu-t" style="font-size:.85em">--°C</span></div>
  <div class="hud-row"><span class="hud-lbl">RAM</span><span class="hud-val c2" id="h-ram">--%</span></div>
  <div class="hud-row"><span class="hud-lbl">POWER</span><span class="hud-val c3" id="h-pwr">-- W</span></div>
  <div class="hud-row"><span class="hud-lbl">PING</span><span class="hud-val c1" id="h-ping">-- ms</span></div>
</div>

<!-- MAIN GAUGES -->
<div class="grid-main">

  <!-- CPU -->
  <div class="card c1" id="cpu-card">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      <span>// CPU</span>
      <span style="display:flex;align-items:center;gap:4px;">
        <span id="cpu-throttle-badge" style="display:none;background:var(--a3);color:#000;font-size:.55em;letter-spacing:.15em;padding:2px 6px;border-radius:3px;font-weight:bold;box-shadow:0 0 6px var(--a3g);animation:pulse 1s infinite;" title="Fréquence CPU sous-cadencée sous charge — thermal ou power throttle">⚠ THROTTLE</span>
        <span class="heal-btn" onclick="healOne('cpu')" title="Soigner CPU : Balanced power plan, arrête WSearch, restart explorer si obèse">✚</span>
      </span>
    </div>
    <div class="gauge-wrap">
      <div class="gauge-outer">
        <svg class="gauge-svg" viewBox="0 0 120 120">
          <circle class="g-track" cx="60" cy="60" r="50"/>
          <circle class="g-glow" id="cpu-glow" cx="60" cy="60" r="50" stroke="var(--a1)"/>
          <circle class="g-fill" id="cpu-arc"  cx="60" cy="60" r="50" stroke="var(--a1)"/>
        </svg>
        <div class="gauge-center">
          <div class="gauge-val c1" id="cpu-pct">0%</div>
          <div class="gauge-unit">USAGE</div>
          <div class="gauge-sub" id="cpu-temp-g">--°C</div>
        </div>
      </div>
    </div>
    <div class="srow"><span class="slabel">MODEL</span><span class="sval" id="cpu-name" style="color:var(--a1);font-size:.6em;">--</span></div>
    <div class="srow"><span class="slabel">FRÉQ</span><span class="sval c1" id="cpu-freq">--</span></div>
    <div class="srow"><span class="slabel">CŒURS</span><span class="sval" id="cpu-cores">--</span></div>
    <div class="srow"><span class="slabel">TEMP</span><span class="sval" id="cpu-temp">--</span></div>
    <div class="srow"><span class="slabel">PUISSANCE</span><span class="sval" id="cpu-power">--</span></div>
    <div class="srow" style="margin-top:4px;padding-top:4px;border-top:1px dashed var(--border);">
      <span class="slabel">MOBO</span><span class="sval" id="sys-mobo" style="font-size:.55em;color:var(--dim);">--</span>
    </div>
    <div class="srow"><span class="slabel">BIOS</span><span class="sval" id="sys-bios" style="font-size:.55em;color:var(--dim);">--</span></div>
    <div class="srow"><span class="slabel">ÉCRANS</span><span class="sval" id="sys-displays" style="font-size:.55em;color:var(--dim);">--</span></div>
  </div>

  <!-- RAM -->
  <div class="card c2" id="ram-card">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      <span>// MÉMOIRE</span>
      <span class="heal-btn" onclick="healOne('ram')" title="Soigner RAM : trim working set de tous les processus (Windows repagine à la demande)">✚</span>
    </div>
    <div class="gauge-wrap">
      <div class="gauge-outer">
        <svg class="gauge-svg" viewBox="0 0 120 120">
          <circle class="g-track" cx="60" cy="60" r="50"/>
          <circle class="g-glow" id="ram-glow" cx="60" cy="60" r="50" stroke="var(--a2)"/>
          <circle class="g-fill" id="ram-arc"  cx="60" cy="60" r="50" stroke="var(--a2)"/>
        </svg>
        <div class="gauge-center">
          <div class="gauge-val c2" id="ram-pct">0%</div>
          <div class="gauge-unit">USED</div>
          <div class="gauge-sub c2" id="ram-used-g">--</div>
        </div>
      </div>
    </div>
    <div class="srow"><span class="slabel">UTILISÉ</span><span class="sval c2" id="ram-used">--</span></div>
    <div class="srow"><span class="slabel">TOTAL</span><span class="sval" id="ram-total">--</span></div>
    <div class="srow"><span class="slabel">LIBRE</span><span class="sval" id="ram-free">--</span></div>
    <div class="srow"><span class="slabel">SWAP</span><span class="sval" id="ram-swap">--</span></div>
  </div>

  <!-- GPU -->
  <div class="card c3" id="gpu-card">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      <span>// GPU</span>
      <span class="heal-btn" onclick="healOne('gpu')" title="Soigner GPU : power limit → défaut, reset clocks">✚</span>
    </div>
    <div id="gpu-content">
      <div class="cd" style="font-size:.72em;text-align:center;padding:18px 0">
        Aucun GPU NVIDIA détecté<br><span style="font-size:.85em">(nvidia-smi requis)</span>
      </div>
    </div>
  </div>

  <!-- STORAGE -->
  <div class="card c1" id="storage-card">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      <span>// STOCKAGE</span>
      <span style="display:flex;align-items:center;gap:4px;">
        <span onclick="openSmartModal()" title="Rapport SMART (santé disques)"
              style="cursor:pointer;color:var(--dim);font-size:.85em;padding:0 4px;transition:color .2s,text-shadow .2s;"
              onmouseover="this.style.color='var(--a1)';this.style.textShadow='0 0 6px var(--a1)';"
              onmouseout="this.style.color='var(--dim)';this.style.textShadow='none';">🩺</span>
        <span class="heal-btn" onclick="healOne('disk')" title="Soigner Disque">✚</span>
      </span>
    </div>
    <div id="disk-content"></div>
  </div>

</div>

<!-- MID ROW: cores / disk IO / network -->
<div class="grid-mid">
  <div class="card c1" id="cores-card">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      <span>// CŒURS CPU</span>
      <span class="heal-btn" onclick="healOne('cpu')" title="Soigner CPU">✚</span>
    </div>
    <div class="core-grid" id="core-grid"></div>
    <div class="spark-wrap">
      <svg id="cpu-spark" class="spark" viewBox="0 0 300 40" preserveAspectRatio="none"></svg>
      <div class="spark-info" id="cpu-spark-info"></div>
      <div class="spark-tip"  id="cpu-spark-tip"></div>
    </div>
  </div>

  <div class="card c2" id="diskio-card">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      <span>// DISQUE I/O</span>
      <span class="heal-btn" onclick="healOne('disk')" title="Soigner Disque : vide %TEMP%, Corbeille, TRIM SSD">✚</span>
    </div>
    <div class="srow"><span class="slabel">LECT</span><span class="sval c1" id="disk-r">--</span></div>
    <div style="height:3px;background:#141428;border-radius:2px;margin:4px 0 8px;">
      <div id="disk-r-bar" style="height:100%;background:var(--a1);border-radius:2px;width:0%;transition:width .5s;"></div>
    </div>
    <div class="srow"><span class="slabel">ÉCRIT</span><span class="sval c2" id="disk-w">--</span></div>
    <div style="height:3px;background:#141428;border-radius:2px;margin:4px 0;">
      <div id="disk-w-bar" style="height:100%;background:var(--a2);border-radius:2px;width:0%;transition:width .5s;"></div>
    </div>
    <div class="spark-wrap">
      <svg id="io-spark" class="spark" viewBox="0 0 300 40" preserveAspectRatio="none"></svg>
      <div class="spark-info" id="io-spark-info"></div>
      <div class="spark-tip"  id="io-spark-tip"></div>
    </div>
  </div>

  <div class="card" id="net-card" style="border-top-color:var(--co,#ff8c00)">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      <span>// RÉSEAU + PING</span>
      <span class="heal-btn" onclick="healOne('net')" title="Soigner Réseau : flush DNS, cache ARP, re-register DNS">✚</span>
    </div>
    <div class="srow"><span class="slabel">↓ DOWN</span><span class="sval co" id="net-r">--</span></div>
    <div style="height:3px;background:#141428;border-radius:2px;margin:4px 0 8px;">
      <div id="net-r-bar" style="height:100%;background:#ff8c00;border-radius:2px;width:0%;transition:width .5s;"></div>
    </div>
    <div class="srow"><span class="slabel">↑ UP</span><span class="sval" id="net-s">--</span></div>
    <div style="height:3px;background:#141428;border-radius:2px;margin:4px 0 8px;">
      <div id="net-s-bar" style="height:100%;background:var(--a1);border-radius:2px;width:0%;transition:width .5s;"></div>
    </div>
    <div class="srow" style="margin-top:6px;"><span class="slabel">PING 8.8.8.8</span><span class="sval c1" id="ping-val">--</span></div>
    <div class="spark-wrap">
      <svg id="net-spark" class="spark" viewBox="0 0 300 40" preserveAspectRatio="none"></svg>
      <div class="spark-info" id="net-spark-info"></div>
      <div class="spark-tip"  id="net-spark-tip"></div>
    </div>
  </div>
</div>

<!-- BOTTOM ROW: temps / fans / power -->
<div class="grid-bottom">
  <!-- TEMPERATURES -->
  <div class="card c3" id="temp-card">
    <div class="card-title">// TEMPÉRATURES</div>
    <div class="temp-list" id="temp-list">
      <div class="cd" style="font-size:.68em;padding:6px 0;color:var(--dim);">
        Chargement des capteurs...
      </div>
    </div>
  </div>

  <!-- FANS -->
  <div class="card c2" id="fan-card">
    <div class="card-title">// VENTILATEURS</div>
    <div id="fan-list">
      <div style="font-size:.68em;" id="fan-content">
        <div class="srow"><span class="slabel">GPU FAN</span><span class="sval c1" id="fan-gpu">--</span></div>
        <div class="cd" style="font-size:.75em;padding:8px 0;margin-top:4px;">
          Chargement RPM...
        </div>
      </div>
    </div>
  </div>

  <!-- POWER + ENERGIE -->
  <div class="card c3" id="power-card">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      <span>// CONSOMMATION MURALE</span>
      <span style="display:flex;align-items:center;gap:4px;">
        <span class="heal-btn" onclick="healOne('gpu')" title="Soigner GPU (power limit)">✚</span>
        <span onclick="openPowerConfig()" style="cursor:pointer;color:var(--a1);letter-spacing:0;font-size:.9em;" title="Configurer le PC">⚙</span>
      </span>
    </div>

    <!-- TOTAL AC (le vrai chiffre) au top -->
    <div style="text-align:center;border:2px solid var(--a3);border-radius:6px;padding:10px 4px;margin-bottom:8px;box-shadow:0 0 20px var(--a3g) inset;">
      <div class="cd" style="font-size:.5em;letter-spacing:.2em;">TOTAL À LA PRISE</div>
      <div class="big-num c3" id="pw-ac-total" style="font-size:2em;">-- W</div>
      <div class="cd" style="font-size:.55em;">PSU <span id="pw-eff">--</span>% · pertes <span id="pw-loss">--</span> W</div>
    </div>

    <!-- Breakdown collapse -->
    <details style="margin-bottom:8px;">
      <summary style="cursor:pointer;font-size:.6em;color:var(--dim);letter-spacing:.15em;padding:4px 0;">▸ DÉTAIL PAR COMPOSANT</summary>
      <div id="pw-breakdown" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;font-size:.65em;margin-top:6px;padding:8px;background:rgba(0,0,0,.35);border-radius:4px;">
        <span class="slabel">🔥 CPU</span>          <span class="sval c1" id="pw-b-cpu">-- W</span>
        <span class="slabel">🎮 GPU</span>          <span class="sval c3" id="pw-b-gpu">-- W</span>
        <span class="slabel">⚡ Carte mère</span>  <span class="sval" id="pw-b-mobo">-- W</span>
        <span class="slabel">🧠 RAM</span>          <span class="sval" id="pw-b-ram">-- W</span>
        <span class="slabel">💾 SSD</span>          <span class="sval" id="pw-b-ssd">-- W</span>
        <span class="slabel">💿 HDD</span>          <span class="sval" id="pw-b-hdd">-- W</span>
        <span class="slabel">🌀 Ventilos</span>     <span class="sval" id="pw-b-fans">-- W</span>
        <span class="slabel">🔌 USB/RGB</span>      <span class="sval" id="pw-b-extra">-- W</span>
        <span class="slabel">🖥 Écrans</span>       <span class="sval c1" id="pw-b-screens">-- W</span>
        <span class="slabel" style="border-top:1px solid var(--border);padding-top:3px;margin-top:3px;">DC total</span>
        <span class="sval c2"  style="border-top:1px solid var(--border);padding-top:3px;margin-top:3px;" id="pw-b-dc">-- W</span>
      </div>
    </details>

    <div style="margin-bottom:8px;">
      <div class="bar-head"><span>GPU LOAD</span><span id="pw-pct">--%</span></div>
      <div class="bar-track"><div class="bar-fill" id="pw-bar" style="background:var(--a3);"></div></div>
    </div>

    <!-- Séparateur énergie -->
    <div style="border-top:1px solid var(--border);margin:8px 0;"></div>

    <!-- Compteur session -->
    <div class="srow"><span class="slabel">DURÉE SESSION</span><span class="sval c1" id="pw-session">--</span></div>
    <div class="srow"><span class="slabel">ÉNERGIE TOTALE</span><span class="sval c3" id="pw-wh">-- Wh</span></div>
    <div class="srow"><span class="slabel">EN kWh</span><span class="sval c3" id="pw-kwh">-- kWh</span></div>

    <!-- Prix kWh + cumuls persistants -->
    <div style="margin-top:8px;padding:6px 8px;border:1px solid var(--border);border-radius:4px;background:rgba(0,0,0,.3);">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
        <span style="font-size:.6em;color:var(--dim);letter-spacing:.1em;flex:1;">PRIX kWh</span>
        <input id="kwh-price" type="number" value="0.0937" step="0.001" min="0.001" max="2"
          style="width:72px;background:#000;border:1px solid var(--a2);color:var(--a2);font-family:inherit;font-size:.75em;padding:2px 4px;border-radius:2px;text-align:right;"
          oninput="updateCost()" onchange="saveKwhPrice()">
        <span style="font-size:.6em;color:var(--dim);" id="kwh-currency-lbl">€/kWh</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <span style="font-size:.62em;color:var(--dim);">COÛT SESSION</span>
        <span class="sval c3" id="pw-cost" style="font-size:1.1em;font-weight:bold;">-- ¢</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:3px;">
        <span style="font-size:.58em;color:var(--dim);">COÛT/HEURE</span>
        <span class="sval" style="color:var(--a2);font-size:.85em;" id="pw-cost-h">-- /h</span>
      </div>
      <!-- Cumuls persistants -->
      <div style="border-top:1px dashed var(--border);margin-top:8px;padding-top:6px;display:grid;grid-template-columns:auto 1fr auto;gap:2px 8px;font-size:.62em;">
        <span style="color:var(--dim);letter-spacing:.1em;">📅 AUJOURD'HUI</span>
        <span style="color:var(--dim);text-align:right;" id="pw-daily-kwh">-- kWh</span>
        <span class="sval c3" id="pw-daily-cost" style="font-weight:bold;min-width:52px;text-align:right;">--</span>
        <span style="color:var(--dim);letter-spacing:.1em;">🗓 CE MOIS</span>
        <span style="color:var(--dim);text-align:right;" id="pw-monthly-kwh">-- kWh</span>
        <span class="sval c3" id="pw-monthly-cost" style="font-weight:bold;text-align:right;">--</span>
        <span style="color:var(--dim);letter-spacing:.1em;">📆 CETTE ANNÉE</span>
        <span style="color:var(--dim);text-align:right;" id="pw-yearly-kwh">-- kWh</span>
        <span class="sval c3" id="pw-yearly-cost" style="font-weight:bold;text-align:right;">--</span>
      </div>
    </div>
  </div>
</div>

<!-- PROCESSES PANEL -->
<div style="margin:0 12px 10px;border:1px solid var(--border);border-radius:6px;overflow:hidden;">
  <div onclick="toggleProc()" style="display:flex;justify-content:space-between;align-items:center;padding:8px 14px;background:var(--bg2);cursor:pointer;border-bottom:1px solid var(--border);">
    <span style="font-size:.62em;letter-spacing:.22em;color:var(--dim);">// TOP PROCESSUS</span>
    <span id="proc-toggle" style="font-size:.6em;color:var(--a2);">▼ AFFICHER</span>
  </div>
  <div id="proc-body" style="display:none;">
    <div style="display:flex;gap:6px;padding:6px 12px;border-bottom:1px solid var(--border);background:var(--bg2);align-items:center;">
      <button class="tbtn active" id="sort-cpu-btn" onclick="sortProcs('cpu')">CPU</button>
      <button class="tbtn"        id="sort-ram-btn" onclick="sortProcs('ram')">RAM</button>
      <input type="text" id="proc-search" placeholder="🔍 rechercher..." oninput="filterProcs(this.value)"
             style="flex:1;background:#000;border:1px solid var(--border);color:var(--text);font-family:inherit;font-size:.65em;padding:3px 8px;border-radius:3px;margin-left:6px;">
      <span style="font-size:.55em;color:var(--dim);">SYS = process critique</span>
    </div>
    <div id="proc-list" style="max-height:260px;overflow-y:auto;"></div>
  </div>
</div>

<!-- FAN CURVE MODAL -->
<div id="fan-curve-modal" class="modal">
  <div class="modal-box" style="max-width:500px;">
    <div class="modal-head">📈 Courbe fan — <span id="fc-name" style="color:var(--a1);">--</span></div>
    <div class="modal-body">
      <div style="font-size:.7em;color:var(--dim);margin-bottom:10px;line-height:1.5;">
        Interpolation linéaire entre points. Sous le premier point → PWM du premier, au-dessus du dernier → PWM du dernier. Trie automatique par température.
      </div>
      <div class="mrow" style="display:flex;gap:8px;align-items:center;">
        <label style="flex:1;">Source de température</label>
        <select id="fc-source" style="width:120px;">
          <option value="cpu">CPU</option>
          <option value="gpu">GPU</option>
          <option value="hottest">Le plus chaud</option>
        </select>
      </div>
      <div class="mrow" style="display:flex;gap:8px;align-items:center;">
        <label style="flex:1;">Courbe active</label>
        <input type="checkbox" id="fc-enabled" style="width:20px;height:20px;accent-color:#00ff88;cursor:pointer;">
      </div>
      <div style="border-top:1px solid var(--border);margin:10px 0;"></div>
      <div style="font-size:.6em;color:var(--dim);letter-spacing:.15em;margin-bottom:6px;">POINTS (temp °C → PWM %)</div>
      <div id="fc-points" style="display:flex;flex-direction:column;gap:4px;"></div>
      <div style="display:flex;gap:6px;margin-top:8px;">
        <button class="ctrl-btn" onclick="fcAddPoint()" style="font-size:.7em;">+ Ajouter point</button>
        <button class="ctrl-btn" onclick="fcLoadPreset('silent')" style="font-size:.7em;border-color:var(--a1);color:var(--a1);">🤫 SILENT</button>
        <button class="ctrl-btn" onclick="fcLoadPreset('balanced')" style="font-size:.7em;border-color:var(--a2);color:var(--a2);">⚖ BALANCED</button>
        <button class="ctrl-btn" onclick="fcLoadPreset('perf')" style="font-size:.7em;border-color:var(--a3);color:var(--a3);">🚀 PERF</button>
      </div>
      <!-- Visualisation SVG de la courbe -->
      <svg id="fc-preview" viewBox="0 0 300 100" preserveAspectRatio="none"
           style="width:100%;height:100px;margin-top:12px;background:rgba(0,0,0,.4);border:1px solid var(--border);border-radius:3px;"></svg>
      <div style="border-top:1px solid var(--border);margin:12px 0;"></div>
      <div class="mrow" style="display:flex;gap:8px;">
        <button class="ctrl-btn preset preset-gaming" onclick="fcSave()">✓ Enregistrer</button>
        <button class="ctrl-btn danger" onclick="fcDelete()">🗑 Supprimer</button>
        <button class="ctrl-btn" onclick="closeFanCurve()" style="margin-left:auto;">Fermer</button>
      </div>
    </div>
  </div>
</div>

<!-- FAN CONTROL PANEL -->
<div style="margin:0 12px 10px;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:var(--card);">
  <div onclick="toggleFanCtrl()" style="display:flex;justify-content:space-between;align-items:center;padding:8px 14px;background:var(--bg2);cursor:pointer;border-bottom:1px solid var(--border);">
    <span style="font-size:.62em;letter-spacing:.22em;color:var(--dim);">// VENTILATION — Contrôle PWM direct</span>
    <div style="display:flex;gap:8px;align-items:center;">
      <span id="fan-info" style="font-size:.55em;color:var(--dim);">--</span>
      <button class="ctrl-btn" onclick="event.stopPropagation(); rescanFans();" style="font-size:.6em;" title="Rescan des canaux PWM">🔄</button>
      <button class="ctrl-btn" onclick="event.stopPropagation(); resetAllFans();" style="font-size:.6em;border-color:var(--a2);color:var(--a2);" title="Rendre tous les ventilos au BIOS">↺ BIOS</button>
      <span id="fan-toggle" style="font-size:.6em;color:var(--a1);">▼ AFFICHER</span>
    </div>
  </div>
  <div id="fan-body" style="display:none;padding:12px 14px;">
    <div id="fan-list" style="display:flex;flex-direction:column;gap:6px;"></div>
    <div style="font-size:.6em;color:var(--dim);margin-top:10px;border-top:1px dashed var(--border);padding-top:8px;line-height:1.5;">
      ⚠ Fonctionne uniquement si le chipset Super I/O est supporté par LibreHardwareMonitor (Nuvoton, ITE, Fintek…). Certaines cartes mères verrouillent le contrôle software par le BIOS. En cas de blocage, aller dans le BIOS et régler les fans sur "Full Speed" ou "PWM Manual".
    </div>
  </div>
</div>

<!-- HISTORIQUE PERSISTANT -->
<div style="margin:0 12px 10px;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:var(--card);">
  <div onclick="toggleHistory()" style="display:flex;justify-content:space-between;align-items:center;padding:8px 14px;background:var(--bg2);cursor:pointer;border-bottom:1px solid var(--border);">
    <span style="font-size:.62em;letter-spacing:.22em;color:var(--dim);">// HISTORIQUE PERSISTANT</span>
    <div style="display:flex;gap:6px;align-items:center;">
      <button class="ctrl-btn hist-r" data-r="60"    onclick="loadHistory(60,event)">1h</button>
      <button class="ctrl-btn hist-r" data-r="360"   onclick="loadHistory(360,event)">6h</button>
      <button class="ctrl-btn hist-r" data-r="1440"  onclick="loadHistory(1440,event)" style="border-color:var(--a1);color:var(--a1);">24h</button>
      <button class="ctrl-btn hist-r" data-r="10080" onclick="loadHistory(10080,event)">7j</button>
      <button class="ctrl-btn hist-r" data-r="43200" onclick="loadHistory(43200,event)">30j</button>
      <span id="hist-info" style="font-size:.55em;color:var(--dim);margin-left:8px;">--</span>
      <span id="hist-toggle" style="font-size:.6em;color:var(--a1);">▼ AFFICHER</span>
    </div>
  </div>
  <div id="hist-body" style="display:none;padding:12px 14px;">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
      <div>
        <div style="font-size:.55em;color:var(--dim);letter-spacing:.15em;margin-bottom:4px;">CPU LOAD %  &  CPU TEMP °C</div>
        <div class="spark-wrap">
          <svg id="hist-cpu" viewBox="0 0 400 94" preserveAspectRatio="none" style="width:100%;height:94px;cursor:crosshair;"></svg>
          <div class="spark-info" id="hist-cpu-info"></div>
          <div class="spark-tip" id="hist-cpu-tip"></div>
        </div>
      </div>
      <div>
        <div style="font-size:.55em;color:var(--dim);letter-spacing:.15em;margin-bottom:4px;">GPU LOAD %  &  GPU TEMP °C</div>
        <div class="spark-wrap">
          <svg id="hist-gpu" viewBox="0 0 400 94" preserveAspectRatio="none" style="width:100%;height:94px;cursor:crosshair;"></svg>
          <div class="spark-info" id="hist-gpu-info"></div>
          <div class="spark-tip" id="hist-gpu-tip"></div>
        </div>
      </div>
      <div>
        <div style="font-size:.55em;color:var(--dim);letter-spacing:.15em;margin-bottom:4px;">RAM %  &  CONSO TOTALE (W)</div>
        <div class="spark-wrap">
          <svg id="hist-power" viewBox="0 0 400 94" preserveAspectRatio="none" style="width:100%;height:94px;cursor:crosshair;"></svg>
          <div class="spark-info" id="hist-power-info"></div>
          <div class="spark-tip" id="hist-power-tip"></div>
        </div>
      </div>
      <div>
        <div style="font-size:.55em;color:var(--dim);letter-spacing:.15em;margin-bottom:4px;">RÉSEAU (↓ + ↑)  &  DISQUE (R+W)</div>
        <div class="spark-wrap">
          <svg id="hist-io" viewBox="0 0 400 94" preserveAspectRatio="none" style="width:100%;height:94px;cursor:crosshair;"></svg>
          <div class="spark-info" id="hist-io-info"></div>
          <div class="spark-tip" id="hist-io-tip"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- NETWORK PROCESSES PANEL -->
<div style="margin:0 12px 10px;border:1px solid var(--border);border-radius:6px;overflow:hidden;">
  <div onclick="toggleNetProc()" style="display:flex;justify-content:space-between;align-items:center;padding:8px 14px;background:var(--bg2);cursor:pointer;border-bottom:1px solid var(--border);">
    <span style="font-size:.62em;letter-spacing:.22em;color:var(--dim);">// RÉSEAU PAR PROCESSUS</span>
    <div style="display:flex;gap:10px;align-items:center;">
      <span style="font-size:.6em;color:var(--dim);">Total: <span id="net-proc-total" style="color:var(--a1);">--</span> connexions</span>
      <span id="net-proc-toggle" style="font-size:.6em;color:var(--a1);">▼ AFFICHER</span>
    </div>
  </div>
  <div id="net-proc-body" style="display:none;">
    <div id="net-proc-list" style="max-height:200px;overflow-y:auto;"></div>
  </div>
</div>

<!-- EVENTS PANEL -->
<div style="margin:0 12px 10px;border:1px solid var(--border);border-radius:6px;overflow:hidden;">
  <div style="display:flex;justify-content:space-between;padding:8px 14px;background:var(--bg2);border-bottom:1px solid var(--border);">
    <span style="font-size:.62em;letter-spacing:.22em;color:var(--dim);">// ERREURS SYSTÈME (24h)</span>
    <span id="events-count" style="font-size:.6em;color:var(--a3);">--</span>
  </div>
  <div id="events-list" style="max-height:130px;overflow-y:auto;padding:0 14px;"></div>
</div>

<!-- KILL CONFIRM DIALOG -->
<div id="kill-dialog" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;display:none;align-items:center;justify-content:center;">
  <div style="background:var(--card);border:1px solid var(--a3);border-radius:8px;padding:24px;min-width:300px;text-align:center;box-shadow:0 0 30px var(--a3g);">
    <div style="font-size:.7em;letter-spacing:.2em;color:var(--a3);margin-bottom:12px;">// TERMINER PROCESSUS</div>
    <div id="kill-name" style="font-size:1.1em;font-weight:bold;margin-bottom:6px;"></div>
    <div id="kill-info" style="font-size:.75em;color:var(--dim);margin-bottom:12px;"></div>
    <div id="kill-warn">
      ⚠ PROCESS SYSTÈME CRITIQUE<br>
      Tuer ce process peut crasher Windows ou provoquer un BSOD.<br>
      Fais-le seulement si tu sais exactement ce que tu fais.
    </div>
    <div style="display:flex;gap:10px;justify-content:center;">
      <button class="tbtn danger" id="kill-confirm-btn" onclick="confirmKill()" style="padding:6px 20px;">CONFIRMER</button>
      <button class="tbtn" onclick="closeKill()" style="padding:6px 20px;">ANNULER</button>
    </div>
  </div>
</div>

<!-- POWER FOOTER -->
<div class="power-footer">
  <span class="pf-label pulse">⚡</span>
  <span class="pf-val">GPU: <b id="footer-gpu-pwr">-- W</b></span>
  <span class="pf-val">CPU: <b id="footer-cpu-pwr">-- W</b></span>
  <span class="pf-val">CPU: <b id="footer-cpu-t">--°C</b></span>
  <span class="pf-val">GPU: <b id="footer-gpu-t">--°C</b></span>
  <span class="pf-ping">Ping: <b id="footer-ping">-- ms</b></span>
  <span class="pf-val">Total~: <b id="footer-total">-- W</b></span>
  <span style="color:var(--a2);font-size:.8em;">kWh session: <b id="footer-kwh" style="color:var(--a2)">--</b></span>
  <span class="pf-val">W×h: <b id="footer-wh" style="color:var(--a2)">--</b></span>
  <button class="tbtn" id="autostart-btn" onclick="toggleAutostart()" style="margin-left:auto;font-size:.6em;">AUTOSTART: --</button>
  <span id="footer-ts" class="cd" style="font-size:.8em;">--</span>
</div>

<script>
// ── Config ────────────────────────────────────────────────────────────────────
const ARC_LEN  = 235.6;  // 270° arc for r=50
const MAX_H    = 150;    // 5 min at 2s interval
let soundEnabled = true;
let hudVisible   = false;
let frameCount   = 0;
let _lastWh      = 0;
let _sessionStart= null;

// Full history for CSV
const histFull = [];

// Sparkline history
const H = { cpu:[], gpu:[], io:[], net:[] };
function addH(arr, v) { arr.push(v); if (arr.length > MAX_H) arr.shift(); }

// ── Audio ─────────────────────────────────────────────────────────────────────
let audioCtx = null;
function beep(freq=880, dur=150, vol=0.12) {
  if (!soundEnabled) return;
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext||window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.connect(gain); gain.connect(audioCtx.destination);
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(vol, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + dur/1000);
    osc.start(); osc.stop(audioCtx.currentTime + dur/1000);
  } catch(e) {}
}
function toggleSound() {
  soundEnabled = !soundEnabled;
  document.getElementById('sound-btn').textContent = soundEnabled ? '🔔' : '🔕';
}

// ── Theme ─────────────────────────────────────────────────────────────────────
function setTheme(t) {
  document.body.setAttribute('data-theme', t);
  localStorage.setItem('sindri-theme', t);
  const sel = document.getElementById('s-theme');
  if (sel) sel.value = t;
}
// Restore saved theme (supporte l'ancienne clé pulse-theme et l'ancien 'theme')
const savedTheme = localStorage.getItem('sindri-theme')
                || localStorage.getItem('pulse-theme')
                || localStorage.getItem('theme')
                || 'neon';
setTheme(savedTheme);

// ── Alert system par catégorie ────────────────────────────────────────────────
// Seuils : w = warning (orange), c = critique (rouge pulsant)
const THRESH = {
  cpu_temp:{w:75,c:90},  gpu_temp:{w:80,c:90},
  cpu_pct: {w:85,c:95},  gpu_pct: {w:90,c:98},
  ram_pct: {w:88,c:95},  disk_pct:{w:88,c:95},
  temp_any:{w:75,c:90},  ping:    {w:120,c:300},
  power_w: {w:400,c:550},
};
// Etat par métrique pour éviter de biper en boucle
const alertSt = {};

function level(val, key) {
  if (val == null) return null;
  const t = THRESH[key];
  if (!t) return null;
  if (val >= t.c) return 'crit';
  if (val >= t.w) return 'warn';
  return null;
}
// Prend le pire niveau de plusieurs valeurs (null < warn < crit)
function worst(...lvls) {
  if (lvls.includes('crit')) return 'crit';
  if (lvls.includes('warn')) return 'warn';
  return null;
}
// Applique classe + badge sur une carte, joue un son sur changement d'état
function applyCard(id, lvl, label) {
  const card = document.getElementById(id);
  if (!card) return;
  card.classList.remove('warn','crit');
  // badge
  let badge = card.querySelector('.card-alert-badge');
  if (lvl) {
    card.classList.add(lvl);
    if (!badge) {
      badge = document.createElement('div');
      badge.className = 'card-alert-badge';
      card.appendChild(badge);
    }
    badge.textContent = lvl === 'crit' ? '⚠ ALERTE' : '!';
  } else if (badge) {
    badge.remove();
  }
  // bip sur transition
  const prev = alertSt[id];
  if (lvl && lvl !== prev) {
    if (lvl === 'crit') beep(1400, 220);
    else                beep(660, 100);
  }
  alertSt[id] = lvl;
}

function checkAlerts(d) {
  // ── CPU card : temp OU charge
  const cpuTempL = level(d.cpu?.temp, 'cpu_temp');
  const cpuPctL  = level(d.cpu?.pct,  'cpu_pct');
  applyCard('cpu-card', worst(cpuTempL, cpuPctL));

  // ── Mémoire card
  applyCard('ram-card', level(d.mem?.pct, 'ram_pct'));

  // ── GPU card
  const gpuTempL = level(d.gpus?.[0]?.temp, 'gpu_temp');
  const gpuPctL  = level(d.gpus?.[0]?.util, 'gpu_pct');
  applyCard('gpu-card', worst(gpuTempL, gpuPctL));

  // ── Stockage card : pire disque
  let diskL = null;
  (d.disks || []).forEach(dk => {
    const l = level(dk.pct, 'disk_pct');
    diskL = worst(diskL, l);
  });
  applyCard('storage-card', diskL);

  // ── Cœurs card : pire cœur
  let coreL = null;
  (d.cpu?.cores || []).forEach(c => {
    coreL = worst(coreL, level(c, 'cpu_pct'));
  });
  applyCard('cores-card', coreL);

  // ── Réseau card : ping
  applyCard('net-card', level(d.ping_ms, 'ping'));

  // ── Températures card : pire sonde
  let tempL = null;
  Object.values(d.temps || {}).forEach(v => {
    tempL = worst(tempL, level(v, 'temp_any'));
  });
  applyCard('temp-card', tempL);

  // ── Ventilateurs card : fan à 0 tandis qu'une température est critique = crit
  let fanL = null;
  const fanVals = Object.values(d.fans || {});
  const anyTempCrit = Object.values(d.temps || {}).some(v => v >= THRESH.temp_any.c);
  if (fanVals.length && fanVals.every(v => v === 0) && anyTempCrit) fanL = 'crit';
  applyCard('fan-card', fanL);

  // ── Consommation card : total watts
  const gpuW = d.power?.gpu_w || 0;
  const cpuW = d.power?.cpu_w || 0;
  applyCard('power-card', level(gpuW + cpuW, 'power_w'));

  // ── Banner global (résumé) ──────────────────────────────────────────────
  const msgs = [];
  let topLevel = null;
  const push = (val, lvl, label) => {
    if (!lvl) return;
    msgs.push(`${lvl==='crit'?'⚠':'!'} ${label}: ${val}`);
    if (lvl === 'crit') topLevel = 'crit';
    else if (topLevel !== 'crit') topLevel = 'warn';
  };
  push(d.cpu?.temp + '°C', cpuTempL, 'CPU');
  push(d.cpu?.pct + '%',   cpuPctL,  'CPU load');
  push(d.gpus?.[0]?.temp + '°C', gpuTempL, 'GPU');
  push(d.mem?.pct + '%',   level(d.mem?.pct, 'ram_pct'), 'RAM');
  push(d.ping_ms + 'ms',   level(d.ping_ms, 'ping'), 'PING');
  const totW = gpuW + cpuW;
  push(totW.toFixed(0)+'W', level(totW,'power_w'), 'CONSO');

  const banner = document.getElementById('alert-banner');
  const dot    = document.getElementById('status-dot');
  if (msgs.length) {
    banner.className = 'alert-banner ' + topLevel;
    banner.textContent = msgs.join('  |  ');
    dot.className = 'dot ' + topLevel;
  } else {
    banner.className = 'alert-banner';
    dot.className = 'dot';
  }
}

// ── Formatting ────────────────────────────────────────────────────────────────
function fmtB(b, d=1) {
  if (!b || b===0) return '0 B';
  const k=1024, u=['B','KB','MB','GB','TB'];
  const i=Math.floor(Math.log(b)/Math.log(k));
  return (b/Math.pow(k,i)).toFixed(d)+' '+u[i];
}
function fmtSpd(bps) { return fmtB(bps)+'/s'; }

// ── Color by % ────────────────────────────────────────────────────────────────
function pctColor(p) {
  if (p < 65) return 'var(--a1)';
  if (p < 82) return 'var(--a2)';
  return 'var(--a3)';
}
function tempColor(t) {
  if (t==null) return 'var(--dim)';
  if (t < 60)  return 'var(--a1)';
  if (t < 80)  return 'var(--a2)';
  return 'var(--a3)';
}
function pingColor(ms) {
  if (ms==null) return 'var(--dim)';
  if (ms < 30)  return 'var(--a1)';
  if (ms < 80)  return 'var(--a2)';
  return 'var(--a3)';
}

// ── Gauge ─────────────────────────────────────────────────────────────────────
function setGauge(arcId, glowId, pct, color) {
  const off = ARC_LEN * (1 - Math.min(100,Math.max(0,pct))/100);
  const arc = document.getElementById(arcId);
  const glow= document.getElementById(glowId);
  if (!arc) return;
  arc.style.strokeDashoffset = off;
  arc.style.stroke = color;
  if (glow) { glow.style.strokeDashoffset=off; glow.style.stroke=color; }
}

// ── Sparkline ─────────────────────────────────────────────────────────────────
// Formatage : 'pct' pour %, 'bps' pour bytes/s, sinon fmt sert de suffixe unité (°C, W, ms…)
function _fmtSpark(v, fmt) {
  if (v == null) return '--';
  if (fmt === 'bps') return fmtSpd(v);
  if (fmt === 'pct') return v.toFixed(1) + '%';
  return v.toFixed(1) + (fmt || '');
}

function drawSpark(id, data, color, cap=100, fmt='pct') {
  const el=document.getElementById(id);
  if (!el||data.length<2) return;
  const W=300, H=40;
  const dataMax = Math.max(...data, 1);
  const M = cap>0 ? cap : dataMax;
  const xy = data.map((v,i)=>({x:(i/(MAX_H-1))*W, y:H-(Math.min(v,M)/M)*H, v:v}));
  const pts = xy.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  const fillPts = [...xy.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`),`${W},${H}`,`0,${H}`].join(' ');

  // Point actuel + point max
  const cur = xy[xy.length-1];
  const maxIdx = data.indexOf(dataMax);
  const maxPt  = xy[maxIdx];

  el.innerHTML = `
    <polygon points="${fillPts}" fill="${color}" opacity=".1"/>
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" opacity=".9" stroke-linejoin="round"/>
    <line x1="0" y1="${H}" x2="${W}" y2="${H}" stroke="${color}" stroke-width=".4" opacity=".2"/>
    <line class="spark-cursor" id="${id}-cursor" x1="0" y1="0" x2="0" y2="${H}"/>
    ${maxPt ? `<circle cx="${maxPt.x.toFixed(1)}" cy="${maxPt.y.toFixed(1)}" r="2.2" fill="#ff2d78" opacity=".85"/>
               <circle cx="${maxPt.x.toFixed(1)}" cy="${maxPt.y.toFixed(1)}" r="4" fill="none" stroke="#ff2d78" stroke-width=".5" opacity=".5"/>` : ''}
    ${cur    ? `<circle cx="${cur.x.toFixed(1)}"    cy="${cur.y.toFixed(1)}"    r="2.2" fill="${color}"/>` : ''}
  `;

  // Info overlay
  const info = document.getElementById(id+'-info');
  if (info) {
    info.innerHTML = `<span class="spark-max">▲ ${_fmtSpark(dataMax, fmt)}</span> · <span class="spark-now">● ${_fmtSpark(cur.v, fmt)}</span>`;
  }

  // Store latest data on element (référence toujours à jour pour hover)
  el._data = data;
  el._fmt  = fmt;
  _wireSpark(id);
}

function _wireSpark(id) {
  const el  = document.getElementById(id);
  const tip = document.getElementById(id+'-tip');
  if (!el || !tip || el.dataset.wired) return;
  el.dataset.wired = '1';

  el.addEventListener('mousemove', ev => {
    const arr = el._data;
    if (!arr || arr.length < 2) return;
    const r = el.getBoundingClientRect();
    const relX = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    const idx = Math.round(relX * (MAX_H - 1));
    const v = arr[idx];
    if (v == null) return;
    tip.style.display = 'block';
    tip.style.left = (relX * r.width) + 'px';
    tip.style.top  = (ev.clientY - r.top) + 'px';
    tip.textContent = _fmtSpark(v, el._fmt || 'pct');
    const cur = document.getElementById(id+'-cursor');
    if (cur) { cur.setAttribute('x1', (relX*300).toFixed(1)); cur.setAttribute('x2', (relX*300).toFixed(1)); cur.style.opacity = '.8'; }
  });
  el.addEventListener('mouseleave', () => {
    tip.style.display = 'none';
    const cur = document.getElementById(id+'-cursor');
    if (cur) cur.style.opacity = '0';
  });
}

// ── CPU cores ─────────────────────────────────────────────────────────────────
function updateCores(cores) {
  const g=document.getElementById('core-grid'); if(!g) return;
  const cols=Math.min(8,cores.length<=8?cores.length:Math.ceil(cores.length/2));
  g.style.gridTemplateColumns=`repeat(${cols},1fr)`;
  if (g.children.length!==cores.length)
    g.innerHTML=cores.map((_,i)=>`<div class="core-cell" id="cr${i}">-</div>`).join('');
  cores.forEach((p,i)=>{
    const el=document.getElementById(`cr${i}`); if(!el) return;
    const c=pctColor(p);
    el.style.background=`color-mix(in srgb,${c} ${p}%,#141428)`;
    el.style.boxShadow=p>60?`0 0 4px ${c}`:'none';
    el.textContent=Math.round(p)+'%';
    el.style.color=p>30?'rgba(0,0,0,.75)':'var(--dim)';
  });
}

// ── GPU render ────────────────────────────────────────────────────────────────
function renderGPU(gpus) {
  if (!gpus?.length) return;
  const g=gpus[0];
  const util=g.util??0, memPct=g.mem_total_mb?(g.mem_used_mb/g.mem_total_mb*100):0;
  const gCol=pctColor(util), tCol=tempColor(g.temp), vCol=pctColor(memPct);
  const vramUsedGB = g.mem_used_mb!=null ? (g.mem_used_mb/1024).toFixed(1) : '--';
  const vramTotGB  = g.mem_total_mb!=null ? (g.mem_total_mb/1024).toFixed(0) : '--';

  document.getElementById('gpu-content').innerHTML=`
    <div class="gauge-wrap">
      <div class="gauge-outer">
        <svg class="gauge-svg" viewBox="0 0 120 120">
          <circle class="g-track" cx="60" cy="60" r="50"/>
          <circle class="g-glow" id="gpu-glow" cx="60" cy="60" r="50" stroke="${gCol}"/>
          <circle class="g-fill" id="gpu-arc"  cx="60" cy="60" r="50" stroke="${gCol}"/>
        </svg>
        <div class="gauge-center">
          <div class="gauge-val" style="color:${gCol}" id="gpu-pct-v">${Math.round(util)}%</div>
          <div class="gauge-unit">GPU</div>
          <div class="gauge-sub" style="color:${tCol}">${g.temp!=null?g.temp+'°C':'--'}</div>
        </div>
      </div>
    </div>
    <div class="srow"><span class="slabel">MODEL</span><span class="sval" style="color:var(--a3);font-size:.62em">${g.name.replace('NVIDIA GeForce ','')}</span></div>
    ${g.driver ? `<div class="srow"><span class="slabel">DRIVER</span><span class="sval" style="font-size:.62em;">${g.driver}</span></div>` : ''}
    ${g.vbios  ? `<div class="srow"><span class="slabel">VBIOS</span><span class="sval" style="font-size:.62em;color:var(--dim);">${g.vbios}</span></div>` : ''}
    <div style="margin-top:6px;">
      <div class="bar-head">
        <span>VRAM</span>
        <span style="color:${vCol};">${vramUsedGB} / ${vramTotGB} GB · ${Math.round(memPct)}%</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill" style="background:${vCol};width:${Math.min(100, memPct)}%;box-shadow:0 0 6px ${vCol};"></div>
      </div>
    </div>
  `;
  setGauge('gpu-arc','gpu-glow',util,gCol);

  // Fan
  document.getElementById('fan-gpu').textContent=g.fan_pct!=null?Math.round(g.fan_pct)+'%':'--';

  // Power (les éléments pw-gpu et pw-limit ont été retirés — plus utiles)
  const pw = g.power_w ?? 0;
  const lim = g.power_limit_w;
  const pwPct = lim ? pw/lim*100 : 0;
  const elPct = document.getElementById('pw-pct');
  const elBar = document.getElementById('pw-bar');
  if (elPct) elPct.textContent = Math.round(pwPct) + '%';
  if (elBar) elBar.style.width = Math.min(100, pwPct) + '%';

  document.getElementById('footer-gpu-pwr').textContent=pw!=null?Math.round(pw)+' W':'-- W';
  document.getElementById('footer-gpu-t').textContent=g.temp!=null?g.temp+'°C':'--°C';
}

// ── Temps render ──────────────────────────────────────────────────────────────
function renderTemps(temps) {
  const list=document.getElementById('temp-list');
  if (!temps||!Object.keys(temps).length) {
    list.innerHTML=`<div class="cd" style="font-size:.65em;padding:6px 0;color:var(--dim)">
      Chargement des capteurs...
    </div>`; return;
  }
  const entries=Object.entries(temps).filter(([,v])=>v>0&&v<125).sort((a,b)=>b[1]-a[1]);
  list.innerHTML=entries.map(([k,v])=>{
    const name=k.split('/').slice(-2).join(' › ');
    const col=tempColor(v);
    return `<div class="temp-item">
      <span class="temp-name" title="${k}">${name}</span>
      <span class="temp-deg" style="color:${col}">${v}°C</span>
    </div>`;
  }).join('');
}

// ── Fans render ───────────────────────────────────────────────────────────────
function renderFans(fans, gpuFan) {
  const content=document.getElementById('fan-content');
  const gpuRow=`<div class="srow"><span class="slabel">GPU FAN</span><span class="sval c1" id="fan-gpu">${gpuFan!=null?Math.round(gpuFan)+'%':'--'}</span></div>`;
  if (!fans||!Object.keys(fans).length) {
    content.innerHTML=gpuRow+`<div class="cd" style="font-size:.68em;padding:6px 0">Chargement RPM...</div>`;
    return;
  }
  const entries=Object.entries(fans).filter(([,v])=>v>0).sort((a,b)=>b[1]-a[1]);
  content.innerHTML=gpuRow+entries.slice(0,6).map(([k,v])=>{
    const name=k.split('/').slice(-1)[0];
    const col=v>3000?'var(--a3)':v>2000?'var(--a2)':'var(--a1)';
    return `<div class="fan-item"><span class="fan-name">${name}</span><span class="fan-rpm" style="color:${col}">${v} RPM</span></div>`;
  }).join('');
}

// ── Disk ──────────────────────────────────────────────────────────────────────
function renderDisks(disks) {
  if (!disks?.length) return;
  document.getElementById('disk-content').innerHTML=disks.map(d=>{
    const col=pctColor(d.pct), dev=d.device.replace(/[\\:]/g,'')||d.mountpoint;
    return `<div style="margin-bottom:8px;">
      <div class="bar-head"><span>${dev}</span><span style="color:${col}">${d.pct.toFixed(0)}%</span></div>
      <div class="bar-track"><div class="bar-fill" style="background:${col};width:${d.pct}%;box-shadow:0 0 4px ${col}"></div></div>
      <div style="font-size:.58em;color:var(--dim);margin-top:1px">${fmtB(d.used)} / ${fmtB(d.total)}</div>
    </div>`;
  }).join('');
}

// ── IO bar (log scale) ────────────────────────────────────────────────────────
function ioBar(bps, max=600*1024*1024) { return Math.min(100,(Math.log(bps+1)/Math.log(max+1))*100); }

// ── CSV Export ────────────────────────────────────────────────────────────────
function exportCSV() {
  const cols=['Time','CPU%','CPU_Temp_C','RAM%','GPU%','GPU_Temp_C','GPU_Power_W','CPU_Power_W','Net_Down_MB_s','Net_Up_MB_s','Ping_ms'];
  const rows=[cols, ...histFull.map(h=>[
    h.t, h.cpu, h.cpu_temp??'', h.ram, h.gpu??'', h.gpu_temp??'', h.gpu_pwr??'', h.cpu_pwr??'',
    (h.net_r/1048576).toFixed(3), (h.net_s/1048576).toFixed(3), h.ping??''
  ])];
  const csv=rows.map(r=>r.join(',')).join('\n');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download=`pc_stats_${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.csv`;
  a.click();
}

// ── HUD mode ──────────────────────────────────────────────────────────────────
function toggleHUD() {
  hudVisible=!hudVisible;
  document.getElementById('hud').className=hudVisible?'visible':'';
}
document.addEventListener('keydown', e=>{ if(e.key==='g'||e.key==='G') toggleHUD(); });

// HUD drag
const hud=document.getElementById('hud');
let dragging=false, dx=0, dy=0;
hud.addEventListener('mousedown', e=>{ if(e.target.id==='hud-close') return; dragging=true; dx=e.clientX-hud.offsetLeft; dy=e.clientY-hud.offsetTop; });
document.addEventListener('mousemove', e=>{ if(!dragging) return; hud.style.right='auto'; hud.style.left=(e.clientX-dx)+'px'; hud.style.top=(e.clientY-dy)+'px'; });
document.addEventListener('mouseup', ()=>dragging=false);

// ── Main update ───────────────────────────────────────────────────────────────
async function update() {
  try {
    const resp = await fetch('/api/stats');
    if (!resp.ok) throw new Error(resp.status);
    const d = await resp.json();
    frameCount++;

    // ── CPU
    const cpuPct=d.cpu.pct, cpuCol=pctColor(cpuPct), cpuT=d.cpu.temp;
    setGauge('cpu-arc','cpu-glow',cpuPct,cpuCol);
    setText('cpu-pct',cpuPct+'%','color:'+cpuCol);
    setText('cpu-freq',d.cpu.freq_mhz?d.cpu.freq_mhz+' MHz':'--');
    setText('cpu-cores',(d.cpu.physical??'?')+'C / '+(d.cpu.logical??'?')+'T');
    const ctCol=tempColor(cpuT);
    setText('cpu-temp',cpuT!=null?cpuT+'°C':'--','color:'+ctCol);
    setText('cpu-temp-g',cpuT!=null?cpuT+'°C':'--','color:'+ctCol);
    setText('cpu-power',d.cpu.power_w!=null?d.cpu.power_w+' W':'--','color:var(--a1)');
    // Badge THROTTLE (fréquence sous-cadencée sous charge)
    const throttleBadge = document.getElementById('cpu-throttle-badge');
    if (throttleBadge) throttleBadge.style.display = d.cpu.throttle ? 'inline-block' : 'none';
    // Modèle CPU
    if (d.cpu.name) setText('cpu-name', d.cpu.name.replace(/^(Intel|AMD)\(R\) /,'').replace(' CPU','').slice(0,32));
    // Infos système (BIOS + mobo + écrans)
    if (d.system?.bios) {
      const b = d.system.bios;
      const mobo = b.mobo || '--';
      setText('sys-mobo', mobo.slice(0, 34));
      const biosStr = [b.vendor, b.version, b.date].filter(x => x).join(' · ') || '--';
      setText('sys-bios', biosStr.slice(0, 34));
    }
    if (d.system?.displays) {
      const disp = d.system.displays;
      const dspStr = disp.length
        ? disp.map(x => x.label).join(' + ')
        : 'aucun détecté';
      setText('sys-displays', dspStr.slice(0, 42));
    }

    // ── RAM
    const ramPct=d.mem.pct, ramCol=pctColor(ramPct);
    setGauge('ram-arc','ram-glow',ramPct,ramCol);
    setText('ram-pct',ramPct+'%','color:'+ramCol);
    setText('ram-used-g',fmtB(d.mem.used));
    setText('ram-used',fmtB(d.mem.used));
    setText('ram-total',fmtB(d.mem.total));
    setText('ram-free',fmtB(d.mem.free));
    setText('ram-swap',d.mem.swap_total?fmtB(d.mem.swap_used)+' / '+fmtB(d.mem.swap_total):'N/A');

    // ── GPU
    if (d.gpus?.length) renderGPU(d.gpus);

    // ── CPU Power & breakdown certifié (MESURÉ vs ESTIMÉ)
    const cpuW = d.cpu.power_w, gpuW = d.gpus?.[0]?.power_w ?? 0;
    const cpuEst = d.cpu.power_est === true;
    const p = d.power || {};

    // Petits helpers pour badges MESURÉ / ESTIMÉ
    const _tag = (est) => est
      ? '<span class="tag-est" title="Estimé - pas de mesure directe">EST</span>'
      : '<span class="tag-real" title="Mesure hardware directe">RÉEL</span>';

    // CPU (mesuré ou estimé)
    const cpuEl = document.getElementById('pw-cpu');
    if (cpuEl) cpuEl.innerHTML = (cpuW!=null ? cpuW+' W ' : '-- W ') + _tag(cpuEst);

    // Total AC : la vraie conso à la prise (toujours estimation)
    setText('pw-ac-total', (p.ac_total_w!=null?p.ac_total_w:'--') + ' W');
    setText('pw-eff',      p.psu_eff_pct  ?? '--');
    setText('pw-loss',     p.psu_loss_w   ?? '--');
    // Breakdown détaillé
    setText('pw-b-cpu',    (cpuW ?? '--') + ' W');
    setText('pw-b-gpu',    gpuW.toFixed(1) + ' W');
    setText('pw-b-mobo',   (p.mobo_w ?? '--') + ' W');
    setText('pw-b-ram',    (p.ram_w ?? '--') + ' W');
    setText('pw-b-ssd',    (p.ssd_w ?? '--') + ' W');
    setText('pw-b-hdd',    (p.hdd_w ?? '--') + ' W');
    setText('pw-b-fans',   (p.fans_w ?? '--') + ' W');
    setText('pw-b-extra',  (p.extra_w ?? '--') + ' W');
    setText('pw-b-screens',(p.screens_w ?? '--') + ' W');
    setText('pw-b-dc',     (p.dc_total_w ?? '--') + ' W');

    setText('footer-cpu-pwr',cpuW!=null?cpuW+' W':'-- W');
    setText('footer-cpu-t',cpuT!=null?cpuT+'°C':'--°C');
    setText('footer-total', (p.ac_total_w!=null?p.ac_total_w+' W':'-- W'));

    // ── Disks
    renderDisks(d.disks);

    // ── Cores
    if (d.cpu.cores) updateCores(d.cpu.cores);

    // ── IO
    const dr=d.io.disk_r, dw=d.io.disk_w;
    setText('disk-r',fmtSpd(dr));
    setText('disk-w',fmtSpd(dw));
    document.getElementById('disk-r-bar').style.width=ioBar(dr)+'%';
    document.getElementById('disk-w-bar').style.width=ioBar(dw)+'%';

    // ── Network
    const nr=d.io.net_r, ns=d.io.net_s;
    setText('net-r',fmtSpd(nr));
    setText('net-s',fmtSpd(ns));
    document.getElementById('net-r-bar').style.width=ioBar(nr,125*1024*1024)+'%';
    document.getElementById('net-s-bar').style.width=ioBar(ns,125*1024*1024)+'%';

    // ── Ping
    const pm=d.ping_ms;
    const pingTxt=pm!=null?pm+' ms':'-- ms';
    const pingCol=pingColor(pm);
    setText('ping-val',pingTxt,'color:'+pingCol);
    setText('footer-ping',pingTxt,'color:'+pingCol);

    // ── Temps
    renderTemps(d.temps);
    renderFans(d.fans, d.gpus?.[0]?.fan_pct);

    // ── Sparklines
    addH(H.cpu,cpuPct);
    addH(H.gpu,d.gpus?.[0]?.util??0);
    addH(H.io,dr/1048576);
    addH(H.net,nr/1048576);
    drawSpark('cpu-spark',H.cpu,'var(--a1)', 100, 'pct');
    drawSpark('io-spark', H.io, 'var(--a2)', 0,   'bps');
    drawSpark('net-spark',H.net,'#ff8c00',   0,   'bps');

    // ── HUD
    setText('h-cpu',cpuPct+'%');
    setText('h-cpu-t',cpuT!=null?cpuT+'°C':'--°C','color:'+ctCol);
    setText('h-gpu',Math.round(d.gpus?.[0]?.util??0)+'%');
    const gtCol=tempColor(d.gpus?.[0]?.temp);
    setText('h-gpu-t',d.gpus?.[0]?.temp!=null?d.gpus[0].temp+'°C':'--°C','color:'+gtCol);
    setText('h-ram',d.mem.pct+'%');
    setText('h-pwr', (p.ac_total_w != null ? p.ac_total_w : '--') + ' W');
    setText('h-ping',pingTxt,'color:'+pingCol);

    // ── Alerts
    checkAlerts(d);

    // ── History for CSV
    histFull.push({
      t:new Date().toISOString(), cpu:cpuPct, cpu_temp:cpuT, ram:d.mem.pct,
      gpu:d.gpus?.[0]?.util, gpu_temp:d.gpus?.[0]?.temp, gpu_pwr:Math.round(gpuW),
      cpu_pwr:cpuW, net_r:nr, net_s:ns, ping:pm
    });
    if (histFull.length>MAX_H) histFull.shift();

    // ── Energy & cost
    if (d.energy_wh != null) {
      const wh  = d.energy_wh;
      const kwh = wh / 1000;
      setText('pw-wh',  wh.toFixed(1) + ' Wh');
      setText('pw-kwh', kwh.toFixed(4) + ' kWh');
      setText('footer-wh',  wh.toFixed(1) + ' Wh');
      setText('footer-kwh', kwh.toFixed(4) + ' kWh');
      _lastWh = wh;
      updateCost();
    }
    // ── Cumuls persistants jour/mois/année
    if (d.energy_totals) renderEnergyTotals(d.energy_totals);
    // ── Auto-heal : sync état bouton + événements + diag live + throttles actifs
    if (d.heal) {
      if (d.heal.auto_enabled !== _autoHealOn) _renderHealBtn(d.heal.auto_enabled);
      processHealEvents(d.heal.events);
      updateHealDiag(d.heal.diag);
      renderHealActive(d.heal.active);
    }
    // ── Fan control (rendu léger si panel visible)
    if (d.fan_controls && _fanVisible) renderFanControls(d.fan_controls);
    if (d.session_start) {
      if (!_sessionStart) _sessionStart = d.session_start;
      const elapsed = Math.floor(Date.now()/1000 - d.session_start);
      const h = Math.floor(elapsed/3600), m = Math.floor((elapsed%3600)/60), s = elapsed%60;
      const fmt = h > 0 ? `${h}h ${String(m).padStart(2,'0')}m` : `${m}m ${String(s).padStart(2,'0')}s`;
      setText('pw-session', fmt);
    }

    // ── Network processes
    if (d.net_procs !== undefined) renderNetProcs(d.net_procs);

    // ── Processes
    if (d.processes) renderProcs(d.processes);

    // ── Events
    if (d.events !== undefined) renderEvents(d.events);

    // ── Energy (kWh)
    if (d.energy_wh != null) {
      const wh = d.energy_wh;
      setText('footer-wh', wh.toFixed(2)+' Wh');
      setText('footer-kwh', (wh/1000).toFixed(5)+' kWh');
    }

    // ── Status
    setText('status-txt',`ONLINE · #${frameCount}`,'color:var(--a1)');
    // ── Auto-monitoring
    if (d.self) {
      setText('self-cpu', d.self.cpu.toFixed(1));
      setText('self-ram', d.self.ram_mb.toFixed(0));
      const su = document.getElementById('self-usage');
      if (su) {
        // Change de couleur si le dash bouffe trop (> 5% CPU ou > 300 MB RAM)
        const heavy = d.self.cpu > 5 || d.self.ram_mb > 300;
        su.style.color = heavy ? 'var(--a3)' : 'var(--dim)';
      }
    }
    setText('footer-ts','MAJ: '+new Date().toLocaleTimeString('fr-FR'));

  } catch(e) {
    setText('status-txt','ERREUR: '+e.message,'color:var(--a3)');
  }
}

function setText(id, txt, style='') {
  const el=document.getElementById(id); if(!el) return;
  el.textContent=txt;
  if (style) el.style.cssText=(el.style.cssText||'')+';'+style;
}

// ── Clock ─────────────────────────────────────────────────────────────────────
setInterval(()=>{ document.getElementById('clock').textContent=new Date().toLocaleTimeString('fr-FR',{hour12:false}); },1000);

// ── HEAL Forge (soins CPU/RAM/Disque/Net/GPU) ─────────────────────────────────
// Cible → cartes qui clignotent vert pendant le soin
const _HEAL_CARDS = {
  cpu:  ['cpu-card', 'cores-card'],
  ram:  ['ram-card'],
  disk: ['diskio-card', 'storage-card'],
  net:  ['net-card'],
  gpu:  ['gpu-card', 'power-card'],
};

function _startHealing(cardIds, durationMs) {
  cardIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.add('healing');
  });
  setTimeout(() => {
    cardIds.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.remove('healing');
    });
  }, durationMs || 3500);
}

async function healOne(target) {
  const cards = _HEAL_CARDS[target] || [];
  _startHealing(cards, 5000);
  // Effet sonore "eau froide" : whoosh (bip descendant)
  beep(1200, 120, 0.08);
  setTimeout(() => beep(600, 250, 0.06), 100);
  try {
    const r = await fetch('/api/heal', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({target})});
    const d = await r.json();
    if (d.ok) {
      const actions = (d.actions || []).join('\n  ');
      showToast(`💧 EAU FROIDE sur ${target.toUpperCase()}\n  ${actions}`, 'ok');
    } else {
      showToast(`Heal ${target} : ${d.msg || 'erreur'}`, 'err');
    }
  } catch(e) { showToast('Heal fail: '+e, 'err'); }
}

// ── Fan Control (LHM ISensor.Control) ───────────────────────────────────────
let _fanVisible   = false;
let _fanDragging  = false;   // évite d'écraser un slider que l'user manipule
let _fanCache     = {};      // {id: {slider dom, valLabel}} pour ne pas recréer à chaque tick

function toggleFanCtrl() {
  _fanVisible = !_fanVisible;
  document.getElementById('fan-body').style.display = _fanVisible ? 'block' : 'none';
  document.getElementById('fan-toggle').textContent = _fanVisible ? '▲ MASQUER' : '▼ AFFICHER';
}

function _fanIcon(sensorName) {
  const n = (sensorName || '').toLowerCase();
  if (n.includes('cpu'))            return '🔥';
  if (n.includes('gpu'))            return '🎮';
  if (n.includes('pump') || n.includes('aio')) return '💧';
  if (n.includes('case') || n.includes('sys'))  return '🌀';
  return '🌀';
}

function renderFanControls(controls) {
  const list = document.getElementById('fan-list');
  if (!list || !controls) return;
  const ids = Object.keys(controls);
  const n = ids.length;
  const info = document.getElementById('fan-info');
  if (info) info.textContent = n ? `${n} canal(aux)` : 'aucun canal contrôlable';
  if (!n) {
    list.innerHTML = `<div style="font-size:.7em;color:var(--dim);padding:10px;text-align:center;">
      Aucun ventilateur contrôlable détecté par LibreHardwareMonitor.<br>
      Cause probable : chipset Super I/O non supporté, BIOS en mode auto, ou fans en DC only.
    </div>`;
    _fanCache = {};
    return;
  }
  // (Re)build DOM si le set des ids a changé
  const wantKeys = ids.sort().join('|');
  if (list.dataset.keys !== wantKeys) {
    list.dataset.keys = wantKeys;
    _fanCache = {};
    list.innerHTML = ids.map(cid => {
      const c = controls[cid];
      const sid = 'fan-' + cid.replace(/[^a-zA-Z0-9]/g, '_');
      return `
        <div class="fan-row" data-id="${encodeURIComponent(cid)}" style="display:flex;align-items:center;gap:10px;padding:6px 10px;background:rgba(0,0,0,.28);border:1px solid var(--border);border-radius:4px;">
          <span style="font-size:1.05em;flex-shrink:0;">${_fanIcon(c.sensor)}</span>
          <div style="flex:1;min-width:0;">
            <div style="font-size:.68em;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${c.sensor}</div>
            <div style="font-size:.52em;color:var(--dim);letter-spacing:.1em;">${c.hw}</div>
          </div>
          <span id="${sid}-mode" style="font-size:.55em;padding:2px 6px;border-radius:3px;border:1px solid var(--border);letter-spacing:.1em;">--</span>
          <input type="range" min="${Math.round(c.min)}" max="${Math.round(c.max)}" step="1" value="50"
                 id="${sid}-slider"
                 style="width:150px;accent-color:var(--a1);cursor:pointer;"
                 onmousedown="_fanDragging=true"
                 onmouseup="_fanDragging=false; setFanFromSlider('${encodeURIComponent(cid)}', this.value)"
                 oninput="document.getElementById('${sid}-val').textContent=this.value+'%'">
          <span id="${sid}-val" style="font-size:.7em;color:var(--a1);min-width:38px;text-align:right;">--%</span>
          <button class="ctrl-btn" onclick="openFanCurve('${encodeURIComponent(cid)}')"
                  title="Courbe automatique temp → PWM"
                  style="border-color:var(--a3);color:var(--a3);font-size:.6em;">📈 CURVE</button>
          <button class="ctrl-btn" onclick="fanSetDefault('${encodeURIComponent(cid)}')"
                  title="Redonner le contrôle au BIOS"
                  style="border-color:var(--a2);color:var(--a2);font-size:.6em;">↺ BIOS</button>
        </div>
      `;
    }).join('');
    // Remplit le cache
    ids.forEach(cid => {
      const sid = 'fan-' + cid.replace(/[^a-zA-Z0-9]/g, '_');
      _fanCache[cid] = {
        slider: document.getElementById(sid + '-slider'),
        val:    document.getElementById(sid + '-val'),
        mode:   document.getElementById(sid + '-mode'),
      };
    });
  }
  // Update valeurs live (sans casser le slider si drag en cours)
  ids.forEach(cid => {
    const c = controls[cid];
    const dom = _fanCache[cid];
    if (!dom) return;
    // Mode label
    if (dom.mode) {
      const isSw = c.mode === 1;
      dom.mode.textContent = isSw ? 'MANUAL' : (c.mode === 2 ? 'BIOS' : 'AUTO');
      dom.mode.style.color        = isSw ? 'var(--a1)' : 'var(--dim)';
      dom.mode.style.borderColor  = isSw ? 'var(--a1)' : 'var(--border)';
      dom.mode.style.background   = isSw ? 'rgba(0,255,249,.08)' : 'transparent';
    }
    // Software PWM value
    if (c.software_pct != null && dom.slider && dom.val) {
      const v = Math.round(c.software_pct);
      if (!_fanDragging) {
        dom.slider.value = v;
        dom.val.textContent = v + '%';
      }
    }
  });
}

async function setFanFromSlider(cidEnc, val) {
  const cid = decodeURIComponent(cidEnc);
  try {
    const r = await fetch('/api/fan/set', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: cid, percent: parseFloat(val)})});
    const d = await r.json();
    if (!d.ok) showToast('Fan set fail: '+(d.msg||''), 'err');
  } catch(e) { showToast('Fan set fail: '+e, 'err'); }
}

async function fanSetDefault(cidEnc) {
  const cid = decodeURIComponent(cidEnc);
  try {
    const r = await fetch('/api/fan/default', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: cid})});
    const d = await r.json();
    if (d.ok) showToast('↺ Contrôle rendu au BIOS', 'ok');
    else      showToast('Fail: '+(d.msg||''), 'err');
  } catch(e) { showToast('Fail: '+e, 'err'); }
}

async function resetAllFans() {
  if (!confirm('Rendre TOUS les ventilateurs au contrôle BIOS ?')) return;
  try {
    const r = await fetch('/api/fan/default', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({})});
    const d = await r.json();
    showToast(d.ok ? `↺ ${(d.results||[]).length} fans rendus au BIOS` : 'Fail', d.ok?'ok':'err');
  } catch(e) { showToast('Fail: '+e, 'err'); }
}

async function rescanFans() {
  try {
    const r = await fetch('/api/fan/rescan', {method:'POST'});
    const d = await r.json();
    showToast(`🔄 Rescan : ${d.count} canaux`, 'ok');
    renderFanControls(d.controls || {});
  } catch(e) { showToast('Rescan fail: '+e, 'err'); }
}

// ── Fan curve editor ──────────────────────────────────────────────────────────
let _fcCurrent = null;   // {cid, source, enabled, points, name}

const _FC_PRESETS = {
  silent:   [[35,20], [50,30], [65,50], [75,75],  [85,100]],
  balanced: [[30,25], [45,35], [60,55], [70,80],  [80,100]],
  perf:     [[25,35], [40,50], [55,75], [65,100], [80,100]],
};

async function openFanCurve(cidEnc) {
  const cid = decodeURIComponent(cidEnc);
  // Récupère la courbe existante depuis settings, sinon défaut balanced
  const r = await fetch('/api/settings');
  const d = await r.json();
  const curves = d.fan_curves || {};
  const cur = curves[cid];
  // Nom lisible depuis le control snapshot
  let name = cid;
  const fanControls = (await (await fetch('/api/stats')).json()).fan_controls || {};
  if (fanControls[cid]) name = fanControls[cid].sensor + '  (' + fanControls[cid].hw + ')';
  _fcCurrent = {
    cid,
    name,
    source:  cur?.source  || 'cpu',
    enabled: cur?.enabled != null ? cur.enabled : true,
    points:  cur?.points  || _FC_PRESETS.balanced.map(p => [...p]),
  };
  document.getElementById('fc-name').textContent    = name.length > 40 ? name.slice(0,40) + '…' : name;
  document.getElementById('fc-source').value        = _fcCurrent.source;
  document.getElementById('fc-enabled').checked     = _fcCurrent.enabled;
  fcRenderPoints();
  document.getElementById('fan-curve-modal').classList.add('open');
}

function closeFanCurve() {
  document.getElementById('fan-curve-modal').classList.remove('open');
  _fcCurrent = null;
}

function fcRenderPoints() {
  if (!_fcCurrent) return;
  const box = document.getElementById('fc-points');
  const pts = _fcCurrent.points;
  box.innerHTML = pts.map((p, i) => `
    <div style="display:flex;gap:6px;align-items:center;">
      <span style="font-size:.55em;color:var(--dim);width:14px;text-align:right;">${i+1}</span>
      <span style="font-size:.65em;color:var(--dim);">temp</span>
      <input type="number" min="0" max="120" step="1" value="${p[0]}"
             oninput="fcUpdatePoint(${i}, 0, this.value)"
             style="width:60px;background:#000;border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:2px;font-family:inherit;font-size:.75em;text-align:right;">
      <span style="font-size:.65em;color:var(--dim);">°C  →  PWM</span>
      <input type="number" min="0" max="100" step="1" value="${p[1]}"
             oninput="fcUpdatePoint(${i}, 1, this.value)"
             style="width:60px;background:#000;border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:2px;font-family:inherit;font-size:.75em;text-align:right;">
      <span style="font-size:.65em;color:var(--dim);">%</span>
      <button class="ctrl-btn" onclick="fcRemovePoint(${i})" style="font-size:.7em;border-color:var(--a3);color:var(--a3);margin-left:auto;">✕</button>
    </div>
  `).join('');
  fcRenderPreview();
}

function fcUpdatePoint(i, col, val) {
  if (!_fcCurrent) return;
  const v = parseFloat(val);
  if (isNaN(v)) return;
  _fcCurrent.points[i][col] = v;
  fcRenderPreview();
}

function fcAddPoint() {
  if (!_fcCurrent) return;
  const last = _fcCurrent.points[_fcCurrent.points.length - 1] || [50, 50];
  _fcCurrent.points.push([Math.min(120, last[0] + 10), Math.min(100, last[1] + 10)]);
  fcRenderPoints();
}

function fcRemovePoint(i) {
  if (!_fcCurrent) return;
  _fcCurrent.points.splice(i, 1);
  fcRenderPoints();
}

function fcLoadPreset(key) {
  if (!_fcCurrent) return;
  _fcCurrent.points = (_FC_PRESETS[key] || _FC_PRESETS.balanced).map(p => [...p]);
  fcRenderPoints();
}

function fcRenderPreview() {
  if (!_fcCurrent) return;
  const svg = document.getElementById('fc-preview');
  if (!svg) return;
  const W = 300, H = 100;
  const pts = [..._fcCurrent.points].sort((a,b) => a[0]-b[0]);
  if (!pts.length) { svg.innerHTML = ''; return; }
  // X = 20-100°C, Y = 100-0% (inverse)
  const xOf = t => Math.max(0, Math.min(W, (t - 20) / 80 * W));
  const yOf = p => H - Math.max(0, Math.min(H, p / 100 * H));
  // Path
  const path = pts.map((p, i) => `${i?'L':'M'} ${xOf(p[0]).toFixed(1)} ${yOf(p[1]).toFixed(1)}`).join(' ');
  const dots = pts.map(p => `<circle cx="${xOf(p[0]).toFixed(1)}" cy="${yOf(p[1]).toFixed(1)}" r="3" fill="#00ff88" stroke="#000" stroke-width=".5"/>`).join('');
  // Grille : 20, 40, 60, 80, 100°C
  let grid = '';
  for (let t = 20; t <= 100; t += 20) {
    const x = xOf(t);
    grid += `<line x1="${x}" y1="0" x2="${x}" y2="${H}" stroke="#222" stroke-width=".5"/>`;
    grid += `<text x="${x+2}" y="${H-2}" fill="#666" font-size="6" font-family="Consolas">${t}°</text>`;
  }
  for (let p = 25; p <= 100; p += 25) {
    const y = yOf(p);
    grid += `<line x1="0" y1="${y}" x2="${W}" y2="${y}" stroke="#222" stroke-width=".5"/>`;
    grid += `<text x="2" y="${y-1}" fill="#666" font-size="6" font-family="Consolas">${p}%</text>`;
  }
  svg.innerHTML = grid +
    `<path d="${path}" fill="none" stroke="#00ff88" stroke-width="1.5" filter="drop-shadow(0 0 3px #00ff88)"/>` +
    dots;
}

async function fcSave() {
  if (!_fcCurrent) return;
  const body = {
    id:       _fcCurrent.cid,
    source:   document.getElementById('fc-source').value,
    enabled:  document.getElementById('fc-enabled').checked,
    points:   _fcCurrent.points,
  };
  try {
    const r = await fetch('/api/fan/curve', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    if (d.ok) {
      showToast(body.enabled ? '📈 Courbe activée' : '📈 Courbe sauvegardée (inactive)', 'ok');
      closeFanCurve();
    } else showToast('Save fail: '+(d.msg||''), 'err');
  } catch(e) { showToast('Save fail: '+e, 'err'); }
}

async function fcDelete() {
  if (!_fcCurrent) return;
  if (!confirm('Supprimer la courbe pour ce ventilateur ?')) return;
  try {
    const r = await fetch('/api/fan/curve', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: _fcCurrent.cid, delete: true})});
    const d = await r.json();
    if (d.ok) { showToast('📈 Courbe supprimée', 'ok'); closeFanCurve(); }
    else       showToast('Delete fail', 'err');
  } catch(e) { showToast('Delete fail: '+e, 'err'); }
}

// Auto-heal toggle ON/OFF : les règles cochées dans ⚙ se déclenchent automatiquement
let _autoHealOn = false;

function _renderHealBtn(on) {
  _autoHealOn = !!on;
  const btn = document.getElementById('heal-toggle-btn');
  if (!btn) return;
  btn.classList.toggle('on', _autoHealOn);
  btn.textContent = _autoHealOn ? '✚ HEAL: ON' : '✚ HEAL: OFF';
}

async function toggleAutoHeal() {
  const next = !_autoHealOn;
  _renderHealBtn(next);   // optimistic
  try {
    const r = await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({heal_auto_enabled: next})});
    const d = await r.json();
    if (d.ok) {
      showToast(next ? '✚ AUTO-HEAL activé' : '○ AUTO-HEAL désactivé', next ? 'ok' : 'info');
      if (next) beep(880, 90, 0.06);
    } else {
      _renderHealBtn(!next); showToast('Toggle heal fail', 'err');
    }
  } catch(e) {
    _renderHealBtn(!next); showToast('Toggle heal fail: '+e, 'err');
  }
}

// Bandeau "eau froide active" (throttles CPU/GPU temporaires en cours)
function renderHealActive(active) {
  const strip = document.getElementById('heal-active-strip');
  if (!strip) return;
  const keys = active ? Object.keys(active) : [];
  if (!keys.length) { strip.style.display = 'none'; return; }
  const parts = keys.map(k => {
    const a = active[k];
    return `💧 ${a.label} · ${a.seconds_left}s`;
  });
  strip.textContent = parts.join('  |  ');
  strip.style.display = 'inline-block';
}

// Déclenche l'animation verte sur les cartes touchées par un auto-heal
const _healSeenEvents = new Set();
function processHealEvents(events) {
  if (!Array.isArray(events)) return;
  events.forEach(ev => {
    const key = `${ev.rule}-${ev.ts}`;
    if (_healSeenEvents.has(key)) return;
    _healSeenEvents.add(key);
    const cards = _HEAL_CARDS[ev.target] || [];
    _startHealing(cards, 5000);
    beep(1200, 120, 0.08);
    setTimeout(() => beep(600, 250, 0.06), 100);
    showToast(`💧 AUTO-HEAL — eau froide sur ${ev.target.toUpperCase()}\n  ${ev.label}`, 'ok');
  });
  // Purge vieux (>60s)
  if (_healSeenEvents.size > 50) {
    const keep = new Set(events.map(e => `${e.rule}-${e.ts}`));
    _healSeenEvents.forEach(k => { if (!keep.has(k)) _healSeenEvents.delete(k); });
  }
}

// ── Energy cost ───────────────────────────────────────────────────────────────
let _currency = '€';

function _fmtCost(cost, cur) {
  cur = cur || _currency;
  if (cost < 0.01)   return (cost * 100).toFixed(2) + ' ¢';
  if (cost < 1)      return (cost * 100).toFixed(1) + ' ¢';
  if (cost < 10)     return cost.toFixed(3) + ' ' + cur;
  return cost.toFixed(2) + ' ' + cur;
}

function updateCost() {
  const priceEl = document.getElementById('kwh-price');
  if (!priceEl) return;
  const price = parseFloat(priceEl.value) || 0.0937;
  const kwh   = _lastWh / 1000;
  const cost  = kwh * price;
  setText('pw-cost', _fmtCost(cost));

  // Cost per hour (extrapolated from current Wh accumulated vs session time)
  if (_sessionStart) {
    const elapsedH = (Date.now()/1000 - _sessionStart) / 3600;
    if (elapsedH > 0.001) {
      const costPerH = (kwh / elapsedH) * price;
      setText('pw-cost-h', costPerH.toFixed(3) + ' ' + _currency + '/h');
    }
  }
}

// Persist price côté serveur quand l'utilisateur change la valeur (blur/enter)
async function saveKwhPrice() {
  const el = document.getElementById('kwh-price');
  const v = parseFloat(el.value);
  if (!(v > 0)) return;
  try {
    await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kwh_price: v})});
    showToast(`Prix kWh sauvegardé : ${v} ${_currency}/kWh`, 'ok');
  } catch(e) { showToast('Erreur save prix: '+e, 'err'); }
}

// Rendu cumuls jour/mois/année (depuis d.energy_totals)
function renderEnergyTotals(t) {
  if (!t) return;
  const price = t.kwh_price || 0.0937;
  _currency = t.currency || '€';
  setText('kwh-currency-lbl', _currency + '/kWh');
  const rows = [
    ['daily',   t.daily_wh   || 0],
    ['monthly', t.monthly_wh || 0],
    ['yearly',  t.yearly_wh  || 0],
  ];
  rows.forEach(([k, wh]) => {
    const kwh = wh / 1000;
    setText(`pw-${k}-kwh`,  kwh.toFixed(kwh < 1 ? 3 : 2) + ' kWh');
    setText(`pw-${k}-cost`, _fmtCost(kwh * price));
  });
}

// ── Processes ─────────────────────────────────────────────────────────────────
let procVisible = false;
let procSort    = 'cpu';
let procData    = [];
let killTarget  = null;

function toggleProc() {
  procVisible = !procVisible;
  document.getElementById('proc-body').style.display = procVisible ? 'block' : 'none';
  document.getElementById('proc-toggle').textContent = procVisible ? '▲ MASQUER' : '▼ AFFICHER';
}

function sortProcs(by) {
  procSort = by;
  document.getElementById('sort-cpu-btn').className = 'tbtn' + (by==='cpu'?' active':'');
  document.getElementById('sort-ram-btn').className = 'tbtn' + (by==='ram'?' active':'');
  renderProcs(procData);
}

let procFilter = '';

function filterProcs(q) {
  procFilter = q.trim().toLowerCase();
  renderProcs(procData);
}

function renderProcs(procs) {
  procData = procs || [];
  if (!procVisible) return;
  const filtered = procFilter
    ? procData.filter(p => (p.name||'').toLowerCase().includes(procFilter))
    : procData;
  const sorted = [...filtered].sort((a,b) => procSort==='cpu' ? b.cpu-a.cpu : b.mem_mb-a.mem_mb);
  const el = document.getElementById('proc-list');
  if (!el) return;
  const maxCpu = Math.max(...sorted.map(p=>p.cpu), 1);
  const maxRam = Math.max(...sorted.map(p=>p.mem_mb), 1);
  el.innerHTML = `
    <div class="proc-row" style="font-size:.58em;color:var(--dim);border-bottom:1px solid var(--border);padding-top:4px;">
      <span>PID</span><span>NOM</span><span style="text-align:right">CPU%</span><span style="text-align:right">RAM</span><span></span>
    </div>
    ${sorted.map(p=>{
      const cpuCol = pctColor(p.cpu);
      const cpuBar = Math.min(100, (p.cpu/maxCpu)*100);
      const ramBar = Math.min(100, (p.mem_mb/maxRam)*100);
      const critTag = p.critical
        ? '<span class="crit-tag" title="Process système - kill = risque de crash Windows">SYS</span>'
        : '';
      return `<div class="proc-row ${p.critical?'proc-critical':''}"
                   style="background:linear-gradient(90deg,${cpuCol}18 0%,${cpuCol}18 ${cpuBar}%,transparent ${cpuBar}%);">
        <span class="proc-pid">${p.pid}</span>
        <span class="proc-name" title="${p.name} (${p.user||'?'})">${p.name}${critTag}</span>
        <span class="proc-cpu" style="color:${cpuCol}">${p.cpu}%</span>
        <span class="proc-ram">${p.mem_mb} MB</span>
        <button class="kill-btn ${p.critical?'kill-sys':''}"
                onclick="askKill(${p.pid},'${p.name.replace(/'/g,"\\'")}',${p.cpu},${p.mem_mb},${p.critical?'true':'false'})">✕</button>
      </div>`;
    }).join('')}
  `;
}

let _killCritical = false;

function askKill(pid, name, cpu, mem, critical) {
  killTarget = pid;
  _killCritical = critical;
  document.getElementById('kill-name').textContent = name;
  document.getElementById('kill-info').textContent = `PID ${pid}  |  CPU ${cpu}%  |  RAM ${mem} MB`;
  const warn = document.getElementById('kill-warn');
  if (warn) warn.style.display = critical ? 'block' : 'none';
  const confirmBtn = document.getElementById('kill-confirm-btn');
  if (confirmBtn) {
    confirmBtn.textContent = critical ? 'JE SAIS CE QUE JE FAIS' : 'CONFIRMER';
    confirmBtn.style.background = critical ? '#ff0044' : '';
  }
  document.getElementById('kill-dialog').style.display = 'flex';
  beep(critical ? 220 : 440, 80, 0.08);
}
function closeKill() {
  killTarget = null;
  _killCritical = false;
  document.getElementById('kill-dialog').style.display = 'none';
}
async function confirmKill() {
  if (!killTarget) return;
  try {
    const d = await _apiPost('/api/kill', {pid: killTarget, force: _killCritical});
    beep(d.ok ? 880 : 220, 150);
    if (!d.need_pin) showToast(d.msg, d.ok ? 'ok' : 'err');
  } catch(e) { showToast('Erreur: '+e, 'err'); }
  closeKill();
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function showToast(msg, type='ok') {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.style.cssText='position:fixed;bottom:20px;right:20px;z-index:2000;padding:8px 16px;border-radius:4px;font-size:.72em;font-family:Courier New,monospace;opacity:0;transition:opacity .3s;pointer-events:none;white-space:pre-line;max-width:420px;line-height:1.5;';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  // ok=cyan / err=rouge / info=violet / cold=bleu clair (eau froide)
  const isHealCold = /💧/.test(msg);
  if (isHealCold) {
    el.style.background = 'rgba(125,215,255,.14)';
    el.style.border = '1px solid #7dd7ff';
    el.style.color  = '#a8e6ff';
    el.style.boxShadow = '0 0 20px rgba(125,215,255,.5), inset 0 0 12px rgba(125,215,255,.15)';
  } else {
    el.style.background = type==='ok' ? 'rgba(0,255,249,.15)' : 'rgba(255,45,120,.2)';
    el.style.border = `1px solid ${type==='ok'?'var(--a1)':'var(--a3)'}`;
    el.style.color  = type==='ok' ? 'var(--a1)' : 'var(--a3)';
    el.style.boxShadow = 'none';
  }
  el.style.opacity = '1';
  clearTimeout(el._t);
  el._t = setTimeout(()=>{ el.style.opacity='0'; }, isHealCold ? 5000 : 3000);
}

// ── Events ────────────────────────────────────────────────────────────────────
function renderEvents(events) {
  const list = document.getElementById('events-list');
  const count= document.getElementById('events-count');
  if (!events?.length) {
    if (list) list.innerHTML='<div class="cd" style="font-size:.68em;padding:8px 0">Aucune erreur récente ✓</div>';
    if (count) count.textContent='OK';
    return;
  }
  if (count) { count.textContent=events.length+' evt'; }
  const levelMap = { Critical:'crit', Error:'error', Warning:'warn' };
  list.innerHTML = events.map(e=>{
    const lvl = levelMap[e.level] || 'warn';
    const t   = e.time ? e.time.replace('T',' ').slice(5,16) : '';
    return `<div class="evt-row">
      <span class="evt-lvl ${lvl}">${e.level?.slice(0,4)||'?'}</span>
      <div class="evt-body">
        <span class="evt-src">${e.source}</span>
        <div class="evt-msg" title="${e.msg}">${e.msg}</div>
      </div>
      <span class="evt-time">${t}</span>
    </div>`;
  }).join('');
}

// ── Network processes ─────────────────────────────────────────────────────────
let netProcVisible = false;

function toggleNetProc() {
  netProcVisible = !netProcVisible;
  document.getElementById('net-proc-body').style.display = netProcVisible ? 'block' : 'none';
  document.getElementById('net-proc-toggle').textContent  = netProcVisible ? '▲ MASQUER' : '▼ AFFICHER';
}

function renderNetProcs(procs) {
  const totalConns = procs ? procs.reduce((s, p) => s + p.conns, 0) : 0;
  setText('net-proc-total', totalConns);

  if (!netProcVisible || !procs?.length) return;

  const maxConns = Math.max(...procs.map(p => p.conns), 1);
  const el = document.getElementById('net-proc-list');
  if (!el) return;

  el.innerHTML = `
    <div class="net-row" style="font-size:.58em;color:var(--dim);border-bottom:1px solid var(--border);padding-top:5px;">
      <span>PID</span><span>PROCESSUS</span><span style="text-align:center">CONN.</span><span>REMOTE</span><span></span><span></span>
    </div>
    ${procs.map(p => {
      const barW = Math.round(p.conns / maxConns * 100);
      const col  = p.conns > 10 ? 'var(--a3)' : p.conns > 4 ? 'var(--a2)' : 'var(--a1)';
      const remoteStr = p.remotes?.join('  ') || '--';
      const safeName  = (p.name || '').replace(/'/g,"\\'");
      return `<div class="net-row">
        <span class="net-pid">${p.pid}</span>
        <span class="net-name" title="${p.name}">${p.name}</span>
        <span class="net-conns" style="color:${col}">${p.conns}</span>
        <span class="net-remotes" title="${remoteStr}">${remoteStr}</span>
        <div class="net-bar-wrap">
          <div class="net-bar-mini"><div class="net-bar-fill" style="width:${barW}%;background:${col}"></div></div>
        </div>
        <button onclick="killConns(${p.pid},'${safeName}')" title="Tuer les connexions TCP de ce process"
                style="background:transparent;border:1px solid var(--a3);color:var(--a3);font-family:inherit;font-size:.6em;padding:1px 6px;border-radius:3px;cursor:pointer;">✕ NET</button>
      </div>`;
    }).join('')}
  `;
}

async function killConns(pid, name) {
  if (!confirm(`Fermer TOUTES les connexions TCP de "${name}" (PID ${pid}) ?\n\nLe processus ne sera PAS tué, mais ses connexions actives seront coupées (Close-NetTCPConnection).`)) return;
  try {
    const r = await fetch('/api/net/kill_conns', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({pid})});
    const d = await r.json();
    if (d.ok) showToast(`✂ ${d.closed} connexion(s) fermée(s) pour ${name}`, 'ok');
    else      showToast('Erreur : ' + (d.msg || ''), 'err');
  } catch(e) { showToast('Fail: '+e, 'err'); }
}

// ── FORGE: Presets / GPU / Actions / Settings ────────────────────────────────

let _pinCache = null;   // Session : demande une seule fois par onglet
let _pinRequired = false;   // Détecté depuis /api/settings

function _askPin() {
  if (!_pinRequired) return '';
  if (_pinCache) return _pinCache;
  const p = prompt('🔒 PIN requis pour cette action :');
  if (p) _pinCache = p;
  return p || '';
}

async function _apiPost(url, body={}) {
  const withPin = { ...body, pin: _askPin() };
  const r = await fetch(url, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(withPin)});
  const d = await r.json();
  if (!d.ok && d.need_pin) {
    _pinCache = null;    // reset si mauvais PIN
    showToast('PIN incorrect', 'err');
  }
  return d;
}

async function applyPreset(key) {
  try {
    const d = await _apiPost('/api/preset', {preset:key});
    showToast(d.msg || (d.ok?'Preset appliqué':'Échec'), d.ok?'ok':'err');
    if (d.ok) updatePresetUI(key);
  } catch(e) { showToast('Erreur preset: '+e, 'err'); }
}
function updatePresetUI(active) {
  ['gaming','office','silence'].forEach(k => {
    const b = document.querySelector('.preset-'+k);
    if (b) b.classList.toggle('active', k===active);
  });
  const cur = document.getElementById('preset-cur');
  if (cur) {
    if (active) {
      const colors = {gaming:'var(--a3)', office:'var(--a2)', silence:'var(--a1)'};
      const icons  = {gaming:'⚡',        office:'📊',        silence:'🌙'};
      const col = colors[active] || 'var(--a1)';
      cur.innerHTML = `<span style="color:${col};font-weight:bold;letter-spacing:.15em;padding:2px 8px;border:1px solid ${col};border-radius:3px;background:rgba(0,0,0,.3);box-shadow:0 0 8px ${col};animation:blink 1.5s ease-in-out infinite;">${icons[active]||'●'} ${active.toUpperCase()} ACTIF</span>`;
    } else {
      cur.textContent = '';
    }
  }
}

let _gpuInfo = null;
async function setGpuPowerLimit(watts) {
  try {
    const d = await _apiPost('/api/gpu', {power_limit_w:parseInt(watts)});
    if (d.ok) { _gpuInfo = d.info; showToast(`GPU Power Limit → ${watts}W`, 'ok'); }
    else if (!d.need_pin) showToast('Échec: '+d.msg, 'err');
  } catch(e) { showToast('Erreur GPU: '+e, 'err'); }
}

async function doAction(action, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  try {
    const d = await _apiPost('/api/action', {action});
    if (d.ok || !d.need_pin) showToast(d.msg || (d.ok?'OK':'Échec'), d.ok?'ok':'err');
  } catch(e) { showToast('Erreur: '+e, 'err'); }
}

async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    const d = await r.json();
    // Intervalle de rafraîchissement
    if (d.refresh_interval_s) startPolling(d.refresh_interval_s);
    // GPU slider init
    if (d.gpu_info) {
      _gpuInfo = d.gpu_info;
      const s = document.getElementById('gpu-pl-slider');
      const v = document.getElementById('gpu-pl-val');
      if (s) {
        s.min = Math.round(d.gpu_info.min_w);
        s.max = Math.round(d.gpu_info.max_w);
        s.value = Math.round(d.gpu_info.limit_w);
        v.textContent = s.value + 'W';
      }
    } else {
      // pas de GPU nvidia → cacher le contrôle GPU
      const s = document.getElementById('gpu-pl-slider');
      const v = document.getElementById('gpu-pl-val');
      if (s) s.style.display='none';
      if (v) v.style.display='none';
    }
    // Preset actif
    if (d.current_preset) updatePresetUI(d.current_preset);
    // Modal fields
    const u = document.getElementById('s-webhook-url');
    const l = document.getElementById('s-webhook-level');
    if (u) u.value = d.webhook_url || '';
    if (l) l.value = d.webhook_min_level || 'crit';
    const t = document.getElementById('s-toast-enabled');
    if (t) t.checked = d.toast_enabled !== false;
    // LAN + PIN
    _pinRequired = !!d.pin_set;
    const la = document.getElementById('s-lan-access');
    const li = document.getElementById('s-lan-info');
    const pi = document.getElementById('s-pin-status');
    if (la) la.checked = !!d.lan_access;
    if (li) li.innerHTML = d.lan_access
      ? `Actif → <b style="color:var(--a1)">${d.lan_url}</b> (scan avec ton téléphone sur le même WiFi)`
      : 'Localhost uniquement — coche pour ouvrir au réseau local';
    if (pi) pi.textContent = d.pin_set ? '✓ PIN configuré' : '⚠ Aucun PIN (actions non protégées)';
    // Prix kWh persisté
    const kp = document.getElementById('kwh-price');
    if (kp && d.kwh_price != null) kp.value = d.kwh_price;
    if (d.kwh_currency) {
      _currency = d.kwh_currency;
      setText('kwh-currency-lbl', _currency + '/kWh');
    }
    // Auto-heal : toggle + rendu de la liste des règles
    _renderHealBtn(!!d.heal_auto_enabled);
    renderHealRules(d.heal_catalog, d.heal_rules || {});
    // Refresh dropdown (sync avec le paramètre courant)
    const rSel = document.getElementById('s-refresh');
    if (rSel && d.refresh_interval_s != null) {
      const v = String(d.refresh_interval_s);
      const opts = [...rSel.options].map(o => o.value);
      // Ajoute une option si la valeur ne matche aucun preset
      if (!opts.includes(v)) {
        const o = document.createElement('option');
        o.value = v; o.textContent = `${v} s (custom)`;
        rSel.appendChild(o);
      }
      rSel.value = v;
    }
    // Thème sync
    const tSel = document.getElementById('s-theme');
    const savedTheme = localStorage.getItem('sindri-theme') || localStorage.getItem('pulse-theme') || 'neon';
    if (tSel) tSel.value = savedTheme;
  } catch(e) { console.error(e); }
}

// Icônes selon la cible du heal
const _HEAL_TARGET_ICON = {
  cpu: '🔥', ram: '🧠', disk: '💾', net: '📡', gpu: '🎮',
};

function renderHealRules(catalog, active) {
  const box = document.getElementById('s-heal-rules');
  if (!box || !catalog) return;
  box.dataset.catalog = JSON.stringify(catalog);   // pour le rafraîchissement diag
  box.innerHTML = Object.entries(catalog).map(([key, meta]) => {
    const on = !!active[key];
    const icon = _HEAL_TARGET_ICON[meta.target] || '✚';
    return `
      <div class="heal-rule" data-key="${key}" style="display:flex;flex-direction:column;gap:4px;padding:8px 10px;border:1px solid ${on ? '#00ff88' : 'var(--border)'};border-radius:4px;background:${on ? 'rgba(0,255,136,.06)' : 'rgba(0,0,0,.25)'};transition:all .2s;">
        <div style="display:flex;align-items:center;gap:10px;">
          <input type="checkbox" data-heal-key="${key}" ${on ? 'checked' : ''}
                 style="width:18px;height:18px;accent-color:#00ff88;cursor:pointer;flex-shrink:0;"
                 onchange="onHealRuleToggle(this.closest('.heal-rule'))">
          <span style="font-size:1.1em;">${icon}</span>
          <span style="flex:1;font-size:.75em;color:${on ? '#00ff88' : 'var(--text)'};">${meta.label}</span>
          <span style="font-size:.55em;color:var(--dim);letter-spacing:.15em;text-transform:uppercase;">${meta.target}</span>
          <button onclick="forceHealRule('${meta.target}', '${meta.label.replace(/'/g,"\\'")}')" title="Forcer le soin maintenant (test)"
                  style="background:transparent;border:1px solid var(--a2);color:var(--a2);padding:2px 8px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:.75em;">🧪</button>
        </div>
        <!-- Ligne diagnostic live -->
        <div id="hd-${key}" style="display:flex;align-items:center;gap:8px;font-size:.55em;color:var(--dim);padding-left:32px;">
          <span id="hd-${key}-state" style="min-width:70px;">condition : --</span>
          <div style="flex:1;height:4px;background:#0e0e20;border-radius:2px;overflow:hidden;">
            <div id="hd-${key}-bar" style="height:100%;background:#00ff88;width:0%;transition:width .3s;"></div>
          </div>
          <span id="hd-${key}-time" style="min-width:80px;text-align:right;">--</span>
        </div>
      </div>
    `;
  }).join('');
}

// Rafraîchit l'affichage diag depuis d.heal.diag (appelé par le tick global stats)
function updateHealDiag(diag) {
  if (!diag) return;
  Object.entries(diag).forEach(([key, d]) => {
    const stateEl = document.getElementById(`hd-${key}-state`);
    const barEl   = document.getElementById(`hd-${key}-bar`);
    const timeEl  = document.getElementById(`hd-${key}-time`);
    if (!stateEl) return;
    // État condition
    if (!d.enabled) {
      stateEl.textContent = 'règle décochée';
      stateEl.style.color = 'var(--dim)';
    } else if (d.err) {
      stateEl.textContent = '⚠ erreur';
      stateEl.style.color = 'var(--a3)';
    } else if (d.active) {
      stateEl.textContent = '● CONDITION VRAIE';
      stateEl.style.color = '#00ff88';
    } else {
      stateEl.textContent = '○ condition fausse';
      stateEl.style.color = 'var(--dim)';
    }
    // Barre progress sustain
    if (barEl) {
      const need = d.sustain_needed || 15;
      const sus  = Math.min(need, d.sustained_s || 0);
      const pct  = need > 0 ? (sus / need * 100) : 0;
      barEl.style.width = pct.toFixed(0) + '%';
      barEl.style.background = pct >= 100 ? '#00ff88' : (pct > 0 ? 'var(--a2)' : '#0e0e20');
    }
    // Cooldown / dernier fire
    if (timeEl) {
      if (d.cooldown_left_s > 0) {
        const m = Math.floor(d.cooldown_left_s / 60), s = Math.floor(d.cooldown_left_s % 60);
        timeEl.textContent = `cooldown ${m}:${s.toString().padStart(2,'0')}`;
        timeEl.style.color = 'var(--a2)';
      } else if (d.enabled && d.active) {
        const remaining = Math.max(0, (d.sustain_needed || 15) - (d.sustained_s || 0));
        timeEl.textContent = remaining > 0 ? `fire dans ${remaining.toFixed(0)}s` : '⚡ prêt à firer';
        timeEl.style.color = remaining > 0 ? 'var(--a1)' : '#00ff88';
      } else if (d.last_fired_s_ago != null) {
        const m = Math.floor(d.last_fired_s_ago / 60);
        timeEl.textContent = `fired il y a ${m}min`;
        timeEl.style.color = 'var(--dim)';
      } else {
        timeEl.textContent = 'jamais firé';
        timeEl.style.color = 'var(--dim)';
      }
    }
  });
}

function onHealRuleToggle(row) {
  if (!row) return;
  const cb = row.querySelector('input[type="checkbox"]');
  if (!cb) return;
  row.style.borderColor = cb.checked ? '#00ff88' : 'var(--border)';
  row.style.background  = cb.checked ? 'rgba(0,255,136,.06)' : 'rgba(0,0,0,.25)';
  const txt = row.querySelector('span:nth-of-type(2)');
  if (txt) txt.style.color = cb.checked ? '#00ff88' : 'var(--text)';
}

// Force le firing immédiat d'une règle (bypass sustain + cooldown) pour tester
async function forceHealRule(target, label) {
  showToast('🧪 Force heal : ' + label, 'info');
  const cards = _HEAL_CARDS[target] || [];
  _startHealing(cards, 3500);
  beep(880, 60, 0.05);
  try {
    const r = await fetch('/api/heal', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({target})});
    const d = await r.json();
    if (d.ok) {
      const actions = (d.actions || []).slice(0, 3).join(' · ');
      showToast(`✚ ${target.toUpperCase()} soigné — ${actions}`, 'ok');
    } else {
      showToast('Heal fail: '+ (d.msg||''), 'err');
    }
  } catch(e) { showToast('Heal fail: '+e, 'err'); }
}

async function saveHealRules() {
  const rules = {};
  document.querySelectorAll('#s-heal-rules input[type="checkbox"]').forEach(cb => {
    rules[cb.dataset.healKey] = cb.checked;
  });
  try {
    const r = await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({heal_rules: rules})});
    const d = await r.json();
    if (d.ok) showToast('✚ Règles HEAL enregistrées', 'ok');
    else      showToast('Erreur save règles', 'err');
  } catch(e) { showToast('Erreur: '+e, 'err'); }
}

function openSettings()  { document.getElementById('settings-modal').classList.add('open'); }
function closeSettings() { document.getElementById('settings-modal').classList.remove('open'); }

async function openPowerConfig() {
  const r = await fetch('/api/settings');
  const d = await r.json();
  document.getElementById('pc-eff').value   = d.psu_efficiency   ?? 87;
  document.getElementById('pc-idle').value  = d.psu_idle_loss_w  ?? 8;
  document.getElementById('pc-ram').value   = d.ram_gb           ?? 16;
  document.getElementById('pc-fans').value  = d.num_fans         ?? 4;
  document.getElementById('pc-nvme').value  = d.num_ssd_nvme     ?? 1;
  document.getElementById('pc-sata').value  = d.num_ssd_sata     ?? 0;
  document.getElementById('pc-hdd').value   = d.num_hdd          ?? 0;
  document.getElementById('pc-extra').value = d.extra_devices_w  ?? 5;
  document.getElementById('pc-screens').value = d.screens_manual_w != null ? d.screens_manual_w : '';
  // Info auto : lit la dernière conso écran calculée
  try {
    const s = await (await fetch('/api/stats')).json();
    const screensAuto = s.power?.screens_w;
    const disp = (s.system?.displays || []).map(x => x.label).join(' + ') || 'aucun écran';
    document.getElementById('pc-screens-info').textContent = `Auto (${disp}) : ${screensAuto ?? '--'} W`;
  } catch(e) {}
  document.getElementById('power-config-modal').classList.add('open');
}
function closePowerConfig() { document.getElementById('power-config-modal').classList.remove('open'); }

// ── SMART report modal ───────────────────────────────────────────────────────
function closeSmartModal() { document.getElementById('smart-modal').classList.remove('open'); }
async function openSmartModal() {
  const modal = document.getElementById('smart-modal');
  const body  = document.getElementById('smart-body');
  modal.classList.add('open');
  body.innerHTML = `
    <div style="text-align:center;color:var(--dim);padding:30px;">
      <div style="font-size:1.8em;margin-bottom:8px;">🔍</div>
      <div>Analyse SMART en cours...</div>
      <div style="font-size:.7em;margin-top:6px;opacity:.7;">(peut prendre 5-10 secondes)</div>
    </div>`;
  try {
    const r = await fetch('/api/smart');
    const d = await r.json();
    if (!d.ok || !d.disks || !d.disks.length) {
      // Message différencié selon la cause
      let title, hint;
      if (d.error === 'smartctl_missing') {
        title = 'smartctl.exe non trouvé';
        hint  = `Installe smartmontools :<br><code style="color:var(--a1);">winget install smartmontools.smartmontools</code>`;
      } else if (d.error === 'no_disks') {
        title = 'Aucun disque lisible';
        hint  = 'SINDRI a besoin des droits admin pour lire les disques via SMART (IOCTL PhysicalDrive).<br>Vérifie que <code style="color:var(--a1);">Lancer SINDRI.bat</code> est bien élevé (icône bouclier UAC).';
      } else {
        title = 'Aucun disque détecté par smartctl';
        hint  = d.msg || 'Vérifie que smartmontools est installé et que SINDRI tourne en admin.';
      }
      body.innerHTML = `
        <div style="text-align:center;color:var(--dim);padding:30px;">
          <div style="font-size:1.4em;margin-bottom:10px;color:var(--a3);">⚠ ${title}</div>
          <div style="font-size:.75em;line-height:1.6;opacity:.85;">${hint}</div>
        </div>`;
      return;
    }
    body.innerHTML = d.disks.map(dsk => {
      const badgeCol = dsk.status==='ok'?'#00ff41':(dsk.status==='warn'?'#ff8c00':(dsk.status==='fail'?'#ff2d78':'var(--dim)'));
      const badgeLbl = dsk.status==='ok'?'PASSED':(dsk.status==='warn'?'WARNING':(dsk.status==='fail'?'FAILED':'UNKNOWN'));
      const attrsHtml = Object.entries(dsk.attrs).map(([k,v]) => {
        const label = k.replace(/_/g,' ');
        let val = v;
        // Formats lisibles pour NVMe
        if (k === 'data_units_written' && v != null) val = (v * 512 * 1000 / (1024**4)).toFixed(2) + ' TB écrits';
        else if (k === 'power_on_hours' && v != null) val = v + ' h  (' + (v/24/365).toFixed(2) + ' ans)';
        else if (k === 'percentage_used' && v != null) val = v + ' %  (usure)';
        else if (k === 'available_spare' && v != null) val = v + ' %  (réserve)';
        else if (k === 'Power_On_Hours' && v != null) val = v + ' h  (' + (v/24/365).toFixed(2) + ' ans)';
        return `<tr>
          <td style="color:var(--dim);padding:2px 12px 2px 0;">${label}</td>
          <td style="color:var(--text);text-align:right;padding:2px 0;">${val ?? '--'}</td>
        </tr>`;
      }).join('');
      return `
        <div style="border:1px solid var(--border);border-radius:4px;padding:12px 14px;margin-bottom:12px;background:var(--bg2);">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
            <div style="flex:1;min-width:0;">
              <div style="color:var(--a1);font-weight:bold;font-size:.9em;">${dsk.model}</div>
              <div style="font-size:.68em;color:var(--dim);margin-top:2px;">
                ${dsk.size_gb ? dsk.size_gb + ' GB' : '?? GB'} · ${dsk.type}
                ${dsk.temp ? ' · ' + dsk.temp + '°C' : ''}
                · <span style="opacity:.6;">${dsk.device}</span>
              </div>
            </div>
            <div style="color:${badgeCol};font-weight:bold;text-shadow:0 0 8px ${badgeCol};padding:4px 10px;border:1px solid ${badgeCol};border-radius:3px;font-size:.75em;letter-spacing:.1em;">
              ${badgeLbl}
            </div>
          </div>
          <table style="width:100%;font-size:.72em;border-collapse:collapse;">${attrsHtml}</table>
        </div>
      `;
    }).join('');
  } catch(e) {
    body.innerHTML = `<div style="color:#ff2d78;padding:20px;">Erreur : ${e.message}</div>`;
  }
}
async function savePowerConfig() {
  const scrVal = document.getElementById('pc-screens').value.trim();
  const body = {
    psu_efficiency:   parseFloat(document.getElementById('pc-eff').value),
    psu_idle_loss_w:  parseFloat(document.getElementById('pc-idle').value),
    ram_gb:           parseFloat(document.getElementById('pc-ram').value),
    num_fans:         parseFloat(document.getElementById('pc-fans').value),
    num_ssd_nvme:     parseFloat(document.getElementById('pc-nvme').value),
    num_ssd_sata:     parseFloat(document.getElementById('pc-sata').value),
    num_hdd:          parseFloat(document.getElementById('pc-hdd').value),
    extra_devices_w:  parseFloat(document.getElementById('pc-extra').value),
    screens_manual_w: scrVal === '' ? null : parseFloat(scrVal),
  };
  await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  showToast('Config PC enregistrée','ok');
  closePowerConfig();
}
document.addEventListener('click', ev => {
  const m = document.getElementById('power-config-modal');
  if (m && ev.target === m) closePowerConfig();
});

async function saveSettings() {
  const url   = document.getElementById('s-webhook-url').value.trim();
  const lvl   = document.getElementById('s-webhook-level').value;
  const toast = document.getElementById('s-toast-enabled').checked;
  try {
    await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({webhook_url:url, webhook_min_level:lvl, toast_enabled:toast})});
    showToast('Paramètres enregistrés','ok');
    closeSettings();
  } catch(e) { showToast('Erreur save: '+e, 'err'); }
}

async function testToast() {
  try {
    await fetch('/api/toast_test', {method:'POST'});
    showToast('Toast envoyé — regarde le coin bas-droit de Windows','ok');
  } catch(e) { showToast('Erreur: '+e, 'err'); }
}

async function toggleLanAccess(enabled) {
  const d = await _apiPost('/api/settings', {lan_access: enabled});
  if (d.ok) {
    showToast('LAN → ' + (enabled?'activé':'désactivé') + ' (redémarre SINDRI pour appliquer)', 'ok');
    loadSettings();
  } else if (d.need_pin) {
    document.getElementById('s-lan-access').checked = !enabled;   // revert
  } else {
    showToast('Erreur: '+d.msg, 'err');
    document.getElementById('s-lan-access').checked = !enabled;
  }
}

async function changePin() {
  const oldP = document.getElementById('s-pin-old').value.trim();
  const newP = document.getElementById('s-pin-new').value.trim();
  if (newP && (!/^\d+$/.test(newP) || newP.length < 4 || newP.length > 8)) {
    return showToast('PIN doit être 4-8 chiffres', 'err');
  }
  try {
    const r = await fetch('/api/set_pin', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({old_pin:oldP, new_pin:newP})});
    const d = await r.json();
    showToast(d.msg, d.ok?'ok':'err');
    if (d.ok) {
      document.getElementById('s-pin-old').value = '';
      document.getElementById('s-pin-new').value = '';
      _pinCache = null;
      loadSettings();
    }
  } catch(e) { showToast('Erreur PIN: '+e, 'err'); }
}

async function testWebhook() {
  const url = document.getElementById('s-webhook-url').value.trim();
  if (!url) return showToast('URL vide','err');
  try {
    const r = await fetch('/api/webhook_test', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({webhook_url:url})});
    const d = await r.json();
    showToast(d.msg, d.ok?'ok':'err');
  } catch(e) { showToast('Erreur test: '+e, 'err'); }
}

// Fermer le modal en cliquant en dehors
document.addEventListener('click', ev => {
  const m = document.getElementById('settings-modal');
  if (m && ev.target === m) closeSettings();
});

// ── Autostart ─────────────────────────────────────────────────────────────────
async function refreshAutostart() {
  try {
    const r = await fetch('/api/autostart');
    const d = await r.json();
    const btn = document.getElementById('autostart-btn');
    if (btn) {
      btn.textContent = 'AUTOSTART: ' + (d.enabled ? 'ON ✓' : 'OFF');
      btn.className   = 'tbtn' + (d.enabled ? ' active' : '');
    }
  } catch(e) {}
}
async function toggleAutostart() {
  try {
    const cur = document.getElementById('autostart-btn').textContent.includes('ON');
    const r = await fetch('/api/autostart', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({enable: !cur}) });
    const d = await r.json();
    const btn = document.getElementById('autostart-btn');
    btn.textContent = 'AUTOSTART: ' + (d.enabled ? 'ON ✓' : 'OFF');
    btn.className   = 'tbtn' + (d.enabled ? ' active' : '');
    showToast(d.enabled ? 'Démarrage auto activé' : 'Démarrage auto désactivé');
  } catch(e) { showToast('Erreur autostart: '+e, 'err'); }
}

// ── Historique persistant ────────────────────────────────────────────────────
let _histVisible = false;
let _histCurRange = 1440;

function toggleHistory() {
  _histVisible = !_histVisible;
  document.getElementById('hist-body').style.display = _histVisible ? 'block' : 'none';
  document.getElementById('hist-toggle').textContent  = _histVisible ? '▲ MASQUER' : '▼ AFFICHER';
  if (_histVisible) loadHistory(_histCurRange);
}

async function loadHistory(minutes, ev) {
  _histCurRange = minutes;
  // UI highlight
  document.querySelectorAll('.hist-r').forEach(b => {
    const active = parseInt(b.dataset.r) === minutes;
    b.style.borderColor = active ? 'var(--a1)' : '';
    b.style.color       = active ? 'var(--a1)' : '';
  });
  try {
    const r = await fetch('/api/history?minutes=' + minutes);
    const d = await r.json();
    // Info : nombre de points + plage RÉELLE couverte (pas la plage demandée)
    let infoStr = `${d.count} points`;
    if (d.points && d.points.length >= 2) {
      const first = d.points[0].ts, last = d.points[d.points.length-1].ts;
      const actualH = (last - first) / 3600;
      const actualStr = actualH < 1 ? `${(actualH*60).toFixed(0)}min` :
                        actualH < 48 ? `${actualH.toFixed(1)}h` : `${(actualH/24).toFixed(1)}j`;
      infoStr += `  ·  couverture réelle : ${actualStr}`;
      // Si l'utilisateur demande une plage bien plus grande que ce qu'on a → avertit
      const requestedH = d.range_s/3600;
      if (requestedH > actualH * 1.3) {
        infoStr += `  ·  ⚠ données limitées`;
      }
    } else if (d.points && d.points.length < 2) {
      infoStr += `  ·  historique vide (démarrage récent ?)`;
    }
    document.getElementById('hist-info').textContent = infoStr;
    if (!d.points || !d.points.length) {
      ['hist-cpu','hist-gpu','hist-power','hist-io'].forEach(id=>{
        const el=document.getElementById(id);
        if(el) el.innerHTML='<text x="200" y="45" fill="#333" font-size="10" text-anchor="middle">Aucune donnée dans cette plage</text>';
      });
      return;
    }
    _drawDual('hist-cpu',   d.points, 'cpu_pct',  'cpu_temp', 'var(--a1)', '#ff8c00', 100, 100, 'pct', '°C');
    _drawDual('hist-gpu',   d.points, 'gpu_pct',  'gpu_temp', 'var(--a3)', '#ff8c00', 100, 100, 'pct', '°C');
    // Fallback pour anciens points d'historique sans ac_total_w
    d.points.forEach(p => { if (p.ac_total_w == null) p.ac_total_w = (p.cpu_w||0) + (p.gpu_w||0); });
    _drawDual('hist-power', d.points, 'ram_pct',  'ac_total_w', 'var(--a2)', 'var(--a3)', 100, 0, 'pct', 'W');
    _drawDual('hist-io',    d.points, null,       null,       '#ff8c00',   'var(--a1)', 0,   0, 'bps', 'bps', ['net_r','net_s'], null, ['disk_r','disk_w']);
  } catch(e) { console.error(e); showToast('Erreur historique: '+e, 'err'); }
}

// Dessine deux courbes superposées : key1 (échelle 1) et key2 (échelle 2)
// Ou avec sum1/sum2 pour additionner plusieurs clés (ex: net_r + net_s)
function _drawDual(id, points, key1, key2, col1, col2, cap1, cap2, fmt1, fmt2, sum1, col_alt, sum2) {
  const el = document.getElementById(id);
  if (!el || !points.length) return;
  const W = 400, H = 80;

  const getV1 = p => sum1 ? sum1.reduce((s,k)=>s+(p[k]||0),0) : p[key1];
  const getV2 = p => sum2 ? sum2.reduce((s,k)=>s+(p[k]||0),0) : (key2 ? p[key2] : null);

  const arr1 = points.map(getV1).filter(v=>v!=null);
  const arr2 = points.map(getV2).filter(v=>v!=null);
  const max1 = cap1>0 ? cap1 : Math.max(...arr1, 1);
  const max2 = cap2>0 ? cap2 : Math.max(...arr2, 1);

  const xy1 = points.map((p,i) => {
    const v = getV1(p);
    if (v==null) return null;
    return { x:(i/(points.length-1))*W, y:H-(Math.min(v,max1)/max1)*H, v:v };
  });
  const xy2 = points.map((p,i) => {
    const v = getV2(p);
    if (v==null) return null;
    return { x:(i/(points.length-1))*W, y:H-(Math.min(v,max2)/max2)*H, v:v };
  });

  const pts1 = xy1.filter(p=>p).map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  const pts2 = xy2.filter(p=>p).map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  const fillPts1 = xy1[0] ? `${xy1.filter(p=>p).map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')} ${W},${H} 0,${H}` : '';

  const max1v = arr1.length ? Math.max(...arr1) : 0;
  const max2v = arr2.length ? Math.max(...arr2) : 0;
  const cur1  = arr1.length ? arr1[arr1.length-1] : 0;
  const cur2  = arr2.length ? arr2[arr2.length-1] : 0;

  // Labels de dates sous le graphe (5 timestamps équi-espacés)
  const spanH = points.length > 1 ? (points[points.length-1].ts - points[0].ts) / 3600 : 0;
  const nLab  = 5;
  let labelsSvg = '';
  for (let i = 0; i < nLab; i++) {
    const idx = Math.floor(i * (points.length - 1) / (nLab - 1));
    const p = points[idx];
    if (!p) continue;
    const dt = new Date(p.ts * 1000);
    let label;
    if (spanH > 24*3) {
      // > 3 jours : juste la date
      label = dt.toLocaleDateString('fr-FR', {day:'2-digit', month:'2-digit'});
    } else if (spanH > 24) {
      // 1 → 3 jours : date courte + heure
      label = dt.toLocaleDateString('fr-FR', {day:'2-digit', month:'2-digit'}) + ' ' +
              dt.toLocaleTimeString('fr-FR', {hour:'2-digit', minute:'2-digit'});
    } else {
      // < 24h : heure seule
      label = dt.toLocaleTimeString('fr-FR', {hour:'2-digit', minute:'2-digit'});
    }
    const x = (i / (nLab - 1)) * W;
    const anchor = i === 0 ? 'start' : (i === nLab - 1 ? 'end' : 'middle');
    labelsSvg += `<text x="${x.toFixed(1)}" y="91" fill="#383858" font-size="7" text-anchor="${anchor}" font-family="Consolas,monospace">${label}</text>`;
  }

  el.innerHTML = `
    ${fillPts1 ? `<polygon points="${fillPts1}" fill="${col1}" opacity=".12"/>` : ''}
    ${pts1 ? `<polyline points="${pts1}" fill="none" stroke="${col1}" stroke-width="1.5" opacity=".9"/>` : ''}
    ${pts2 && col2 ? `<polyline points="${pts2}" fill="none" stroke="${col2}" stroke-width="1.5" opacity=".8" stroke-dasharray="3,2"/>` : ''}
    <line class="spark-cursor" id="${id}-cursor" x1="0" y1="0" x2="0" y2="${H}"/>
    <line x1="0" y1="${H}" x2="${W}" y2="${H}" stroke="${col1}" stroke-width=".5" opacity=".2"/>
    ${labelsSvg}
  `;

  const info = document.getElementById(id+'-info');
  if (info) {
    let html = `<span style="color:${col1};">● ${_fmtSpark(cur1, fmt1)}</span> · <span class="spark-max">▲ ${_fmtSpark(max1v, fmt1)}</span>`;
    if (col2 && arr2.length) html += `<br><span style="color:${col2};">◇ ${_fmtSpark(cur2, fmt2)}</span> · <span class="spark-max">▲ ${_fmtSpark(max2v, fmt2)}</span>`;
    info.innerHTML = html;
  }

  // Hover
  el._data = { points, fmt1, fmt2, key1, key2, sum1, sum2, getV1, getV2, col1, col2 };
  const tip = document.getElementById(id+'-tip');
  const cur = document.getElementById(id+'-cursor');
  if (tip && !el.dataset.wired) {
    el.dataset.wired = '1';
    el.addEventListener('mousemove', ev => {
      const dat = el._data;
      if (!dat || !dat.points.length) return;
      const r = el.getBoundingClientRect();
      const relX = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
      const idx = Math.round(relX * (dat.points.length - 1));
      const p = dat.points[idx];
      if (!p) return;
      const v1 = dat.getV1(p), v2 = dat.getV2(p);
      // Plage > 24h → on montre la date, sinon juste l'heure
      const spanH = (dat.points[dat.points.length-1].ts - dat.points[0].ts) / 3600;
      const dt = new Date(p.ts*1000);
      const t  = spanH > 24
        ? dt.toLocaleString('fr-FR', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'})
        : dt.toLocaleTimeString('fr-FR', {hour:'2-digit', minute:'2-digit'});
      let txt = `<span style="color:var(--dim);">${t}</span>` +
                ` <span style="color:${dat.col1};">● ${_fmtSpark(v1, dat.fmt1)}</span>`;
      if (dat.col2 && (dat.key2 || dat.sum2)) {
        txt += ` <span style="color:${dat.col2};">◇ ${_fmtSpark(v2, dat.fmt2)}</span>`;
      }
      tip.innerHTML = txt;
      tip.style.display = 'block';
      tip.style.left = (relX * r.width) + 'px';
      tip.style.top  = (ev.clientY - r.top) + 'px';
      if (cur) { cur.setAttribute('x1', (relX*W).toFixed(1)); cur.setAttribute('x2', (relX*W).toFixed(1)); cur.style.opacity='.8'; }
    });
    el.addEventListener('mouseleave', () => {
      tip.style.display='none';
      if (cur) cur.style.opacity='0';
    });
  }
}

// ── Refresh interval (en secondes, live-configurable) ────────────────────────
let _refreshTimer = null;
let _refreshSec   = 2.0;

function startPolling(sec) {
  _refreshSec = Math.max(0.1, Math.min(30, parseFloat(sec) || 2));
  if (_refreshTimer) clearInterval(_refreshTimer);
  _refreshTimer = setInterval(update, _refreshSec * 1000);
  const sel = document.getElementById('s-refresh');
  if (sel) sel.value = String(_refreshSec);
}
function onIntervalInput(sec) {
  const v = document.getElementById('iv-val');
  if (v) v.textContent = parseFloat(sec).toFixed(1) + ' s';
}
async function setInterval2(sec) {
  const secN = parseFloat(sec);
  startPolling(secN);
  try {
    await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({refresh_interval_s: secN})});
    showToast(`Rafraîchissement → ${secN.toFixed(1)}s`, 'ok');
  } catch(e) { showToast('Erreur: '+e, 'err'); }
}

// ── Start ─────────────────────────────────────────────────────────────────────
update();
startPolling(2.0);   // sera ré-ajusté par loadSettings()
refreshAutostart();
loadSettings();
</script>
</body>
</html>"""

# ── Settings persistence ──────────────────────────────────────────────────────

_settings_default = {
    'webhook_url': '',
    'webhook_min_level': 'crit',   # 'warn' ou 'crit'
    'current_preset': None,
    'gpu_power_limit_w': None,
    'refresh_interval_s': 2.0,     # intervalle de collecte + rafraîchissement UI
    'toast_enabled': True,         # notifications Windows natives sur critique
    'lan_access': False,           # écoute sur 0.0.0.0 (accessible sur le réseau local)
    'pin_hash': '',                # hash SHA256 du PIN (vide = pas de PIN)
    # Conso : configuration hardware pour estimation "wall power" précise
    # (ram_gb / num_ssd_nvme / num_ssd_sata / num_hdd sont auto-détectés au 1er
    # lancement si settings.json n'existe pas — les valeurs ci-dessous sont juste
    # des fallback génériques)
    'psu_efficiency':  87,         # % (Bronze=82, Silver=85, Gold=87-90, Platinum=92, Titanium=94)
    'psu_idle_loss_w': 8,          # pertes fixes du PSU (fan, contrôleur, etc.)
    'ram_gb':          16,         # fallback si détection échoue
    'num_ssd_nvme':    1,          # fallback si détection échoue
    'num_ssd_sata':    0,
    'num_hdd':         0,
    'num_fans':        4,          # ventilateurs 120mm (~1.8W chacun) — défaut mid-tower
    'extra_devices_w': 5,          # RGB/USB/périphériques (fixe)
    'screens_manual_w': None,      # override manuel conso écrans (W). None = calcul auto
    # Coût électrique — cumul persistant + tarif (à ajuster selon fournisseur)
    'kwh_price':          0.20,    # prix par kWh (défaut générique ~ moyenne UE)
    'kwh_currency':       '€',     # symbole devise (€, CHF, $, £…)
    'energy_daily_wh':    0.0,
    'energy_daily_key':   '',      # 'YYYY-MM-DD'
    'energy_monthly_wh':  0.0,
    'energy_monthly_key': '',      # 'YYYY-MM'
    'energy_yearly_wh':   0.0,
    'energy_yearly_key':  '',      # 'YYYY'
    # HUD flottant persistant
    'hud_always_on': False,
    'hud_x':         None,         # None = coin bas-droite auto
    'hud_y':         None,
    # Alertes durée-based (secondes soutenues avant firing webhook/toast)
    'alert_hold_s':  20,           # 0 = firing immédiat (comportement legacy)
    # Courbes fans auto : {ctrl_id: {enabled, source: 'cpu'|'gpu'|'hottest', points: [[temp,pwm], ...]}}
    # Défaut : vide → chaque canal reste en mode manuel/BIOS
    'fan_curves': {},
    # Auto-heal : le bouton HEAL du dashboard toggle ce flag ; les règles cochées
    # dans heal_rules se déclenchent automatiquement quand leur condition est
    # soutenue HEAL_SUSTAIN_S secondes.
    'heal_auto_enabled': False,
    'heal_rules': {
        'ram_high':         True,   # RAM > 85%  → trim working sets
        'temp_hot':         True,   # CPU/GPU > 85°C soutenu → Balanced + GPU limit
        'thermal_throttle': True,   # throttle CPU détecté → Balanced
        'disk_full':        False,  # disque > 90% → vide %TEMP% + Corbeille
        'net_slow':         False,  # ping > 200ms → flush DNS
        'explorer_hog':     False,  # explorer.exe > 15% CPU → redémarre Explorer
        'wsearch_hog':      False,  # WSearch > 5% CPU → stop indexation
    },
}

def _auto_detect_hw():
    """Auto-détecte RAM + nombre de disques pour de meilleurs défauts au 1er lancement.
       Retourne un dict à merger dans les settings par défaut."""
    d = {}
    # RAM installée (arrondi au GB)
    try:
        d['ram_gb'] = int(round(psutil.virtual_memory().total / (1024**3)))
    except Exception:
        pass
    # Disques : compte NVMe / SATA SSD / HDD via WMI Get-PhysicalDisk
    if sys.platform == 'win32':
        try:
            ps = ('Get-PhysicalDisk | Select-Object MediaType,BusType | '
                  'ConvertTo-Json -Compress')
            out = _run(['powershell', '-NoProfile', '-Command', ps], timeout=6)
            if out:
                data = json.loads(out)
                if not isinstance(data, list): data = [data]
                nvme = sata = hdd = 0
                for disk in data:
                    media = str(disk.get('MediaType') or '').upper()
                    bus   = str(disk.get('BusType')   or '').upper()
                    if 'HDD' in media:
                        hdd += 1
                    elif 'SSD' in media:
                        # BusType 17 = NVMe, "NVMe" en string aussi. 11=SATA, 8=RAID
                        if 'NVME' in bus or bus == '17':
                            nvme += 1
                        else:
                            sata += 1
                if (nvme + sata + hdd) > 0:
                    d['num_ssd_nvme'] = nvme
                    d['num_ssd_sata'] = sata
                    d['num_hdd']      = hdd
        except Exception as e:
            print(f'[auto-hw] {e}')
    return d

def _load_settings():
    try:
        if _os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {**_settings_default, **data}
        # Premier lancement : auto-détection matérielle
        auto = _auto_detect_hw()
        if auto:
            print(f'✓ Auto-détection hardware : {auto}')
        return {**_settings_default, **auto}
    except Exception as e:
        print(f'[settings] {e}')
    return dict(_settings_default)

def _save_settings(s):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(s, f, indent=2)
        return True
    except Exception as e:
        print(f'[settings] {e}')
        return False

_settings = _load_settings()

def _hash_pin(pin):
    return hashlib.sha256(str(pin).encode()).hexdigest() if pin else ''

def _check_pin(pin_provided):
    """Vérifie le PIN. Retourne True si OK ou si aucun PIN n'est configuré."""
    saved = _settings.get('pin_hash', '')
    if not saved:
        return True    # pas de PIN → tout est autorisé
    return _hash_pin(pin_provided) == saved

# ── Conso PC totale (estimation "wall power") ────────────────────────────────

def _estimate_screens_w():
    """Estime la conso des écrans détectés (auto) ou utilise l'override manuel.
       Approximation par écran : base = 15W + 8W par million de pixels + 10W si Hz > 100."""
    manual = _settings.get('screens_manual_w')
    if manual is not None:
        try: return max(0.0, float(manual))
        except Exception: pass
    total = 0.0
    for d in _displays_info():
        px_m = (d.get('width', 1920) * d.get('height', 1080)) / 1_000_000
        hz   = d.get('hz', 60)
        w    = 15 + px_m * 8 + (10 if hz > 100 else 0)
        total += w
    return round(total, 1)

def _estimate_total_power(cpu_w, gpu_w, cpu_pct, gpu_pct, disk_read, disk_write, net_recv, net_send):
    """
    Estime la conso TOTALE du PC à la prise (AC wall power), incluant :
    - CPU + GPU (mesurés / estimés)
    - Carte mère (chipset + VRM)
    - RAM (~0.4W/GB actif)
    - Disques (idle vs actif selon I/O)
    - Ventilos
    - USB/RGB/périphériques
    - Pertes PSU (efficacité + idle loss)

    Retourne un dict détaillé (breakdown) pour affichage.
    """
    s = _settings

    # Motherboard + chipset (dépend un peu de la charge CPU)
    mobo_w = 20 + (cpu_pct / 100) * 8    # 20W idle → 28W full load

    # RAM : ~0.3W/GB idle, ~0.5W/GB actif (approche)
    ram_active_ratio = 0.3 + 0.4 * (cpu_pct / 100)
    ram_w = s['ram_gb'] * ram_active_ratio

    # Disques : SSD NVMe/SATA, HDD — idle vs actif selon I/O récent
    disk_busy = 1 if (disk_read + disk_write) > 1_000_000 else 0   # > 1 MB/s = actif
    ssd_nvme_w = s['num_ssd_nvme'] * (4.0 if disk_busy else 0.4)
    ssd_sata_w = s['num_ssd_sata'] * (3.0 if disk_busy else 0.5)
    hdd_w      = s['num_hdd']      * (6.5 if disk_busy else 4.0)

    # Ventilos : ~1.8W chacun (moyenne, dépend du régime)
    fans_w = s['num_fans'] * 1.8

    # USB, RGB, réseau
    extra_w = s['extra_devices_w']
    net_w   = 2.5   # NIC + activité réseau (moyenne)

    # Écrans (branchés sur la même prise multiple typiquement — comptés côté AC)
    screens_w = _estimate_screens_w()

    # Sous-total DC (côté 12V / composants)
    dc_total = (cpu_w + gpu_w + mobo_w + ram_w +
                ssd_nvme_w + ssd_sata_w + hdd_w + fans_w + extra_w + net_w)

    # Pertes PSU : efficacité (typiquement 85-92%) + idle loss fixe
    eff = max(70, min(96, s.get('psu_efficiency', 87))) / 100
    psu_idle = s.get('psu_idle_loss_w', 8)
    ac_pc = dc_total / eff + psu_idle    # AC côté PC (après PSU)
    ac_total = ac_pc + screens_w         # AC total mur = PC + écrans
    psu_loss = ac_pc - dc_total

    return {
        'cpu_w':      round(cpu_w, 1),
        'gpu_w':      round(gpu_w, 1),
        'mobo_w':     round(mobo_w, 1),
        'ram_w':      round(ram_w, 1),
        'ssd_w':      round(ssd_nvme_w + ssd_sata_w, 1),
        'hdd_w':      round(hdd_w, 1),
        'fans_w':     round(fans_w, 1),
        'extra_w':    round(extra_w + net_w, 1),
        'screens_w':  round(screens_w, 1),
        'dc_total_w': round(dc_total, 1),
        'psu_loss_w': round(psu_loss, 1),
        'ac_pc_w':    round(ac_pc, 1),      # AC côté PC seul (sans écrans)
        'ac_total_w': round(ac_total, 1),   # AC total mur (PC + écrans)
        'psu_eff_pct': s.get('psu_efficiency', 87),
    }


def _update_energy_totals(watts, dt_s, now):
    """Cumule Wh dans les compteurs jour/mois/année avec rollover automatique.
       Persiste sur disque toutes les 60s max pour ménager le SSD."""
    global _energy_last_save
    if dt_s <= 0 or dt_s > 300:   # ignore ticks aberrants (sleep, gel)
        return
    wh = (watts or 0) * dt_s / 3600.0
    from datetime import datetime
    d = datetime.fromtimestamp(now)
    dkey, mkey, ykey = d.strftime('%Y-%m-%d'), d.strftime('%Y-%m'), d.strftime('%Y')
    s = _settings
    if s.get('energy_daily_key')   != dkey: s['energy_daily_key']   = dkey; s['energy_daily_wh']   = 0.0
    if s.get('energy_monthly_key') != mkey: s['energy_monthly_key'] = mkey; s['energy_monthly_wh'] = 0.0
    if s.get('energy_yearly_key')  != ykey: s['energy_yearly_key']  = ykey; s['energy_yearly_wh']  = 0.0
    s['energy_daily_wh']   = s.get('energy_daily_wh', 0.0)   + wh
    s['energy_monthly_wh'] = s.get('energy_monthly_wh', 0.0) + wh
    s['energy_yearly_wh']  = s.get('energy_yearly_wh', 0.0)  + wh
    if now - _energy_last_save > 60:
        _energy_last_save = now
        _save_settings(s)


def _local_ip():
    """Retourne l'IP locale principale du PC (ex: 192.168.1.42)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))   # pas d'envoi réel
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

# ── GPU control (nvidia-smi) ──────────────────────────────────────────────────

def _gpu_info():
    """Return current GPU power limit and min/max range."""
    out = _run(['nvidia-smi', '--query-gpu=power.limit,power.min_limit,power.max_limit,power.default_limit',
                '--format=csv,noheader,nounits'])
    if not out:
        return None
    p = [x.strip() for x in out.split(',')]
    if len(p) < 4:
        return None
    try:
        return {
            'limit_w':   float(p[0]),
            'min_w':     float(p[1]),
            'max_w':     float(p[2]),
            'default_w': float(p[3]),
        }
    except Exception:
        return None

def _gpu_set_power_limit(watts):
    """Set GPU power limit in watts. Requires admin."""
    try:
        w = int(round(float(watts)))
        r = subprocess.run(['nvidia-smi', '-pm', '1'],
                           capture_output=True, text=True, timeout=5,
                           creationflags=C_WIN)
        r = subprocess.run(['nvidia-smi', '-pl', str(w)],
                           capture_output=True, text=True, timeout=5,
                           creationflags=C_WIN)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except Exception as e:
        return False, str(e)

# ── Power plan ────────────────────────────────────────────────────────────────

def _set_power_plan(key):
    guid = POWER_PLANS.get(key)
    if not guid:
        return False, f'Plan inconnu: {key}'
    try:
        r = subprocess.run(['powercfg', '/setactive', guid],
                           capture_output=True, text=True, timeout=5,
                           creationflags=C_WIN)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except Exception as e:
        return False, str(e)

def _get_active_plan():
    try:
        r = subprocess.run(['powercfg', '/getactivescheme'],
                           capture_output=True, text=True, timeout=3,
                           creationflags=C_WIN)
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                      r.stdout, re.I)
        if m:
            guid = m.group(1).lower()
            for k, v in POWER_PLANS.items():
                if v == guid:
                    return k
    except Exception:
        pass
    return None

# ── Presets ────────────────────────────────────────────────────────────────────

def _apply_preset(key):
    preset = PRESETS.get(key)
    if not preset:
        return False, f'Preset inconnu: {key}'
    ok_plan, msg_plan = _set_power_plan(preset['plan'])
    gpu_msg = ''
    info = _gpu_info()
    if info:
        target_w = round(info['default_w'] * preset['gpu_pl_pct'] / 100)
        target_w = max(int(info['min_w']), min(int(info['max_w']), target_w))
        ok_gpu, gpu_msg = _gpu_set_power_limit(target_w)
        _settings['gpu_power_limit_w'] = target_w
    _settings['current_preset'] = key
    _save_settings(_settings)
    return True, f"Preset {preset['name']} appliqué (plan={preset['plan']}, GPU={gpu_msg})"

# ── System actions ────────────────────────────────────────────────────────────

def _system_action(action):
    """Execute a system action. Returns (ok, message)."""
    try:
        if action == 'lock':
            subprocess.run(['rundll32.exe', 'user32.dll,LockWorkStation'],
                           creationflags=C_WIN)
            return True, 'Session verrouillée'
        elif action == 'sleep':
            subprocess.run(['rundll32.exe', 'powrprof.dll,SetSuspendState', '0,1,0'],
                           creationflags=C_WIN)
            return True, 'Mise en veille...'
        elif action == 'restart':
            subprocess.run(['shutdown', '/r', '/t', '10', '/c',
                           'Redémarrage demandé depuis SINDRI'],
                           creationflags=C_WIN)
            return True, 'Redémarrage dans 10s (annuler: shutdown /a)'
        elif action == 'shutdown':
            subprocess.run(['shutdown', '/s', '/t', '10', '/c',
                           'Arrêt demandé depuis SINDRI'],
                           creationflags=C_WIN)
            return True, 'Arrêt dans 10s (annuler: shutdown /a)'
        elif action == 'cancel_shutdown':
            subprocess.run(['shutdown', '/a'], creationflags=C_WIN)
            return True, 'Arrêt/redémarrage annulé'
        elif action == 'reboot_uefi':
            subprocess.run(['shutdown', '/r', '/fw', '/t', '10', '/c',
                           'Redémarrage vers UEFI/BIOS demandé depuis SINDRI'],
                           creationflags=C_WIN)
            return True, 'Reboot vers UEFI dans 10s (annuler: shutdown /a)'
        elif action == 'empty_recycle':
            r = subprocess.run(['powershell', '-NoProfile', '-Command',
                               'Clear-RecycleBin -Force -ErrorAction SilentlyContinue'],
                               capture_output=True, text=True, timeout=15,
                               creationflags=C_WIN)
            return True, 'Corbeille vidée'
        elif action == 'clean_temp':
            temp = _os.environ.get('TEMP', '')
            freed = 0
            if temp and _os.path.exists(temp):
                for root, dirs, files in _os.walk(temp):
                    for f in files:
                        try:
                            path = _os.path.join(root, f)
                            freed += _os.path.getsize(path)
                            _os.remove(path)
                        except Exception:
                            pass
            return True, f'%TEMP% nettoyé ({freed/1048576:.1f} MB libérés)'
        elif action == 'restart_explorer':
            subprocess.run(['taskkill', '/F', '/IM', 'explorer.exe'],
                           creationflags=C_WIN)
            time.sleep(0.5)
            subprocess.Popen(['explorer.exe'], creationflags=C_WIN)
            return True, 'Explorer redémarré'
        elif action == 'flush_dns':
            r = subprocess.run(['ipconfig', '/flushdns'],
                               capture_output=True, text=True, timeout=5,
                               creationflags=C_WIN)
            return True, 'Cache DNS vidé'
        else:
            return False, f'Action inconnue: {action}'
    except Exception as e:
        return False, str(e)

# ══ HEAL "EAU FROIDE SUR LA FORGE" ═══════════════════════════════════════════
# Effet VISIBLE : temp/power/RAM chutent après firing. Auto-revert temporel pour
# ne pas paralyser le PC à long terme (typiquement 60s).

HEAL_REVERT_S = 60           # durée après laquelle on rend les réglages "chauds"
_heal_revert_timers = {}      # {'cpu': Timer, 'gpu': Timer, ...} pour annuler si nouveau soin
_heal_backup = {}             # {'cpu_plan': guid, 'cpu_throttle': int, 'gpu_pl': W, ...}

_heal_active = {}   # {'cpu': {'until': ts, 'label': 'boost capé 70%'}, 'gpu': {...}}

def _cancel_revert(key):
    t = _heal_revert_timers.pop(key, None)
    if t:
        try: t.cancel()
        except Exception: pass
    _heal_active.pop(key, None)

def _schedule_revert(key, fn, delay=HEAL_REVERT_S, label=None):
    _cancel_revert(key)
    def _wrapped():
        try: fn()
        except Exception as e: print(f'[heal-revert-{key}] {e}')
        finally: _heal_active.pop(key, None)
    t = threading.Timer(delay, _wrapped)
    t.daemon = True
    t.start()
    _heal_revert_timers[key] = t
    _heal_active[key] = {'until': time.time() + delay, 'label': label or f'{key} bridé'}

def _heal_active_snapshot():
    """Retourne l'état "eau froide" en cours (avec temps restant) pour affichage UI."""
    now = time.time()
    out = {}
    for k, v in list(_heal_active.items()):
        left = max(0, int(v['until'] - now))
        if left <= 0:
            _heal_active.pop(k, None); continue
        out[k] = {'label': v['label'], 'seconds_left': left}
    return out


# ── CPU : bride le boost temporairement (temp + power chutent immédiatement) ──

def _cpu_throttle_max_set(pct):
    """Force PROCTHROTTLEMAX à pct (0-100) sur AC + DC. La fréquence CPU est
       plafonnée → temp descend en quelques secondes. Appliqué à la scheme active."""
    if sys.platform != 'win32': return False
    pct = max(30, min(100, int(pct)))
    # SUB_PROCESSOR alias, PROCTHROTTLEMAX alias
    try:
        for src in ('setacvalueindex', 'setdcvalueindex'):
            subprocess.run(['powercfg', '-' + src, 'SCHEME_CURRENT', 'SUB_PROCESSOR',
                            'PROCTHROTTLEMAX', str(pct)],
                           capture_output=True, timeout=5, creationflags=C_WIN)
        subprocess.run(['powercfg', '/setactive', 'SCHEME_CURRENT'],
                       capture_output=True, timeout=5, creationflags=C_WIN)
        return True
    except Exception as e:
        print(f'[cpu-throttle] {e}')
        return False

def _heal_cpu():
    """💧 EAU FROIDE CPU : cap boost CPU à 70% (temp + power drop), coupe services
       hoggy (WSearch, SysMain), restart Explorer si obèse. Auto-revert 60s."""
    actions = []

    # Sauvegarde du plan actif (pour restore)
    if 'cpu_plan' not in _heal_backup:
        _heal_backup['cpu_plan'] = _get_active_plan()

    # 1. Force plan Balanced (base saine)
    try:
        r = subprocess.run(['powercfg', '/setactive', POWER_PLANS['balanced']],
                           capture_output=True, text=True, timeout=5, creationflags=C_WIN)
        if r.returncode == 0:
            actions.append('plan → Balanced')
    except Exception: pass

    # 2. Cap fréquence CPU à 70% ← EFFET VISIBLE : temp + power chutent en 5-10s
    if _cpu_throttle_max_set(70):
        actions.append('💧 boost CPU capé à 70% (temp ↓)')

    # 3. Coupe indexation Windows (souvent 5-15% CPU cachés)
    for svc in ('WSearch', 'SysMain'):
        try:
            subprocess.run(['sc', 'stop', svc], capture_output=True, timeout=6,
                           creationflags=C_WIN)
            actions.append(f'{svc} suspendu')
        except Exception: pass

    # 4. Explorer si vraiment obèse (>15% depuis un moment)
    try:
        for p in psutil.process_iter(['name', 'cpu_percent']):
            if (p.info.get('name', '').lower() == 'explorer.exe'
                    and (p.info.get('cpu_percent') or 0) > 15):
                subprocess.run(['taskkill', '/F', '/IM', 'explorer.exe'], creationflags=C_WIN)
                time.sleep(0.4)
                subprocess.Popen(['explorer.exe'], creationflags=C_WIN)
                actions.append('Explorer redémarré')
                break
    except Exception: pass

    # 5. Programme le revert (auto-restore boost 100%)
    def _revert():
        _cpu_throttle_max_set(100)
        prev = _heal_backup.pop('cpu_plan', None)
        if prev and prev in POWER_PLANS:
            try: subprocess.run(['powercfg', '/setactive', POWER_PLANS[prev]],
                                 capture_output=True, timeout=5, creationflags=C_WIN)
            except Exception: pass
        print(f'[heal-cpu] eau froide levée — boost CPU 100% restauré')
    _schedule_revert('cpu', _revert, label='CPU boost capé à 70%')

    actions.append(f'⏱ auto-revert dans {HEAL_REVERT_S}s')
    return actions


# ── RAM : vide le standby list + trim working sets (drop % RAM visible) ────────

def _empty_standby_list():
    """Vide la liste standby de Windows (RAM en cache passif) via NtSetSystemInformation.
       C'est ce qui fait le drop RAM le plus spectaculaire. Requiert SeProfileSingleProcessPrivilege."""
    if sys.platform != 'win32': return 0, False
    try:
        import ctypes
        ntdll = ctypes.windll.ntdll
        # Enable SE_PROFILE_SINGLE_PROCESS_NAME privilege
        adv = ctypes.windll.advapi32
        k32 = ctypes.windll.kernel32
        h = ctypes.c_void_p()
        TOKEN_ADJUST_PRIVILEGES = 0x0020; TOKEN_QUERY = 0x0008
        adv.OpenProcessToken(k32.GetCurrentProcess(),
                             TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(h))
        class LUID(ctypes.Structure):
            _fields_ = [('LowPart', ctypes.c_ulong), ('HighPart', ctypes.c_long)]
        class LUID_AND_ATTR(ctypes.Structure):
            _fields_ = [('Luid', LUID), ('Attributes', ctypes.c_ulong)]
        class TP(ctypes.Structure):
            _fields_ = [('PrivilegeCount', ctypes.c_ulong), ('Privileges', LUID_AND_ATTR * 1)]
        luid = LUID()
        adv.LookupPrivilegeValueW(None, 'SeProfileSingleProcessPrivilege', ctypes.byref(luid))
        tp = TP()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x00000002   # SE_PRIVILEGE_ENABLED
        adv.AdjustTokenPrivileges(h, False, ctypes.byref(tp), 0, None, None)
        k32.CloseHandle(h)
        # SystemMemoryListInformation = 80
        # MemoryPurgeStandbyList = 4 (purge all standby)
        SystemMemoryListInformation = 80
        cmd = ctypes.c_int(4)
        rc = ntdll.NtSetSystemInformation(SystemMemoryListInformation,
                                          ctypes.byref(cmd), ctypes.sizeof(cmd))
        return rc, rc == 0
    except Exception as e:
        print(f'[standby] {e}')
        return -1, False

def _heal_ram():
    """💧 EAU FROIDE RAM : purge standby list (drop RAM visible) + trim working sets.
       Aucun process n'est fermé. Windows repagine si besoin réel."""
    actions = []
    # 1. Snapshot avant
    try:
        before = psutil.virtual_memory().percent
    except Exception:
        before = None

    # 2. Purge standby list ← C'EST ÇA QUI FAIT LA GROSSE CHUTE
    rc, ok = _empty_standby_list()
    if ok:
        actions.append('💧 standby list purgée (RAM cache libéré)')
    else:
        actions.append(f'standby purge partielle (rc={rc})')

    # 3. Trim working sets de TOUS les process
    import ctypes
    K32 = ctypes.windll.kernel32
    HPROCESS = 0x0400 | 0x0100
    SIZE_MAX = ctypes.c_size_t(-1).value
    trimmed = 0
    for p in psutil.process_iter(['pid']):
        try:
            h = K32.OpenProcess(HPROCESS, False, p.info['pid'])
            if not h: continue
            if K32.SetProcessWorkingSetSize(h, SIZE_MAX, SIZE_MAX): trimmed += 1
            K32.CloseHandle(h)
        except Exception: pass
    actions.append(f'{trimmed} process trimmés')

    # 4. Mesure après (avec petit délai pour laisser Windows updater les compteurs)
    time.sleep(0.3)
    try:
        after = psutil.virtual_memory().percent
        if before is not None and after is not None:
            delta = before - after
            if delta > 0.1:
                actions.append(f'RAM {before:.1f}% → {after:.1f}% (−{delta:.1f}%)')
            else:
                actions.append(f'RAM inchangée ({after:.1f}%)')
    except Exception: pass
    return actions

def _heal_disk():
    """💧 EAU FROIDE DISK : vide %TEMP% + Corbeille + Prefetch + thumbnails + TRIM.
       Libère de l'espace visible sur C: et coupe la charge I/O de fond."""
    actions = []
    total_freed = 0

    def _wipe_dir(path, label):
        nonlocal total_freed
        if not path or not _os.path.exists(path): return
        freed = 0
        for root, dirs, files in _os.walk(path):
            for f in files:
                try:
                    p = _os.path.join(root, f)
                    freed += _os.path.getsize(p)
                    _os.remove(p)
                except Exception: pass
        if freed > 0:
            total_freed += freed
            actions.append(f'{label} vidé ({freed/1048576:.1f} MB)')

    # %TEMP% (user)
    _wipe_dir(_os.environ.get('TEMP', ''), '%TEMP%')
    # C:\Windows\Temp (system)
    _wipe_dir(r'C:\Windows\Temp', 'Windows\\Temp')
    # Prefetch cache (regénéré à la volée)
    _wipe_dir(r'C:\Windows\Prefetch', 'Prefetch')
    # Thumbnails cache
    thumb = _os.path.expandvars(r'%LOCALAPPDATA%\Microsoft\Windows\Explorer')
    if _os.path.exists(thumb):
        freed = 0
        for f in _os.listdir(thumb):
            if 'thumbcache' in f.lower() or 'iconcache' in f.lower():
                try:
                    p = _os.path.join(thumb, f)
                    freed += _os.path.getsize(p)
                    _os.remove(p)
                except Exception: pass
        if freed > 0:
            total_freed += freed
            actions.append(f'thumbnails cache ({freed/1048576:.1f} MB)')
    # Corbeille
    try:
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        'Clear-RecycleBin -Force -ErrorAction SilentlyContinue'],
                        capture_output=True, timeout=10, creationflags=C_WIN)
        actions.append('Corbeille vidée')
    except Exception: pass
    # TRIM background (invisible mais lance)
    try:
        subprocess.Popen(['defrag', 'C:', '/L'], creationflags=C_WIN,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        actions.append('TRIM C: lancé')
    except Exception: pass

    if total_freed > 0:
        actions.insert(0, f'💧 {total_freed/1048576:.0f} MB libérés au total')
    return actions

def _heal_net():
    """Soigne le réseau : flush DNS + reset table ARP + reset routes."""
    actions = []
    try:
        subprocess.run(['ipconfig', '/flushdns'],
                       capture_output=True, timeout=5, creationflags=C_WIN)
        actions.append('cache DNS vidé')
    except Exception:
        pass
    try:
        subprocess.run(['arp', '-d', '*'],
                       capture_output=True, timeout=5, creationflags=C_WIN)
        actions.append('cache ARP vidé')
    except Exception:
        pass
    try:
        # Renouvelle IP sans casser la connexion (release/renew adaptateur)
        subprocess.Popen(['ipconfig', '/registerdns'], creationflags=C_WIN,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        actions.append('DNS ré-enregistré')
    except Exception:
        pass
    return actions

def _heal_gpu():
    """💧 EAU FROIDE GPU : cap power limit à 70% de la valeur courante (temp + power
       drop en 3-5s), reset app clocks. Auto-revert 60s vers la limite précédente."""
    actions = []
    info = _gpu_info()
    if not info:
        return ['aucun GPU nvidia détecté']

    # Sauvegarde limite actuelle pour revert
    try:
        prev_w = int(info['limit_w'])
        if 'gpu_pl' not in _heal_backup:
            _heal_backup['gpu_pl'] = prev_w
    except Exception:
        prev_w = int(info['default_w'])

    # Cap à 70% de la limite actuelle (plancher = min hardware)
    target_w = max(int(info['min_w']), int(prev_w * 0.7))
    ok, msg = _gpu_set_power_limit(target_w)
    if ok:
        actions.append(f'💧 GPU power limit {prev_w}W → {target_w}W (−30%)')
    else:
        actions.append(f'gpu limit fail: {msg}')

    # Programme le revert
    def _revert():
        prev = _heal_backup.pop('gpu_pl', prev_w)
        try:
            _gpu_set_power_limit(prev)
            print(f'[heal-gpu] eau froide levée — GPU power limit restauré à {prev}W')
        except Exception: pass
    _schedule_revert('gpu', _revert, label=f'GPU limité à {target_w}W (−30%)')
    actions.append(f'⏱ auto-revert dans {HEAL_REVERT_S}s')

    # Reset clocks appli
    try:
        subprocess.run(['nvidia-smi', '-rac'],
                       capture_output=True, timeout=5, creationflags=C_WIN)
    except Exception: pass
    return actions

# Map cible → fonction
_HEAL_FN = {
    'cpu':  _heal_cpu,
    'ram':  _heal_ram,
    'disk': _heal_disk,
    'net':  _heal_net,
    'gpu':  _heal_gpu,
}

def _heal(target):
    """Lance un soin sur une cible. Retourne (ok, list_actions, target)."""
    fn = _HEAL_FN.get(target)
    if not fn:
        return False, [f'cible inconnue: {target}'], target
    try:
        return True, fn(), target
    except Exception as e:
        return False, [f'exception: {e}'], target

# ── AUTO-HEAL : registry de règles "si X alors Y" ─────────────────────────────

HEAL_SUSTAIN_S = 15       # secondes de condition soutenue avant firing

def _rule_ram_high(s):
    return (s.get('mem') or {}).get('pct', 0) > 85

def _rule_temp_hot(s):
    ct = (s.get('cpu') or {}).get('temp') or 0
    gt = ((s.get('gpus') or [{}])[0]).get('temp') or 0
    return max(ct, gt) > 85

def _rule_thermal_throttle(s):
    return bool((s.get('cpu') or {}).get('throttle'))

def _rule_disk_full(s):
    return any((d.get('pct') or 0) > 90 for d in s.get('disks', []))

def _rule_net_slow(s):
    p = s.get('ping_ms')
    return p is not None and p > 200

def _rule_explorer_hog(s):
    for p in s.get('processes', []):
        if (p.get('name', '') or '').lower() == 'explorer.exe' and (p.get('cpu') or 0) > 15:
            return True
    return False

def _rule_wsearch_hog(s):
    for p in s.get('processes', []):
        if (p.get('name', '') or '').lower() in ('searchindexer.exe', 'wsearch.exe') and (p.get('cpu') or 0) > 5:
            return True
    return False

# rule_key → (label, target heal, check fn, cooldown_s après firing)
_HEAL_RULES = {
    'ram_high':         ('RAM > 85% → trim working sets',              'ram',  _rule_ram_high,         180),
    'temp_hot':         ('CPU ou GPU > 85°C → Balanced + reset GPU',   'cpu',  _rule_temp_hot,         300),
    'thermal_throttle': ('Thermal throttle CPU → Balanced',             'cpu',  _rule_thermal_throttle, 300),
    'disk_full':        ('Disque > 90% → vide %TEMP% + Corbeille',      'disk', _rule_disk_full,        900),
    'net_slow':         ('Ping > 200ms → flush DNS + ARP',              'net',  _rule_net_slow,         240),
    'explorer_hog':     ('explorer.exe > 15% CPU → redémarre Explorer', 'cpu',  _rule_explorer_hog,     600),
    'wsearch_hog':      ('WSearch > 5% CPU → stop indexation',          'cpu',  _rule_wsearch_hog,      600),
}

_heal_first_seen = {}   # {rule_key: ts} - début de condition soutenue
_heal_last_fired = {}   # {rule_key: ts} - dernier firing (cooldown)
_heal_events     = []   # broadcast récent au client (< 30s) pour animation .healing

_heal_diag = {}   # {rule_key: {enabled, active, sustained_s, cooldown_left, last_fired_s_ago, err}}

def _run_heal_rules(stats):
    """Moteur auto-heal : évalue chaque règle, tient à jour _heal_diag (pour l'UI),
       lance le soin si activée + condition soutenue HEAL_SUSTAIN_S secondes + hors cooldown.
       Note : on met à jour _heal_diag pour TOUTES les règles (même désactivées) pour
       que l'utilisateur voie l'état live dans le panneau HEAL des settings."""
    rules_state = _settings.get('heal_rules') or {}
    auto_on     = bool(_settings.get('heal_auto_enabled'))
    now = time.time()
    # Purge des events > 30s
    _heal_events[:] = [e for e in _heal_events if now - e['ts'] < 30]
    for key, (label, target, check, cooldown) in _HEAL_RULES.items():
        enabled = bool(rules_state.get(key))
        try:
            active = bool(check(stats))
            err = None
        except Exception as e:
            active = False
            err = str(e)
        # Track sustain
        first = _heal_first_seen.get(key)
        if not active:
            _heal_first_seen.pop(key, None)
            sustained = 0
        else:
            if first is None:
                _heal_first_seen[key] = now
                first = now
            sustained = now - first
        last_fire = _heal_last_fired.get(key, 0)
        cd_left   = max(0, cooldown - (now - last_fire)) if last_fire else 0
        _heal_diag[key] = {
            'enabled':          enabled,
            'active':           active,
            'sustained_s':      round(sustained, 1),
            'sustain_needed':   HEAL_SUSTAIN_S,
            'cooldown_left_s':  round(cd_left, 1),
            'last_fired_s_ago': round(now - last_fire, 1) if last_fire else None,
            'err':              err,
        }
        # Fire uniquement si master ON + règle cochée + tous les critères
        if not auto_on or not enabled or not active: continue
        if sustained < HEAL_SUSTAIN_S: continue
        if cd_left > 0: continue
        _heal_last_fired[key] = now
        _heal_first_seen.pop(key, None)
        _heal_events.append({'ts': now, 'rule': key, 'label': label, 'target': target})
        threading.Thread(target=_heal, args=(target,), daemon=True).start()

# ── Webhooks (Discord/Telegram) ───────────────────────────────────────────────

_wh_last_sent = {}   # {alert_key: ts}
_wh_cooldown  = 300  # sec entre 2 alertes du même type
_alert_first_seen = {}   # {alert_key: ts} - timestamp de première détection soutenue

# ── Windows Toast natif (via PowerShell, sans install supplémentaire) ──────────

def _windows_toast(title, msg):
    """Affiche une notification Windows 10+ native. Non-bloquant."""
    if sys.platform != 'win32':
        return
    # Échappe simple-quotes pour PowerShell
    t = str(title).replace("'", "''")
    m = str(msg).replace("'", "''")
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null
$tmpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $tmpl.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($tmpl.CreateTextNode('{t}')) > $null
$nodes.Item(1).AppendChild($tmpl.CreateTextNode('{m}')) > $null
$notif = [Windows.UI.Notifications.ToastNotification]::new($tmpl)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('SINDRI').Show($notif)
"""
    try:
        subprocess.Popen(
            ['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps],
            creationflags=C_WIN,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f'[toast] {e}')

def _send_webhook(msg):
    """Envoie un message à l'URL webhook configurée (Discord/Telegram/generic)."""
    url = _settings.get('webhook_url', '').strip()
    if not url:
        return False
    try:
        import urllib.request
        data = None
        headers = {'Content-Type': 'application/json'}
        if 'discord.com/api/webhooks' in url:
            data = json.dumps({'content': msg}).encode()
        elif 'api.telegram.org' in url:
            # URL type: https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>
            if 'chat_id=' not in url:
                return False
            data = json.dumps({'text': msg}).encode()
        else:
            # Generic webhook
            data = json.dumps({'message': msg, 'timestamp': time.time()}).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        print(f'[webhook] {e}')
        return False

def _check_and_notify(stats):
    """Vérifie les seuils critiques et envoie un webhook si besoin.
       - Rate-limit : 5 min entre 2 alertes du même type
       - Durée soutenue : alert_hold_s secondes de dépassement avant firing (les bips côté
         client restent instantanés — c'est un choix UX volontaire).
    """
    toast_ok   = _settings.get('toast_enabled', True)
    webhook_ok = bool(_settings.get('webhook_url'))
    if not toast_ok and not webhook_ok:
        return
    min_level = _settings.get('webhook_min_level', 'crit')
    hold_s    = int(_settings.get('alert_hold_s', 20))
    now = time.time()
    # (key, message, active?) — active=True quand seuil dépassé
    checks = []
    cpu = stats.get('cpu', {})
    gpu = (stats.get('gpus') or [{}])[0]
    mem = stats.get('mem', {})
    ct = cpu.get('temp')
    if ct is not None:
        checks.append(('cpu_temp_crit', f"🔥 CPU {ct}°C",     ct >= 90))
        if min_level == 'warn':
            checks.append(('cpu_temp_warn', f"⚠ CPU {ct}°C", 80 <= ct < 90))
    gt = gpu.get('temp')
    if gt is not None:
        checks.append(('gpu_temp_crit', f"🔥 GPU {gt}°C", gt >= 90))
    mp = mem.get('pct')
    if mp is not None:
        checks.append(('ram_crit', f"⚠ RAM {mp}%", mp >= 95))
    for disk in stats.get('disks', []):
        dp = disk.get('pct', 0)
        checks.append((f"disk_{disk['device']}",
                       f"💾 Disque {disk['device']} plein à {dp}%",
                       dp >= 95))

    hostname = _os.environ.get('COMPUTERNAME', 'PC')
    for key, msg, active in checks:
        if not active:
            _alert_first_seen.pop(key, None)   # reset si condition retombe
            continue
        first = _alert_first_seen.get(key)
        if first is None:
            _alert_first_seen[key] = now
            continue
        if now - first < hold_s:
            continue   # pas encore assez soutenu
        # Rate-limit anti-spam
        if now - _wh_last_sent.get(key, 0) <= _wh_cooldown:
            continue
        _wh_last_sent[key] = now
        held = int(now - first)
        detail = f"{msg} (soutenu {held}s)" if hold_s > 0 else msg
        if toast_ok:
            _windows_toast('⚡ SINDRI — Alerte', detail)
        if webhook_ok:
            threading.Thread(target=_send_webhook,
                             args=(f"[{hostname}] {detail}",), daemon=True).start()

# ── HTTP Handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_GET(self):
        if self.path == '/api/stats':
            with _lock:
                body = json.dumps(_state['stats']).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/api/autostart':
            self._json({'enabled': _is_autostart()})
        elif self.path == '/api/smart':
            # Rapport SMART complet — peut prendre 2-10s (bloque le handler)
            try:
                report = _smart_report()
                # Support ancien format (liste) + nouveau (dict avec disks)
                if isinstance(report, list):
                    self._json({'ok': True, 'disks': report})
                else:
                    self._json({'ok': not report.get('error'), **report})
            except Exception as e:
                self._json({'ok': False, 'msg': str(e), 'disks': []}, 500)
        elif self.path.startswith('/api/history'):
            # /api/history?minutes=60  ou  ?hours=6  ou  ?days=1
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            minutes = float(q.get('minutes', [0])[0] or 0)
            hours   = float(q.get('hours',   [0])[0] or 0)
            days    = float(q.get('days',    [0])[0] or 0)
            seconds = minutes*60 + hours*3600 + days*86400
            if seconds <= 0:
                seconds = 3600   # défaut 1h
            since = time.time() - seconds
            points = _read_history(since)
            self._json({'points': points, 'count': len(points), 'range_s': seconds})
        elif self.path == '/api/settings':
            safe = {k: v for k, v in _settings.items() if k != 'pin_hash'}
            # Catalogue des règles HEAL (label, target) pour l'UI settings
            heal_catalog = {
                k: {'label': v[0], 'target': v[1]}
                for k, v in _HEAL_RULES.items()
            }
            self._json({
                **safe,
                'pin_set':     bool(_settings.get('pin_hash')),
                'lan_ip':      _local_ip(),
                'lan_url':     f'http://{_local_ip()}:{PORT}',
                'gpu_info':    _gpu_info(),
                'active_plan': _get_active_plan(),
                'presets':     list(PRESETS.keys()),
                'interval_min': INTERVAL_MIN,
                'interval_max': INTERVAL_MAX,
                'heal_catalog': heal_catalog,
            })
        elif self.path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == '/api/autostart':
            body   = self._read_body()
            enable = bool(body.get('enable', True))
            ok     = _set_autostart(enable)
            self._json({'ok': ok, 'enabled': _is_autostart()})
        elif self.path == '/api/kill':
            body  = self._read_body()
            pid   = int(body.get('pid', 0))
            pin   = str(body.get('pin', ''))
            force = bool(body.get('force', False))
            if _settings.get('pin_hash') and not _check_pin(pin):
                self._json({'ok': False, 'msg': 'PIN incorrect', 'need_pin': True}, 403); return
            try:
                p = psutil.Process(pid)
                name = p.name()
                try: user = p.username()
                except Exception: user = ''
                # Refuse par défaut les process critiques (sauf force=true après double-confirm UI)
                if _is_critical(name, user) and not force:
                    self._json({
                        'ok': False, 'critical': True,
                        'msg': f'⚠ {name} est un process SYSTÈME. Kill = risque de crash Windows. Confirme avec force=true si tu es sûr.',
                    }, 403); return
                # Notre propre process : refus catégorique
                if pid == _os.getpid():
                    self._json({'ok': False, 'msg': 'Kill de SINDRI interdit'}, 403); return
                p.terminate()
                self._json({'ok': True, 'msg': f'Process {name} ({pid}) terminé'})
            except Exception as e:
                self._json({'ok': False, 'msg': str(e)}, 500)
        elif self.path == '/api/preset':
            body = self._read_body()
            key  = str(body.get('preset', '')).lower()
            pin  = str(body.get('pin', ''))
            if _settings.get('pin_hash') and not _check_pin(pin):
                self._json({'ok': False, 'msg': 'PIN incorrect', 'need_pin': True}, 403); return
            ok, msg = _apply_preset(key)
            self._json({'ok': ok, 'msg': msg})
        elif self.path == '/api/gpu':
            body = self._read_body()
            watts = body.get('power_limit_w')
            pin  = str(body.get('pin', ''))
            if _settings.get('pin_hash') and not _check_pin(pin):
                self._json({'ok': False, 'msg': 'PIN incorrect', 'need_pin': True}, 403); return
            if watts is None:
                self._json({'ok': False, 'msg': 'power_limit_w requis'}, 400); return
            ok, msg = _gpu_set_power_limit(watts)
            if ok:
                _settings['gpu_power_limit_w'] = int(watts)
                _save_settings(_settings)
            self._json({'ok': ok, 'msg': msg, 'info': _gpu_info()})
        elif self.path == '/api/action':
            body   = self._read_body()
            action = str(body.get('action', ''))
            pin    = str(body.get('pin', ''))
            # Actions sensibles nécessitent le PIN si un est configuré
            SENSITIVE = {'lock','sleep','restart','shutdown','reboot_uefi','clean_temp','empty_recycle','restart_explorer'}
            if action in SENSITIVE and _settings.get('pin_hash'):
                if not _check_pin(pin):
                    self._json({'ok': False, 'msg': 'PIN incorrect', 'need_pin': True}, 403); return
            ok, msg = _system_action(action)
            self._json({'ok': ok, 'msg': msg})
        elif self.path == '/api/set_pin':
            body    = self._read_body()
            old_pin = str(body.get('old_pin', ''))
            new_pin = str(body.get('new_pin', ''))
            # Si un PIN existe, il faut le connaître pour le changer
            if _settings.get('pin_hash') and not _check_pin(old_pin):
                self._json({'ok': False, 'msg': 'Ancien PIN incorrect'}, 403); return
            if new_pin and (not new_pin.isdigit() or len(new_pin) < 4 or len(new_pin) > 8):
                self._json({'ok': False, 'msg': 'PIN doit être 4-8 chiffres'}, 400); return
            _settings['pin_hash'] = _hash_pin(new_pin) if new_pin else ''
            _save_settings(_settings)
            self._json({'ok': True, 'msg': 'PIN désactivé' if not new_pin else 'PIN mis à jour'})
        elif self.path == '/api/settings':
            body = self._read_body()
            # Changer lan_access nécessite le PIN si un existe
            if 'lan_access' in body and _settings.get('pin_hash'):
                if not _check_pin(str(body.get('pin', ''))):
                    self._json({'ok': False, 'msg': 'PIN incorrect', 'need_pin': True}, 403); return
            for k in ('webhook_url', 'webhook_min_level'):
                if k in body: _settings[k] = body[k]
            # Config hardware pour calcul conso PC
            for k in ('psu_efficiency', 'psu_idle_loss_w', 'ram_gb',
                      'num_ssd_nvme', 'num_ssd_sata', 'num_hdd',
                      'num_fans', 'extra_devices_w'):
                if k in body:
                    try: _settings[k] = float(body[k]) if isinstance(body[k], (int,float,str)) else _settings[k]
                    except Exception: pass
            # Override manuel conso écrans (None = calcul auto)
            if 'screens_manual_w' in body:
                v = body['screens_manual_w']
                if v is None or v == '':
                    _settings['screens_manual_w'] = None
                else:
                    try: _settings['screens_manual_w'] = max(0.0, float(v))
                    except Exception: pass
            if 'toast_enabled' in body: _settings['toast_enabled'] = bool(body['toast_enabled'])
            if 'lan_access'    in body:
                new_val = bool(body['lan_access'])
                if new_val != _settings.get('lan_access'):
                    _settings['lan_access'] = new_val
                    _save_settings(_settings)
                    print('⚠ Redémarrer PULSE pour appliquer le changement d\'accès réseau')
            if 'refresh_interval_s' in body:
                try:
                    v = float(body['refresh_interval_s'])
                    _settings['refresh_interval_s'] = max(INTERVAL_MIN, min(INTERVAL_MAX, v))
                except Exception:
                    pass
            # Coût électrique
            if 'kwh_price' in body:
                try: _settings['kwh_price'] = max(0.001, min(2.0, float(body['kwh_price'])))
                except Exception: pass
            if 'kwh_currency' in body:
                cur = str(body['kwh_currency'])[:4]
                if cur: _settings['kwh_currency'] = cur
            # HUD flottant
            if 'hud_always_on' in body: _settings['hud_always_on'] = bool(body['hud_always_on'])
            if 'hud_x' in body:
                try: _settings['hud_x'] = int(body['hud_x'])
                except Exception: pass
            if 'hud_y' in body:
                try: _settings['hud_y'] = int(body['hud_y'])
                except Exception: pass
            # Alertes durée
            if 'alert_hold_s' in body:
                try: _settings['alert_hold_s'] = max(0, min(600, int(body['alert_hold_s'])))
                except Exception: pass
            # Auto-heal (toggle master + règles cochables)
            if 'heal_auto_enabled' in body:
                _settings['heal_auto_enabled'] = bool(body['heal_auto_enabled'])
            if 'heal_rules' in body and isinstance(body['heal_rules'], dict):
                cur = _settings.get('heal_rules') or {}
                for k, v in body['heal_rules'].items():
                    if k in _HEAL_RULES:
                        cur[k] = bool(v)
                _settings['heal_rules'] = cur
            _save_settings(_settings)
            self._json({'ok': True, 'settings': _settings})
        elif self.path == '/api/toast_test':
            _windows_toast('⚡ SINDRI', 'Test de notification Windows ✓')
            self._json({'ok': True, 'msg': 'Toast envoyé'})
        elif self.path == '/api/webhook_test':
            body = self._read_body()
            url_backup = _settings.get('webhook_url', '')
            if 'webhook_url' in body:
                _settings['webhook_url'] = body['webhook_url']
            ok = _send_webhook('🧪 Test depuis SINDRI')
            _settings['webhook_url'] = url_backup
            self._json({'ok': ok, 'msg': 'Envoyé' if ok else 'Échec (URL invalide?)'})
        elif self.path == '/api/net/kill_conns':
            body = self._read_body()
            pid  = int(body.get('pid', 0))
            pin  = str(body.get('pin', ''))
            if _settings.get('pin_hash') and not _check_pin(pin):
                self._json({'ok': False, 'msg': 'PIN incorrect', 'need_pin': True}, 403); return
            if pid <= 0:
                self._json({'ok': False, 'msg': 'PID invalide'}, 400); return
            # Refuse notre propre process
            if pid == _os.getpid():
                self._json({'ok': False, 'msg': 'Impossible sur SINDRI lui-même'}, 403); return
            ok, n, msg = _kill_process_connections(pid)
            self._json({'ok': ok, 'closed': n, 'msg': msg})
        elif self.path == '/api/fan/set':
            body = self._read_body()
            cid  = str(body.get('id', ''))
            pct  = body.get('percent')
            if not cid or pct is None:
                self._json({'ok': False, 'msg': 'id + percent requis'}, 400); return
            ok, msg = _lhm_control_set(cid, pct)
            self._json({'ok': ok, 'msg': msg})
        elif self.path == '/api/fan/default':
            body = self._read_body()
            cid  = str(body.get('id', ''))
            if not cid:
                # Aucun id → reset TOUS les fans au BIOS
                results = []
                for cid2 in list(_lhm_controls.keys()):
                    ok, msg = _lhm_control_default(cid2)
                    results.append({'id': cid2, 'ok': ok, 'msg': msg})
                self._json({'ok': True, 'all': True, 'results': results}); return
            ok, msg = _lhm_control_default(cid)
            self._json({'ok': ok, 'msg': msg})
        elif self.path == '/api/fan/rescan':
            _lhm_scan_controls()
            self._json({'ok': True, 'count': len(_lhm_controls),
                        'controls': _lhm_controls_snapshot()})
        elif self.path == '/api/fan/curve':
            # Sauvegarde/désactive une courbe pour un canal
            # body: {id, enabled, source: 'cpu'|'gpu'|'hottest', points: [[t,pwm], ...]}
            body = self._read_body()
            cid = str(body.get('id', ''))
            if not cid:
                self._json({'ok': False, 'msg': 'id requis'}, 400); return
            curves = _settings.get('fan_curves') or {}
            if body.get('delete'):
                curves.pop(cid, None)
            else:
                src = str(body.get('source', 'cpu'))
                if src not in ('cpu', 'gpu', 'hottest'): src = 'cpu'
                pts = body.get('points') or []
                # Clamp + valide
                clean = []
                for p in pts:
                    try:
                        t = max(0, min(120, float(p[0])))
                        w = max(0, min(100, float(p[1])))
                        clean.append([t, w])
                    except Exception: pass
                curves[cid] = {
                    'enabled': bool(body.get('enabled', True)),
                    'source':  src,
                    'points':  sorted(clean, key=lambda p: p[0]),
                }
            _settings['fan_curves'] = curves
            _save_settings(_settings)
            self._json({'ok': True, 'curves': curves})
        elif self.path == '/api/heal':
            body = self._read_body()
            target = str(body.get('target', 'all')).lower()
            pin    = str(body.get('pin', ''))
            if _settings.get('pin_hash') and not _check_pin(pin):
                self._json({'ok': False, 'msg': 'PIN incorrect', 'need_pin': True}, 403); return
            if target == 'all':
                results = {}
                for t in _HEAL_FN.keys():
                    ok, actions, _ = _heal(t)
                    results[t] = {'ok': ok, 'actions': actions}
                self._json({'ok': True, 'target': 'all', 'results': results})
            else:
                ok, actions, t = _heal(target)
                self._json({'ok': ok, 'target': t, 'actions': actions})
        else:
            self.send_response(404); self.end_headers()


# ── Autostart ─────────────────────────────────────────────────────────────────

def _set_autostart(enable=True):
    """Add/remove from Windows startup via registry."""
    try:
        import winreg, os
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Run',
                             0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        name = 'PULSE'  # clé HKCU\...\Run pour autostart
        if enable:
            py     = sys.executable
            script = os.path.abspath(__file__)
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, f'"{py}" "{script}"')
            print('✓ Autostart activé (démarrage Windows)')
        else:
            try: winreg.DeleteValue(key, name)
            except FileNotFoundError: pass
            print('✓ Autostart désactivé')
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f'[autostart] {e}')
        return False

def _is_autostart():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Run',
                             0, winreg.KEY_QUERY_VALUE)
        try: winreg.QueryValueEx(key, 'PULSE'); return True
        except FileNotFoundError: return False
        finally: winreg.CloseKey(key)
    except: return False


# ── Systray icon (NEON theme, cyan #00fff9) ───────────────────────────────────

NEON_RGB      = (0, 255, 249)
NEON_FILL_RGB = (0, 255, 249, 55)   # remplissage jauge avec alpha
NEON_DIM_RGB  = (0, 80, 90)

_tray_font_cache = None
def _tray_font(size):
    """Cache une font TTF adaptée à la taille demandée."""
    global _tray_font_cache
    if _tray_font_cache and _tray_font_cache[0] == size:
        return _tray_font_cache[1]
    f = None
    for name in ('consolab.ttf', 'segoeuib.ttf', 'arialbd.ttf', 'arial.ttf'):
        try: f = ImageFont.truetype(name, size); break
        except Exception: pass
    if f is None:
        f = ImageFont.load_default()
    _tray_font_cache = (size, f)
    return f

def _make_tray_icon(cpu_pct):
    """Icône 64x64 : cadre cyan + jauge verticale (fond) + % CPU au centre."""
    size = 64
    pct  = max(0, min(100, int(round(cpu_pct))))
    img  = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)

    # Cadre extérieur
    d.rectangle([0, 0, size - 1, size - 1], outline=NEON_DIM_RGB, width=2)

    # Jauge verticale : remplit du bas vers le haut, transparente
    bar_h = int((size - 6) * pct / 100)
    if bar_h > 0:
        d.rectangle([3, size - 3 - bar_h, size - 4, size - 4], fill=NEON_FILL_RGB)

    # Texte % centré (pas de "%" pour rester lisible)
    txt  = str(pct)
    fs   = 40 if len(txt) < 3 else 30
    font = _tray_font(fs)
    try:
        bbox = d.textbbox((0, 0), txt, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (size - tw) // 2 - bbox[0]
        y = (size - th) // 2 - bbox[1]
    except AttributeError:  # PIL < 8: pas de textbbox
        tw, th = font.getsize(txt)
        x, y = (size - tw) // 2, (size - th) // 2
    d.text((x, y), txt, fill=NEON_RGB, font=font)
    return img

def _tray_tooltip(stats):
    """Construit le HUD récap affiché au survol de l'icône (max 127 chars — limite Windows)."""
    if not stats:
        return 'SINDRI\n(collecte en cours…)'
    cpu  = stats.get('cpu')  or {}
    mem  = stats.get('mem')  or {}
    gpus = stats.get('gpus') or []
    pwr  = stats.get('power') or {}

    lines = ['SINDRI']
    cpu_pct = cpu.get('pct')
    cpu_t   = cpu.get('temp')
    if cpu_pct is not None:
        s = f'CPU  {cpu_pct:>4.0f}%'
        if cpu_t: s += f' • {cpu_t:.0f}°C'
        lines.append(s)
    if gpus:
        g = gpus[0]
        gu, gt = g.get('util'), g.get('temp')
        if gu is not None or gt is not None:
            s = 'GPU  '
            s += f'{gu:>4.0f}%' if gu is not None else '  --%'
            if gt: s += f' • {gt:.0f}°C'
            lines.append(s)
    mp = mem.get('pct')
    if mp is not None:
        used_gb = (mem.get('used') or 0) / (1024**3)
        tot_gb  = (mem.get('total') or 0) / (1024**3)
        lines.append(f'RAM  {used_gb:>4.1f}/{tot_gb:.0f} GB ({mp:.0f}%)')
    ac = pwr.get('ac_total_w')
    if ac:
        lines.append(f'PWR  {ac:.0f} W')
    return '\n'.join(lines)[:127]


# ── HUD popup néon (Tk, clic gauche pour toggle) ─────────────────────────────

_hud_queue = queue.Queue()   # 'toggle' | 'close' — commandes venues du thread pystray

def _hud_text(stats):
    """Contenu multi-ligne du popup HUD (plus riche que le tooltip natif)."""
    if not stats:
        return '(collecte en cours…)'
    cpu  = stats.get('cpu')  or {}
    mem  = stats.get('mem')  or {}
    gpus = stats.get('gpus') or []
    pwr  = stats.get('power') or {}
    io   = stats.get('io')   or {}

    lines = []
    cp, ct, cw = cpu.get('pct'), cpu.get('temp'), cpu.get('power_w')
    if cp is not None:
        s = f'CPU   {cp:>5.1f}%'
        if ct: s += f' • {ct:.0f}°C'
        if cw: s += f' • {cw:.0f}W'
        lines.append(s)
    if gpus:
        g = gpus[0]
        gu, gt, gw = g.get('util'), g.get('temp'), g.get('power_w')
        s = 'GPU   '
        s += f'{gu:>5.1f}%' if gu is not None else '   --%'
        if gt: s += f' • {gt:.0f}°C'
        if gw: s += f' • {gw:.0f}W'
        lines.append(s)
    mp = mem.get('pct')
    if mp is not None:
        used_gb = (mem.get('used') or 0) / (1024**3)
        tot_gb  = (mem.get('total') or 0) / (1024**3)
        lines.append(f'RAM   {used_gb:>5.1f} / {tot_gb:.0f} GB  ({mp:.0f}%)')
    ac = pwr.get('ac_total_w')
    if ac:
        lines.append(f'PWR   {ac:.0f} W  (à la prise)')
    def _bps(n):
        n = n or 0
        for u in ('B/s','KB/s','MB/s','GB/s'):
            if n < 1024: return f'{n:.1f} {u}'
            n /= 1024
        return f'{n:.1f} TB/s'
    lines.append(f'NET   ↓ {_bps(io.get("net_r"))}  ↑ {_bps(io.get("net_s"))}')
    return '\n'.join(lines)


def _hud_run():
    """Fenêtre Tk sans bordure, fond noir semi-transparent, texte cyan NEON.
       - Toggle via _hud_queue.put('toggle')
       - Mode 'always on' persistant : ouvert au démarrage si hud_always_on=True
       - Drag depuis le header pour repositionner (position sauvegardée dans settings)
       Bloque le thread où elle est lancée (mainloop Tk). À lancer dans un thread daemon."""
    try:
        import tkinter as tk
    except Exception as e:
        print(f'[hud] tkinter indisponible ({e}) — popup HUD désactivé')
        return

    A1  = '#00fff9'   # cyan NEON
    A2  = '#b44aff'   # violet NEON
    DIM = '#383858'
    TXT = '#c0d0f0'
    BG  = '#0b0b17'

    root = tk.Tk()
    root.withdraw()                          # caché par défaut
    root.overrideredirect(True)              # pas de bordure/titre
    root.attributes('-topmost', True)
    try: root.attributes('-alpha', 0.94)
    except tk.TclError: pass

    # Cadre avec bordure cyan (via highlightthickness sur le Frame)
    outer = tk.Frame(root, bg=A1, bd=0)      # anneau cyan externe
    outer.pack(fill='both', expand=True)
    inner = tk.Frame(outer, bg=BG, bd=0)
    inner.pack(fill='both', expand=True, padx=2, pady=2)

    header = tk.Frame(inner, bg=BG)
    header.pack(fill='x', padx=16, pady=(12, 6))
    tk.Label(header, text='⚡', fg=A1, bg=BG, font=('Consolas', 16, 'bold')).pack(side='left')
    tk.Label(header, text=' SINDRI', fg=A1, bg=BG,
             font=('Consolas', 15, 'bold')).pack(side='left')
    tk.Label(header, text='   monitoring', fg=DIM, bg=BG,
             font=('Consolas', 8)).pack(side='left', pady=(6,0))
    # Bouton épingle (mode always-on) + bouton close en haut à droite
    pin_var = {'on': bool(_settings.get('hud_always_on'))}
    def _pin_label(): return '📌' if pin_var['on'] else '📍'
    pin_lbl = tk.Label(header, text=_pin_label(), fg=A1 if pin_var['on'] else DIM,
                       bg=BG, font=('Consolas', 12), cursor='hand2')
    pin_lbl.pack(side='right', padx=(0, 4))
    close_lbl = tk.Label(header, text='×', fg=DIM, bg=BG,
                         font=('Consolas', 14, 'bold'), cursor='hand2')
    close_lbl.pack(side='right', padx=(0, 6))

    sep = tk.Frame(inner, bg=A2, height=1)
    sep.pack(fill='x', padx=16, pady=(0, 8))

    body = tk.Label(inner, text='(démarrage…)', fg=TXT, bg=BG,
                    font=('Consolas', 10), justify='left', anchor='w')
    body.pack(fill='x', padx=16, pady=(0, 12))

    footer = tk.Label(inner, text='glisser pour déplacer · 📌 = toujours visible · × pour fermer',
                      fg=DIM, bg=BG, font=('Consolas', 7))
    footer.pack(pady=(0, 8))

    _visible = {'v': False}
    _drag    = {'x': 0, 'y': 0, 'moved': False}

    def _reposition():
        root.update_idletasks()
        w  = root.winfo_reqwidth()
        h  = root.winfo_reqheight()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        # Position sauvegardée si dispo, sinon coin bas-droite
        sx = _settings.get('hud_x')
        sy = _settings.get('hud_y')
        if isinstance(sx, int) and isinstance(sy, int):
            x = max(0, min(sw - w, sx))
            y = max(0, min(sh - h, sy))
        else:
            x = sw - w - 16
            y = sh - h - 60
        root.geometry(f'{w}x{h}+{x}+{y}')

    def _show():
        with _lock:
            stats = _state.get('stats') or {}
        body.config(text=_hud_text(stats))
        _reposition()
        root.deiconify()
        try: root.lift(); root.focus_force()
        except Exception: pass
        _visible['v'] = True

    def _hide():
        root.withdraw()
        _visible['v'] = False

    def _toggle_pin(_ev=None):
        pin_var['on'] = not pin_var['on']
        _settings['hud_always_on'] = pin_var['on']
        _save_settings(_settings)
        pin_lbl.config(text=_pin_label(), fg=A1 if pin_var['on'] else DIM)

    def _drag_start(e):
        _drag['x'] = e.x_root - root.winfo_x()
        _drag['y'] = e.y_root - root.winfo_y()
        _drag['moved'] = False

    def _drag_move(e):
        nx = e.x_root - _drag['x']
        ny = e.y_root - _drag['y']
        root.geometry(f'+{nx}+{ny}')
        _drag['moved'] = True

    def _drag_end(_e):
        if _drag['moved']:
            _settings['hud_x'] = root.winfo_x()
            _settings['hud_y'] = root.winfo_y()
            _save_settings(_settings)

    # Drag depuis le header/body (mais PAS depuis les boutons pin/close)
    for w in (header, body, footer, sep, outer, inner):
        w.bind('<ButtonPress-1>',   _drag_start)
        w.bind('<B1-Motion>',       _drag_move)
        w.bind('<ButtonRelease-1>', _drag_end)

    close_lbl.bind('<Button-1>', lambda e: _hide())
    pin_lbl.bind('<Button-1>',   _toggle_pin)

    def _refresh():
        # Rafraîchit le contenu uniquement quand visible (économise CPU)
        if _visible['v']:
            with _lock:
                stats = _state.get('stats') or {}
            body.config(text=_hud_text(stats))
        root.after(500, _refresh)

    def _poll_queue():
        try:
            while True:
                cmd = _hud_queue.get_nowait()
                if cmd == 'toggle':
                    (_hide if _visible['v'] else _show)()
                elif cmd == 'pin_toggle':
                    _toggle_pin()
                    # Si on active pin depuis le menu tray, ouvrir aussi
                    if pin_var['on'] and not _visible['v']:
                        _show()
                elif cmd == 'close':
                    _hide()
                    root.quit()
                    return
        except queue.Empty:
            pass
        root.after(50, _poll_queue)

    # Auto-show si mode always-on activé au démarrage
    if pin_var['on']:
        root.after(100, _show)

    root.after(50,  _poll_queue)
    root.after(500, _refresh)
    root.mainloop()


def _systray_run(url, srv):
    """Lance l'icône systray. Bloque jusqu'à Quit. Doit tourner dans le thread principal.
       Comportement : clic gauche → toggle popup HUD néon,
                      clic droit → menu avec les options,
                      hover → tooltip natif Windows compact."""
    _running = {'v': True}

    def _open(icon=None, item=None):
        webbrowser.open(url)

    def _hud_toggle(icon=None, item=None):
        _hud_queue.put('toggle')

    def _preset(key):
        def _fn(icon=None, item=None):
            try: _apply_preset(key)
            except Exception as e: print(f'[systray-preset] {e}')
        return _fn

    def _sysact(action):
        def _fn(icon=None, item=None):
            try: _system_action(action)
            except Exception as e: print(f'[systray-action] {e}')
        return _fn

    def _quit(icon=None, item=None):
        _running['v'] = False
        _hud_queue.put('close')
        try: icon.stop()
        except Exception: pass
        try: srv.shutdown()
        except Exception: pass

    def _hud_pin_toggle(icon=None, item=None):
        _hud_queue.put('pin_toggle')

    # Item par défaut invisible : déclenché au clic gauche sur l'icône
    menu = pystray.Menu(
        pystray.MenuItem('HUD (toggle)', _hud_toggle, default=True, visible=False),
        pystray.MenuItem('Ouvrir SINDRI', _open),
        pystray.MenuItem('HUD toujours visible', _hud_pin_toggle,
                         checked=lambda item: bool(_settings.get('hud_always_on'))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Preset : GAMING',  _preset('gaming')),
        pystray.MenuItem('Preset : OFFICE',  _preset('office')),
        pystray.MenuItem('Preset : SILENCE', _preset('silence')),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Verrouiller la session', _sysact('lock')),
        pystray.MenuItem('Mettre en veille',       _sysact('sleep')),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quitter', _quit),
    )
    icon = pystray.Icon('sindri', _make_tray_icon(0), 'SINDRI\n(démarrage…)', menu)

    # Thread HUD popup (Tk mainloop, se déclenche au clic gauche via _hud_queue)
    threading.Thread(target=_hud_run, daemon=True).start()

    def _refresh():
        last_pct = -1
        while _running['v']:
            try:
                with _lock:
                    stats = _state.get('stats') or {}
                pct = int(round((stats.get('cpu') or {}).get('pct') or 0))
                if pct != last_pct:
                    icon.icon = _make_tray_icon(pct)
                    last_pct  = pct
                # HUD tooltip : rafraîchi à chaque tick (les autres métriques bougent aussi)
                icon.title = _tray_tooltip(stats)
            except Exception:
                pass
            time.sleep(1.0)

    threading.Thread(target=_refresh, daemon=True).start()
    icon.run()   # bloque


# ── Main ───────────────────────────────────────────────────────────────────────

def _check_single_instance():
    """Refuse de démarrer si le port PORT est déjà pris (autre instance SINDRI active)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(('127.0.0.1', PORT))
        s.close()
        # Connexion réussie = SINDRI tourne déjà
        print(f'⚠ SINDRI tourne déjà sur le port {PORT} (http://localhost:{PORT})')
        print(f'   Ferme-le d\'abord ou utilise-le, puis relance.')
        print(f'   Pour tuer toutes les instances : taskkill /F /IM python.exe')
        return False
    except Exception:
        return True   # port libre → OK

if __name__ == '__main__':
    print('⚡ SINDRI v3 — Energy • Thermal • Systems — Démarrage...')

    # Anti double-lancement
    if not _check_single_instance():
        time.sleep(3)
        sys.exit(1)

    # Charge LibreHardwareMonitorLib.dll directement (pas de GUI, pas de web server)
    _init_lhm_lib()

    # Scan des contrôles fan disponibles (superIO, GPU, chipset)
    try:
        _lhm_scan_controls()
        n_ctrl = len(_lhm_controls)
        if n_ctrl:
            print(f'✓ Fan control : {n_ctrl} canal(aux) PWM détecté(s)')
            for cid, m in _lhm_controls.items():
                print(f'    → {cid}  [{m["min"]:.0f}-{m["max"]:.0f}%]')
        else:
            print('⚠ Fan control : aucun canal PWM contrôlable (chipset non supporté ou fans en DC only)')
    except Exception as e:
        print(f'[fan-ctrl-scan] {e}')

    # Nettoyage historique ancien (garde HISTORY_MAX_DAYS jours)
    _cleanup_history()

    threading.Thread(target=_loop,      daemon=True).start()
    threading.Thread(target=_ping_loop, daemon=True).start()

    time.sleep(0.8)

    url  = f'http://localhost:{PORT}'
    # Bind sur 0.0.0.0 si l'accès LAN est activé, sinon localhost uniquement
    bind = '0.0.0.0' if _settings.get('lan_access') else '127.0.0.1'
    srv  = HTTPServer((bind, PORT), Handler)
    print(f'✓ SINDRI    : {url}')
    if bind == '0.0.0.0':
        lan = _local_ip()
        print(f'✓ LAN       : http://{lan}:{PORT}  (accessible depuis téléphone / autres PC)')
        if _settings.get('pin_hash'):
            print(f'✓ PIN       : ACTIF (les actions destructives sont protégées)')
        else:
            print(f'⚠ PIN       : DÉSACTIVÉ — configure un PIN via ⚙ Settings')
    print('  Ctrl+C pour arrêter')
    print()
    print('  Nouveautés v2:')
    print('  → Alertes sonores (CPU/GPU temp + charge)')
    print('  → 3 thèmes: NEON / MATRIX / FIRE')
    print('  → HUD gaming (touche G ou bouton HUD)')
    print('  → Export CSV historique 5 min')
    print('  → Ping réseau temps réel')
    print('  → Ventilateurs RPM + puissance CPU (si LHM actif)')
    print('  → NVMe temp (si smartctl installé)')

    if _SYSTRAY_OK:
        # Serveur HTTP en arrière-plan, icône systray au premier plan (bloque le thread principal)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print('✓ Systray   : icône CPU active à côté de l\'horloge (clic droit → menu)')
        try:
            _systray_run(url, srv)
        except KeyboardInterrupt:
            print('\n✓ Arrêté.')
            srv.shutdown()
    else:
        # Fallback : pas de pystray/Pillow → ancien comportement (ouvre le navigateur)
        print('⚠ Systray   : pystray/Pillow non installés → ouverture navigateur à la place')
        print('              (pip install pystray Pillow)')
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print('\n✓ Arrêté.')
            srv.shutdown()
