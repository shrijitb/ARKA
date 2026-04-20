#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
# Arca Installer — Detects hardware, configures, and launches
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/shrijitb/ARCA/main/install.sh | bash
#
# Or locally:
#   bash ~/mara/install.sh
# ──────────────────────────────────────────────────────────────────
set -euo pipefail

ARCA_DIR="${ARCA_DIR:-$HOME/arca}"
ARCA_BRANCH="main"
ARCA_REPO="https://github.com/shrijitb/ARCA.git"

echo "═══════════════════════════════════════════════════"
echo "  ARCA — Agentic Risk-Kinetic Allocator            "
echo "  One-line installer starting...                   "
echo "═══════════════════════════════════════════════════"
echo ""

# ── Helpers ───────────────────────────────────────────────────────

step() { echo ""; echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
die()  { echo ""; echo "✗ ERROR: $*" >&2; exit 1; }

# ── Phase 1: Dependency Check ─────────────────────────────────────

step "Checking dependencies"

if ! command -v docker &>/dev/null; then
    warn "Docker not found. Attempting install..."
    if [ "$(uname -s)" = "Linux" ]; then
        curl -fsSL https://get.docker.com | sh
        sudo usermod -aG docker "$USER" 2>/dev/null || true
        warn "Docker group added. Using 'sg docker' / sudo for this session — no re-login needed."
    else
        die "Please install Docker Desktop: https://www.docker.com/products/docker-desktop/"
    fi
fi

docker compose version &>/dev/null || die "docker compose v2 not found. Install Docker Engine 20.10+ with the compose plugin."
command -v git   &>/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq git; }

# jq is used for reading the hardware profile
if ! command -v jq &>/dev/null; then
    warn "jq not found. Attempting install..."
    if [ "$(uname -s)" = "Linux" ]; then
        sudo apt-get update -qq && sudo apt-get install -y -qq jq 2>/dev/null || \
        sudo yum install -y jq 2>/dev/null || \
        warn "Could not install jq automatically — hardware profile parsing may be limited"
    fi
fi

ok "All dependencies satisfied"

# ── Phase 2: Clone or Update ──────────────────────────────────────

step "Setting up Arca in $ARCA_DIR"

mkdir -p "$ARCA_DIR"

if [ -d "$ARCA_DIR/.git" ]; then
    ok "Existing installation found. Pulling latest from $ARCA_BRANCH..."
    cd "$ARCA_DIR"
    git fetch origin
    git checkout "$ARCA_BRANCH" 2>/dev/null || true
    git pull origin "$ARCA_BRANCH"
else
    git clone -b "$ARCA_BRANCH" "$ARCA_REPO" "$ARCA_DIR"
    cd "$ARCA_DIR"
    ok "Cloned from $ARCA_REPO"
fi

# ── Phase 3: Hardware Detection ───────────────────────────────────

step "Detecting hardware"

detect_hardware() {
    local os_name arch_label board cpu_model cpu_cores ram_mb gpu disk_free_gb os_id os_version

    arch_label="unknown"
    case "$(uname -m)" in
        x86_64|amd64)  arch_label="x86_64" ;;
        aarch64|arm64) arch_label="arm64"  ;;
        armv7l)        arch_label="armv7"  ;;
    esac

    os_name="$(uname -s)"
    os_id="unknown"; os_version="unknown"
    if [ -f /etc/os-release ]; then
        os_id=$(. /etc/os-release && echo "$ID")
        os_version=$(. /etc/os-release && echo "$VERSION_ID")
    elif [ "$os_name" = "Darwin" ]; then
        os_id="macos"
        os_version=$(sw_vers -productVersion 2>/dev/null || echo "unknown")
    fi

    board="generic"
    if [ -f /proc/device-tree/model ]; then
        local model
        model=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\0')
        case "$model" in
            *"Raspberry Pi 5"*)   board="rpi5"     ;;
            *"Raspberry Pi 4"*)   board="rpi4"     ;;
            *"Raspberry Pi"*)     board="rpi_other" ;;
            *"NVIDIA Jetson"*)    board="jetson"   ;;
            *)                    board="sbc_other" ;;
        esac
    fi

    ram_mb=0
    if [ -f /proc/meminfo ]; then
        ram_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
    elif [ "$os_name" = "Darwin" ]; then
        ram_mb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
    fi

    gpu="none"
    command -v nvidia-smi &>/dev/null && gpu="nvidia"
    [ "$gpu" = "none" ] && [ -d /dev/dri ] && ls /dev/dri/renderD* &>/dev/null 2>&1 && gpu="integrated"

    cpu_model="unknown"
    if [ -f /proc/cpuinfo ]; then
        cpu_model=$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | xargs 2>/dev/null || echo "")
        [ -z "$cpu_model" ] && cpu_model=$(grep -m1 "Hardware" /proc/cpuinfo | cut -d: -f2 | xargs 2>/dev/null || echo "unknown")
    elif [ "$os_name" = "Darwin" ]; then
        cpu_model=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "Apple Silicon")
    fi

    cpu_cores=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1)
    disk_free_gb=$(df -BG "$HOME" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}' || echo 0)

    cat > "$ARCA_DIR/.hardware_profile.json" << HWEOF
{
    "arch":         "$arch_label",
    "os":           "$os_id",
    "os_version":   "$os_version",
    "board":        "$board",
    "cpu_model":    "$cpu_model",
    "cpu_cores":    $cpu_cores,
    "ram_mb":       $ram_mb,
    "gpu":          "$gpu",
    "disk_free_gb": $disk_free_gb,
    "detected_at":  "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
HWEOF

    echo "  Board:  $board ($arch_label)"
    echo "  CPU:    $cpu_model ($cpu_cores cores)"
    echo "  RAM:    ${ram_mb}MB"
    echo "  GPU:    $gpu"
    echo "  Disk:   ${disk_free_gb}GB free"
    echo "  OS:     $os_id $os_version"
}

detect_hardware

# ── Phase 4: Profile Selection ────────────────────────────────────

step "Selecting hardware profile"

select_profile() {
    local ram_mb="$1" board="$2" arch="$3"

    if   [ "$board" = "rpi5" ] && [ "$ram_mb" -ge 8000 ]; then echo "pi5_full"
    elif [ "$board" = "rpi5" ];                            then echo "pi5_lite"
    elif [ "$board" = "rpi4" ];                            then echo "pi4"
    elif [ "$arch" = "arm64" ] && [ "$ram_mb" -ge 16000 ]; then echo "arm64_full"
    elif [ "$arch" = "x86_64" ] && [ "$ram_mb" -ge 16000 ]; then echo "x86_full"
    elif [ "$arch" = "x86_64" ] && [ "$ram_mb" -ge 8000 ]; then echo "x86_lite"
    else echo "minimal"
    fi
}

# Read values from the hardware profile (with jq if available, else awk)
if command -v jq &>/dev/null; then
    DETECTED_RAM=$(jq -r '.ram_mb'  "$ARCA_DIR/.hardware_profile.json")
    DETECTED_BOARD=$(jq -r '.board' "$ARCA_DIR/.hardware_profile.json")
    DETECTED_ARCH=$(jq -r '.arch'   "$ARCA_DIR/.hardware_profile.json")
else
    DETECTED_RAM=$(awk -F'"' '/"ram_mb"/{print $0}' "$ARCA_DIR/.hardware_profile.json" | grep -o '[0-9]*' | head -1)
    DETECTED_BOARD=$(awk -F'"' '/"board"/{print $4}' "$ARCA_DIR/.hardware_profile.json")
    DETECTED_ARCH=$(awk -F'"' '/"arch"/{print $4}' "$ARCA_DIR/.hardware_profile.json")
fi

PROFILE=$(select_profile "$DETECTED_RAM" "$DETECTED_BOARD" "$DETECTED_ARCH")
ok "Profile selected: $PROFILE"

# ── Phase 5: Configuration Generation ─────────────────────────────

step "Generating configuration"

# Profile → model / cycle / memory (using case statements for bash 3 compatibility)
case "$PROFILE" in
    pi5_full)   OLLAMA_MODEL="qwen2.5:3b";  CYCLE=90;  MEM_LIMIT="12g" ;;
    pi5_lite)   OLLAMA_MODEL="qwen2.5:1.5b"; CYCLE=120; MEM_LIMIT="6g"  ;;
    pi4)        OLLAMA_MODEL="qwen2.5:0.5b"; CYCLE=180; MEM_LIMIT="3g"  ;;
    arm64_full) OLLAMA_MODEL="qwen3:8b";    CYCLE=60;  MEM_LIMIT="12g" ;;
    x86_full)   OLLAMA_MODEL="qwen3:8b";    CYCLE=60;  MEM_LIMIT="12g" ;;
    x86_lite)   OLLAMA_MODEL="qwen2.5:3b";  CYCLE=60;  MEM_LIMIT="6g"  ;;
    minimal)    OLLAMA_MODEL="qwen2.5:0.5b"; CYCLE=120; MEM_LIMIT="4g"  ;;
esac

echo "  LLM model:      $OLLAMA_MODEL"
echo "  Cycle interval: ${CYCLE}s"
echo "  Memory limit:   $MEM_LIMIT"

# Only write .env if it doesn't already exist (don't clobber existing credentials)
if [ ! -f "$ARCA_DIR/.env" ]; then
    cat > "$ARCA_DIR/.env" << ENVEOF
# ── Arca Configuration (auto-generated by installer) ──
# Profile: $PROFILE  |  Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)

# ── Core ──
MARA_MODE=paper
MARA_LIVE=false
PAPER_TRADING=true
USE_LIVE_RATES=false
USE_LIVE_OHLCV=false
INITIAL_CAPITAL_USD=200.0
CYCLE_INTERVAL_SEC=$CYCLE
EXCHANGES=["okx"]
COMPOSE_PROJECT_NAME=arca

# ── Strategy ──
ACTIVE_STRATEGY=auto

# ── Ollama ──
OLLAMA_HOST=http://ollama:11434
OLLAMA_MODEL=$OLLAMA_MODEL

# ── OKX (set via dashboard wizard) ──
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=

# ── Data feeds (all optional — yfinance fallbacks exist) ──
FRED_API_KEY=
NASA_FIRMS_API_KEY=
UCDP_API_TOKEN=
AISSTREAM_API_KEY=
ACLED_EMAIL=
ACLED_PASSWORD=

# ── Prediction markets (Phase 3) ──
KALSHI_EMAIL=
KALSHI_PASSWORD=
POLY_PRIVATE_KEY=

# ── Notifications (all optional) ──
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_ID=
NTFY_TOPIC=

# ── Setup state (managed by dashboard wizard) ──
SETUP_COMPLETE=false
ENVEOF
    ok ".env created"
else
    ok ".env already exists — skipping (credentials preserved)"
fi

# Profile override for Docker resource limits
cat > "$ARCA_DIR/docker-compose.profile.yml" << DCEOF
# Auto-generated profile override: $PROFILE
# Do not edit — re-run install.sh to regenerate
services:
  ollama:
    deploy:
      resources:
        limits:
          memory: $MEM_LIMIT
    environment:
      - OLLAMA_NUM_PARALLEL=1
      - OLLAMA_MAX_LOADED_MODELS=1
DCEOF

ok "docker-compose.profile.yml written"

# ── Phase 6: Pull Ollama model ────────────────────────────────────

step "Pulling LLM model ($OLLAMA_MODEL) — this may take a few minutes on first run"

# Start ollama temporarily to pull the model if docker is available
# We'll pull it at runtime if it hasn't been pulled yet.
# The hypervisor startup will check and the dashboard will report model readiness.
ok "Model will be pulled automatically on first start"

# ── Phase 7: Build & Launch ───────────────────────────────────────

step "Building and starting Arca"

cd "$ARCA_DIR"

_run_compose() {
    docker compose \
        -f docker-compose.yml \
        -f docker-compose.profile.yml \
        up -d --build
}

# If user was just added to the docker group this session, group may not be
# active yet. Use `sg docker` to execute in a subshell with the group applied.
if id -nG "$USER" 2>/dev/null | grep -qw docker; then
    _run_compose
elif command -v sg &>/dev/null; then
    sg docker -c "cd '$ARCA_DIR' && docker compose -f docker-compose.yml -f docker-compose.profile.yml up -d --build" || \
        sudo docker compose -f docker-compose.yml -f docker-compose.profile.yml up -d --build
else
    sudo docker compose \
        -f docker-compose.yml \
        -f docker-compose.profile.yml \
        up -d --build
fi

# ── Phase 8: Health Wait ──────────────────────────────────────────

step "Waiting for Arca to become healthy"

WAIT_SECS=120
WAITED=0
while [ "$WAITED" -lt "$WAIT_SECS" ]; do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        ok "Hypervisor is healthy"
        break
    fi
    printf "."
    sleep 3
    WAITED=$((WAITED + 3))
done
echo ""

if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    warn "Hypervisor did not respond within ${WAIT_SECS}s."
    warn "Check logs: docker compose logs hypervisor"
fi

# ── Phase 9: Setup CLI ────────────────────────────────────────────

step "Setting up Arca CLI"

# Copy the arca-cli to a location in PATH
CLI_SRC="$ARCA_DIR/arca-cli"
CLI_DEST="/usr/local/bin/arca"

if [ -f "$CLI_SRC" ]; then
    if [ -w "/usr/local/bin" ] || [ "$(id -u)" -eq 0 ]; then
        cp "$CLI_SRC" "$CLI_DEST" 2>/dev/null && chmod +x "$CLI_DEST" && ok "CLI installed to $CLI_DEST" || warn "Could not install CLI to $CLI_DEST (permission denied)"
    else
        warn "Cannot install to $CLI_DEST (permission denied)."
        warn "To install CLI manually:"
        warn "  sudo cp $CLI_SRC /usr/local/bin/arca && sudo chmod +x /usr/local/bin/arca"
        warn "Or add $ARCA_DIR to your PATH:"
        warn "  echo 'export PATH=\$PATH:$ARCA_DIR' >> ~/.bashrc"
    fi
else
    warn "arca-cli not found at $CLI_SRC"
fi

# ── Phase 10: Launch Prompt ───────────────────────────────────────

# Dashboard URL
DASHBOARD_URL="http://localhost:3000"

# Function to detect OS and open URL
open_dashboard() {
    local os_name
    os_name=$(uname -s)
    
    case "$os_name" in
        Darwin)
            open "$DASHBOARD_URL" 2>/dev/null || true
            ;;
        Linux)
            # Try common Linux browsers/openers
            if command -v xdg-open &>/dev/null; then
                xdg-open "$DASHBOARD_URL" 2>/dev/null || true
            elif command -v gnome-open &>/dev/null; then
                gnome-open "$DASHBOARD_URL" 2>/dev/null || true
            elif command -v kde-open &>/dev/null; then
                kde-open "$DASHBOARD_URL" 2>/dev/null || true
            elif command -v chromium-browser &>/dev/null; then
                chromium-browser "$DASHBOARD_URL" &>/dev/null &
            elif command -v google-chrome &>/dev/null; then
                google-chrome "$DASHBOARD_URL" &>/dev/null &
            elif command -v firefox &>/dev/null; then
                firefox "$DASHBOARD_URL" &>/dev/null &
            fi
            ;;
        MINGW*|CYGWIN*|MSYS*)
            start "$DASHBOARD_URL" 2>/dev/null || true
            ;;
    esac
}

# Function to show GUI dialog and launch
show_launch_dialog() {
    local os_name
    os_name=$(uname -s)
    local response=""
    
    case "$os_name" in
        Darwin)
            # Use AppleScript for macOS
            response=$(osascript -e '
                display dialog "Arca installation is complete!\n\nWould you like to open the dashboard now?" \
                    buttons {"Later", "Launch Now"} \
                    default button "Launch Now" \
                    with icon note
                button returned of result
            ' 2>/dev/null || echo "")
            if [ "$response" = "Launch Now" ]; then
                open_dashboard
            fi
            ;;
        Linux)
            # Try zenity for GUI dialog
            if command -v zenity &>/dev/null; then
                if zenity --question --title="Arca Setup" --text="Arca installation is complete!\n\nWould you like to open the dashboard now?" 2>/dev/null; then
                    open_dashboard
                fi
            # Try kdialog for KDE
            elif command -v kdialog &>/dev/null; then
                if kdialog --yesno "Arca installation is complete!\n\nWould you like to open the dashboard now?" --title "Arca Setup" 2>/dev/null; then
                    open_dashboard
                fi
            else
                # Fallback to terminal prompt
                echo ""
                echo "🚀 Arca is running!"
                echo ""
                read -p "📺 Open dashboard now? [Y/n] " -n 1 -r
                echo ""
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    open_dashboard
                fi
            fi
            ;;
        MINGW*|CYGWIN*|MSYS*)
            # Use PowerShell for Windows
            powershell.exe -Command "
                \$result = MessageBox.Show('Arca installation is complete!\n\nWould you like to open the dashboard now?', 'Arca Setup', 'YesNo', 'Question')
                if (\$result -eq 'Yes') { Start-Process 'http://localhost:3000' }
            " 2>/dev/null || echo ""
            ;;
        *)
            # Fallback for unknown OS
            echo ""
            echo "🚀 Arca is running!"
            echo ""
            read -p "📺 Open dashboard now? [Y/n] " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                open_dashboard
            fi
            ;;
    esac
}

# ── Done ──────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Arca is running!                                 "
echo ""
echo "  Dashboard:   http://localhost:3000               "
echo "  API:         http://localhost:8000/status        "
echo ""
echo "  Commands:                                        "
echo "    arca launch    - Open dashboard popup          "
echo "    arca status    - Check system status           "
echo "    arca logs      - View service logs             "
echo "    arca stop      - Stop all services              "
echo ""
echo "  The dashboard will guide you through setup.      "
echo "═══════════════════════════════════════════════════"

# Show launch dialog (GUI or terminal-based)
show_launch_dialog
