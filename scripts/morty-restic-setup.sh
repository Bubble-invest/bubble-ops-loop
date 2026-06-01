#!/usr/bin/env bash
# =============================================================================
# morty-restic-setup.sh — Security/backup sprint, deliverable B.
#
# Provisionne Restic sur Morty (one-shot, idempotent) :
#   1. Installe `restic` via apt (idempotent — apt install no-op si déjà)
#   2. Crée /var/backups/bubble-restic/ (700 root) et /var/cache/restic
#   3. Demande la passphrase Restic à Joris (saisie cachée) et l'écrit
#      dans /etc/bubble/restic-password (mode 400 root)
#   4. `restic init` si le repo n'existe pas encore (sinon skip)
#   5. Installe :
#        bubble-restic-backup.service + .timer  (toutes les 6h)
#        bubble-restic-forget.service + .timer  (rétention 24h/7j/4sem, 1x/jour)
#   6. daemon-reload + enable + start des deux timers
#
# Idempotent : peut être re-lancé sans casse. Tout est `apt`, `mkdir -p`,
# `systemctl enable` (déjà fait = no-op), ou guardé par une existence-check.
#
# PHASE 1 = repo LOCAL (/var/backups/bubble-restic). Si Morty meurt, le
# backup meurt avec lui. C'est UNE ÉTAPE — la suivante est la migration
# vers Backblaze B2 (~1€/mois pour 20GB) ou Hetzner Storage Box (3.81€/mois
# pour 100GB). Cf. docs/BACKUP-STRATEGY.md, section « TODO off-site ».
#
# Usage :
#   bash scripts/morty-restic-setup.sh [--remote=user@host]
#
# Défaut --remote : hetzner (alias ~/.ssh/config).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$PROJECT_ROOT/deploy/templates"

usage() {
  cat <<'USAGE'
Usage : morty-restic-setup.sh [--remote=<user@host>] [--dry-run]

Synopsis :
  One-shot idempotent : installe restic sur Morty, crée le repo local
  /var/backups/bubble-restic, initialise avec une passphrase fournie par
  l'opérateur, installe les unités systemd bubble-restic-backup +
  bubble-restic-forget (toutes les 6h / rétention 24h-7j-4sem).

Arguments :
  --remote=<host>   Cible SSH. Défaut : hetzner.
  --dry-run         Affiche les commandes SSH sans les exécuter.
  --help            Affiche ce message.

PHASE 1 = REPO LOCAL. Migration off-site (B2 ou Storage Box) à faire dès
que Joris fournit les credentials. Cf. docs/BACKUP-STRATEGY.md.
USAGE
}

REMOTE="hetzner"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --remote=*) REMOTE="${arg#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: argument inconnu : $arg" >&2; usage >&2; exit 64 ;;
  esac
done

# Préflight : les templates sont là ?
for tpl in bubble-restic-backup.service bubble-restic-backup.timer \
           bubble-restic-forget.service bubble-restic-forget.timer; do
  if [[ ! -f "$TEMPLATE_DIR/${tpl}.template" ]]; then
    echo "ERROR: template manquant : $TEMPLATE_DIR/${tpl}.template" >&2
    exit 1
  fi
done

run_ssh() {
  local cmd="$1"
  if [[ "$DRY_RUN" = "1" ]]; then
    echo "[dry-run] ssh $REMOTE '$cmd'"
  else
    ssh "$REMOTE" "$cmd"
  fi
}

push_file() {
  local local_path="$1"
  local remote_path="$2"
  if [[ "$DRY_RUN" = "1" ]]; then
    echo "[dry-run] scp $local_path $REMOTE:/tmp/$(basename "$remote_path")"
    echo "[dry-run] ssh $REMOTE 'sudo install -m 644 -o root -g root /tmp/$(basename "$remote_path") $remote_path'"
  else
    scp "$local_path" "$REMOTE:/tmp/$(basename "$remote_path")" >/dev/null
    ssh "$REMOTE" "sudo install -m 644 -o root -g root /tmp/$(basename "$remote_path") $remote_path && rm -f /tmp/$(basename "$remote_path")"
  fi
}

echo "[morty-restic-setup] cible : $REMOTE"
echo "[morty-restic-setup] phase 1 — repo local /var/backups/bubble-restic"
echo ""

# --- Étape 1 : installer restic (idempotent — apt install no-op si déjà) ---
echo "[1/6] installation restic via apt (idempotent)..."
run_ssh "sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq restic"

# --- Étape 2 : créer les dossiers (mode 700 root) ---
echo "[2/6] création /var/backups/bubble-restic + /var/cache/restic..."
run_ssh "sudo install -d -m 700 -o root -g root /var/backups/bubble-restic /var/cache/restic /etc/bubble"

# --- Étape 3 : passphrase Restic ---
echo "[3/6] vérification de la passphrase Restic dans /etc/bubble/restic-password..."
PASSWORD_EXISTS=$(run_ssh "sudo test -s /etc/bubble/restic-password && echo yes || echo no" | tail -1)

if [[ "$PASSWORD_EXISTS" = "yes" ]]; then
  echo "  -> passphrase déjà présente sur Morty, on garde."
else
  if [[ "$DRY_RUN" = "1" ]]; then
    echo "[dry-run] (la passphrase serait récupérée depuis Keychain via bubble-get-keychain et écrite dans /etc/bubble/restic-password mode 400)"
  else
    # Refacto 2026-05-21 (msg 2823-2825) : passphrase passe par Keychain Mac
    # (skill auth Flow 3) au lieu d'une saisie terminale interactive.
    # Bootstrap cycle case : la passphrase Restic protège des backups qui
    # eux-mêmes contiennent les SOPS et autres secrets — donc elle DOIT
    # vivre hors-SOPS. Le Keychain est le bon endroit.
    KEYCHAIN_SERVICE="bubble-restic"
    KEYCHAIN_ACCOUNT="morty"

    if ! command -v bubble-get-keychain >/dev/null 2>&1; then
      echo "ERROR: bubble-get-keychain introuvable dans le PATH." >&2
      echo "Installe la skill auth (operator-set-keychain-secret.sh)." >&2
      exit 1
    fi

    if ! PASSPHRASE=$(bubble-get-keychain --service="$KEYCHAIN_SERVICE" --account="$KEYCHAIN_ACCOUNT" 2>/dev/null); then
      echo ""
      echo "  Passphrase Restic absente du Keychain. Création maintenant…"
      echo "  Un prompt macOS va s'ouvrir pour la saisir."
      echo ""
      echo "  IMPORTANT : choisis FORTE (4-5 mots aléatoires)."
      echo "  AUSSI : note-la dans Apple Notes / 1Password sous"
      echo "          « bubble-restic-morty » pour la redondance hors-Mac."
      echo "  Sans cette passphrase, AUCUN backup ne sera restaurable."
      echo ""
      if ! bubble-set-keychain \
            --service="$KEYCHAIN_SERVICE" \
            --account="$KEYCHAIN_ACCOUNT" \
            --label="Passphrase Restic pour les backups Morty (sans elle, aucun restore possible)"; then
        echo "ERROR: création de la passphrase annulée ou échouée." >&2
        exit 1
      fi
      # Re-fetch après création
      if ! PASSPHRASE=$(bubble-get-keychain --service="$KEYCHAIN_SERVICE" --account="$KEYCHAIN_ACCOUNT" 2>/dev/null); then
        echo "ERROR: impossible de lire la passphrase juste stockée." >&2
        exit 1
      fi
    else
      echo "  -> passphrase récupérée silencieusement depuis le Keychain."
    fi

    # Envoi via stdin → tee → install sur Morty. Le clair voyage dans le
    # pipe SSH (chiffré transport), jamais en argv (visible dans ps).
    printf '%s' "$PASSPHRASE" \
      | ssh "$REMOTE" "sudo tee /etc/bubble/restic-password > /dev/null && sudo chmod 400 /etc/bubble/restic-password && sudo chown root:root /etc/bubble/restic-password"
    PASSPHRASE=""  # wipe local (best-effort)
    echo "  -> passphrase écrite sur Morty, mode 400 root:root."
  fi
fi

# --- Étape 4 : restic init (guardé par existence-check) ---
echo "[4/6] initialisation du repo Restic (skip si déjà initialisé)..."
# Existence check : `restic cat config` renvoie 0 si le repo est initialisé.
REPO_EXISTS=$(run_ssh "sudo RESTIC_REPOSITORY=/var/backups/bubble-restic RESTIC_PASSWORD_FILE=/etc/bubble/restic-password restic cat config >/dev/null 2>&1 && echo yes || echo no" | tail -1)

if [[ "$REPO_EXISTS" = "yes" ]]; then
  echo "  -> repo déjà initialisé, skip restic init."
else
  echo "  -> repo absent, exécution restic init..."
  run_ssh "sudo RESTIC_REPOSITORY=/var/backups/bubble-restic RESTIC_PASSWORD_FILE=/etc/bubble/restic-password restic init"
fi

# --- Étape 5 : installer les unités systemd ---
echo "[5/6] installation des unités systemd..."
push_file "$TEMPLATE_DIR/bubble-restic-backup.service.template" "/etc/systemd/system/bubble-restic-backup.service"
push_file "$TEMPLATE_DIR/bubble-restic-backup.timer.template"   "/etc/systemd/system/bubble-restic-backup.timer"
push_file "$TEMPLATE_DIR/bubble-restic-forget.service.template" "/etc/systemd/system/bubble-restic-forget.service"
push_file "$TEMPLATE_DIR/bubble-restic-forget.timer.template"   "/etc/systemd/system/bubble-restic-forget.timer"

# --- Étape 6 : daemon-reload + enable + start des timers ---
echo "[6/6] daemon-reload + enable + start des timers..."
run_ssh "sudo systemctl daemon-reload"
run_ssh "sudo systemctl enable --now bubble-restic-backup.timer bubble-restic-forget.timer"

echo ""
echo "[morty-restic-setup] OK"
echo ""
echo "Vérification :"
echo "  ssh $REMOTE 'systemctl list-timers bubble-restic-*'"
echo "  ssh $REMOTE 'journalctl -u bubble-restic-backup.service --since today --no-pager | tail -30'"
echo ""
echo "Pour déclencher un backup immédiat (utile pour valider) :"
echo "  ssh $REMOTE 'sudo systemctl start bubble-restic-backup.service'"
echo ""
echo "Documentation complète : docs/BACKUP-STRATEGY.md"
