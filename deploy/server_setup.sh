#!/usr/bin/env bash
# One-time VPS bootstrap for HireLoop on Oracle Cloud Ubuntu 22.04 ARM64.
# Idempotent — safe to re-run if interrupted.
set -euo pipefail

# ── Config — edit before running ──────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/HarshitOO7/HireLoop.git}"
BLOCK_DEVICE="${BLOCK_DEVICE:-/dev/sdb}"
# ──────────────────────────────────────────────────────────────────────────────

APP_DIR="/opt/hireloop/app"
DATA_DIR="/opt/hireloop/data"
OUTPUT_DIR="/opt/hireloop/output"
MOUNT_POINT="/mnt/blockdata"

log()  { echo "[$(date +'%H:%M:%S')] $*"; }
die()  { log "ERROR: $*" >&2; exit 1; }
done_() { log "  ✓ $*"; }

# ── [1/7] Docker ───────────────────────────────────────────────────────────────
log "=== [1/7] Docker ==="
if command -v docker &>/dev/null; then
    done_ "Docker already installed ($(docker --version | cut -d' ' -f3 | tr -d ','))"
else
    curl -fsSL https://get.docker.com | sudo bash
    sudo apt-get install -y docker-compose-plugin git
    done_ "Docker installed"
fi

if ! docker compose version &>/dev/null; then
    sudo apt-get install -y docker-compose-plugin
fi

# Add current user to docker group (takes effect on next login)
if ! groups "$USER" | grep -q docker; then
    sudo usermod -aG docker "$USER"
    log "  Added $USER to docker group — will take effect after re-login"
fi

docker compose version &>/dev/null || die "docker compose plugin not available"
done_ "docker compose ready"

# ── [2/7] Block storage ────────────────────────────────────────────────────────
log "=== [2/7] Block storage ==="
if mountpoint -q "$MOUNT_POINT"; then
    done_ "Already mounted at $MOUNT_POINT"
else
    [[ -b "$BLOCK_DEVICE" ]] || die "$BLOCK_DEVICE not found — check: lsblk"

    if ! sudo blkid "$BLOCK_DEVICE" &>/dev/null; then
        log "  Formatting $BLOCK_DEVICE..."
        sudo mkfs.ext4 -F "$BLOCK_DEVICE"
    fi

    sudo mkdir -p "$MOUNT_POINT"

    if ! grep -q "^$BLOCK_DEVICE" /etc/fstab; then
        echo "$BLOCK_DEVICE $MOUNT_POINT ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2" \
            | sudo tee -a /etc/fstab
    fi

    sudo mount -a
    done_ "Mounted $BLOCK_DEVICE at $MOUNT_POINT"
fi

# ── [3/7] Directory structure ──────────────────────────────────────────────────
log "=== [3/7] Directories ==="
sudo mkdir -p "$MOUNT_POINT/data" "$MOUNT_POINT/output"
sudo mkdir -p /opt/hireloop
sudo ln -sfn "$MOUNT_POINT/data"   "$DATA_DIR"
sudo ln -sfn "$MOUNT_POINT/output" "$OUTPUT_DIR"
sudo chmod 755 "$MOUNT_POINT/data" "$MOUNT_POINT/output"
done_ "/opt/hireloop/data → $MOUNT_POINT/data"
done_ "/opt/hireloop/output → $MOUNT_POINT/output"

# ── [4/7] Clone repo ───────────────────────────────────────────────────────────
log "=== [4/7] Clone repo ==="
if [[ -d "$APP_DIR/.git" ]]; then
    done_ "Repo already cloned at $APP_DIR"
else
    sudo mkdir -p "$APP_DIR"
    sudo chown "$USER:$USER" "$APP_DIR"
    git clone "$REPO_URL" "$APP_DIR"
    done_ "Cloned to $APP_DIR"
fi

# ── [5/7] .env ─────────────────────────────────────────────────────────────────
log "=== [5/7] .env ==="
if [[ -f "$APP_DIR/.env" ]]; then
    done_ ".env already exists — skipping"
else
    echo ""
    echo ">>> Paste your full .env content now (Ctrl+D when done):"
    cat > "$APP_DIR/.env"
    grep -q "DATABASE_URL" "$APP_DIR/.env" \
        || log "  WARNING: DATABASE_URL not found in .env"
    grep -q "sqlite:///data/" "$APP_DIR/.env" \
        || log "  WARNING: DATABASE_URL should be sqlite:///data/hireloop.db"
    done_ ".env saved"
fi

# ── [6/7] Migrate + start ──────────────────────────────────────────────────────
log "=== [6/7] Migrate + start ==="
cd "$APP_DIR"
sudo docker compose run --rm --no-deps bot bash -c "alembic upgrade head"
sudo docker compose up -d
done_ "Bot started"

# ── [7/7] systemd auto-boot ────────────────────────────────────────────────────
log "=== [7/7] systemd ==="
sudo cp "$APP_DIR/deploy/hireloop.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hireloop
done_ "Auto-boot enabled (sudo systemctl status hireloop)"

echo ""
echo "════════════════════════════════════════"
echo "  DONE"
echo "  Logs:  docker compose logs -f bot"
echo "  DB:    $MOUNT_POINT/data/hireloop.db"
echo "  Boot:  sudo systemctl status hireloop"
echo "════════════════════════════════════════"
