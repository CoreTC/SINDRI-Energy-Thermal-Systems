# SINDRI — Energy • Thermal • Systems

Dashboard web local **mono-fichier Python** pour monitorer, contrôler et **soigner** ton PC Windows en temps réel.
Style néon cyberpunk, contrôle direct des ventilateurs et du GPU, coût électrique persistant, auto-heal "eau froide sur la Forge".

> Un seul fichier `pulse.py` (~280 KB) qui embarque serveur HTTP + HTML/CSS/JS + icône systray + HUD flottant Tk.
> Aucun framework externe (pas Flask, pas Electron).

---

## Aperçu

```
⚡ SINDRI     Energy · Thermal · Systems     v3
────────────────────────────────────────────────
CPU 63%  81°C  128W    GPU 82%  71°C  247W
RAM 58%                POWER  521 W (mur)
────────────────────────────────────────────────
✚ HEAL: ON     💧 GPU limité à 231W · 47s
```

- **Jauges 270°** style ROG, animations néon (6 thèmes : NEON / MATRIX / FIRE / ICE / GOLD / VOID)
- **HUD flottant Tk** always-on-top draggable + icône systray qui bat au rythme du CPU
- **Historique persistant** 30 jours (JSONL, sous-échantillonné 500 pts par requête)
- **Localhost:7890** — accès LAN optionnel + PIN 4-8 chiffres sur actions destructives

## Fonctionnalités

### 📊 Monitoring
- CPU, GPU (NVIDIA), RAM, cœurs individuels, températures LHM
- Disques (I/O + SMART), réseau (bande passante + processus par connexions)
- Historique persistant JSONL (30 jours, sous-échantillonnage 500 pts)
- Détection **thermal throttle** CPU (badge ⚠ THROTTLE)
- Infos système : modèle CPU, VBIOS/driver GPU, BIOS + carte mère, écrans

### ⚡ Énergie
- Conso murale « certifiée » : CPU/GPU mesurés + estimation mobo/RAM/SSD/HDD/fans/écrans
- Écrans détectés automatiquement (résolution + Hz) ou override manuel
- **Coût électrique persistant** : jour / mois / année (rollover automatique)

### 🔧 Contrôle
- Presets FORGE : **GAMING** / **OFFICE** / **SILENCE** (powercfg + GPU power limit)
- Slider GPU Power Limit direct (`nvidia-smi -pl`)
- **Contrôle ventilateurs PWM** par canal + courbes automatiques (temp → PWM)
- **Reboot vers UEFI** en 1 clic (`shutdown /r /fw`)

### 💧 HEAL "Eau froide sur la Forge"
Bouton toggle ON/OFF + règles cochables dans les paramètres.

Quand une règle se déclenche (condition soutenue 15s), l'action **fait chuter les stats de manière visible** :

| Cible | Action | Effet visible |
|-------|--------|---------------|
| CPU   | `PROCTHROTTLEMAX=70` (cap boost) + stop WSearch/SysMain | Temp ↓, power ↓ (5-10s) |
| RAM   | Purge kernel **standby list** + trim working sets | RAM % ↓ (immédiat) |
| GPU   | Power limit × 0.7 | Temp ↓, power ↓ (3-5s) |
| Disk  | Vide TEMP + Prefetch + thumbnails + Corbeille + TRIM | Espace libre ↑ |
| Net   | Flush DNS + cache ARP + register DNS | Latence DNS ↓ |

**Auto-revert 60 s** : les brakes CPU/GPU sont temporaires, ton PC n'est pas bridé à vie.
Badge « 💧 EAU FROIDE » + gouttes animées sur la carte pendant l'effet.

### 🚨 Alertes
- **Bips audio** sur transitions warn/crit (choix UX délibéré, à conserver)
- Toast Windows natif + webhook Discord/Telegram (durée soutenue configurable)
- HUD flottant, icône systray, badge THROTTLE

---

## Installation (Windows 10/11)

**Prérequis** : Windows 10+ x64, admin (pour lire les MSR et piloter les fans)

```powershell
# 1. Clone
git clone https://github.com/<TON_USER>/SINDRI.git
cd SINDRI

# 2. Install auto (télécharge Python + .NET + LHM + pip deps)
Install.bat

# 3. Lancer (auto-élevé UAC)
"Lancer SINDRI.bat"
```

L'installeur :
1. Vérifie `winget`
2. Installe Python 3.13 si absent
3. Installe .NET Desktop Runtime 10 (fallback 8)
4. `pip install -r requirements.txt`
5. Télécharge `LibreHardwareMonitorLib.dll` (release officielle)
6. Crée un raccourci Bureau

Ouvre ensuite [http://localhost:7890](http://localhost:7890).

### Installation manuelle

```bash
py -m pip install -r requirements.txt
powershell -File Download-LHM.ps1
py pulse.py
```

## Compatibilité matérielle

**Auto-détecté à l'installation** :
- CPU model + TDP (LHM + regex Intel/AMD ; fallback 95 W)
- RAM totale, nombre de SSD NVMe / SATA / HDD (via `Get-PhysicalDisk`)
- Modèle GPU, VBIOS, driver, temp, load, power (**NVIDIA uniquement** via `nvidia-smi`)
- Températures, ventilateurs, tensions (LHM auto — chipsets **Nuvoton / ITE / Fintek** couverts)
- BIOS, carte mère, écrans détectés (WMI)

**Limites connues** :
- **NVIDIA seul** pour la carte GPU (AMD/Intel Arc = pas de données GPU, autres cartes fonctionnent quand même)
- **Windows 10/11** uniquement (utilise WMI, PowerShell, nvidia-smi, powercfg, MSR)
- **Fan control** dépend du chipset Super I/O et du BIOS (~60 % des cartes desktop). Si les fans sont bloqués en mode "Auto" par le BIOS, il faut les passer en "PWM Manual" dans le BIOS.
- **Purge standby list RAM** utilise `SeProfileSingleProcessPrivilege` (Windows uniquement, requiert admin).

Modifie ta config hardware (nombre exact de disques, PSU efficacité, RAM, ventilos, écrans) via le bouton **⚙** de la carte CONSOMMATION MURALE — les défauts sont conservateurs.

## Dépendances

- **Python 3.10+**
- `psutil` — CPU/RAM/disk/net/process
- `pythonnet` — charge LibreHardwareMonitorLib.dll (nécessite .NET Runtime 8+)
- `pystray` + `Pillow` — icône systray + rendu de la jauge dessinée
- **LibreHardwareMonitor** (DLL) — capteurs hardware bas niveau, contrôle fans PWM ([site officiel](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor))
- **nvidia-smi** (fourni avec le driver NVIDIA) — pour GPU
- **smartmontools** *(optionnel)* — pour rapports SMART : `winget install smartmontools.smartmontools`

## Architecture

```
pulse.py                Le programme complet (HTML/CSS/JS embedded, systray, HUD Tk)
Install.bat             Installeur tout-en-un (winget + pip + LHM)
Lancer SINDRI.bat       Launcher auto-élevé UAC
Download-LHM.ps1        Téléchargement LibreHardwareMonitor depuis release GitHub
requirements.txt        Dépendances Python
INSTALL.txt             Guide utilisateur
LibreHardwareMonitor/   DLL + deps (téléchargées, gitignored)
settings.json           Créé au runtime (gitignored)
history.jsonl           Créé au runtime (gitignored)
```

**Mono-fichier délibéré** : distribution simple (clone + run), pas de packaging complexe.
HTML/CSS/JS restent embedded en raw string Python.

## Endpoints API

| Méthode | Route | Usage |
|---------|-------|-------|
| GET     | `/`, `/api/stats`, `/api/settings`, `/api/history?minutes=N`, `/api/autostart`, `/api/smart` | Données |
| POST    | `/api/settings`, `/api/preset`, `/api/gpu`, `/api/action`, `/api/kill`, `/api/set_pin` | Contrôle |
| POST    | `/api/heal {target}`, `/api/fan/set`, `/api/fan/default`, `/api/fan/curve`, `/api/fan/rescan` | HEAL + Fans |
| POST    | `/api/net/kill_conns {pid}`, `/api/toast_test`, `/api/webhook_test` | Divers |

## Sécurité

- Bind par défaut `127.0.0.1` (localhost uniquement)
- Accès LAN opt-in dans les paramètres (`bind 0.0.0.0`)
- **PIN 4-8 chiffres SHA256** sur toutes les actions destructives (kill, restart, shutdown, reboot UEFI, LAN toggle, presets, HEAL, kill connexions)
- **Blacklist processus critiques** : refuse de kill `System`, `csrss`, `wininit`, `services`, `lsass`, etc.
- Anti double-lancement (détection port 7890 déjà pris)

## Licence

MIT — voir [LICENSE](LICENSE).

LibreHardwareMonitor est [MPL-2.0](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/main/LICENSE), téléchargé séparément.

---

*Historique des noms : « PC Dashboard » → PULSE (juillet 2026) → **SINDRI** (Energy • Thermal • Systems).*
