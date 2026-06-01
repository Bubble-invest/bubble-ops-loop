#!/usr/bin/env bash
# =============================================================================
# backup-age-key.sh — Security/backup sprint, deliverable A.
#
# Re-encrypte /etc/age/key.txt depuis Morty avec une passphrase fournie
# par Joris (mode symétrique age, séparé de la chaîne SOPS), puis écrit
# le ciphertext dans bubble-vps-data/disaster-recovery/age-key-morty.age.
#
# Modèle de menace adressé :
#   /etc/age/key.txt fait 184 octets, mode 400, et n'a AUCUNE copie. Si
#   Morty meurt, tous les secrets SOPS de la stack (Bubble Invest)
#   deviennent inrécupérables. On ne peut pas le copier en clair (cela
#   ruinerait le modèle) ; on le re-chiffre symétriquement avec une
#   passphrase humaine (que Joris stocke en 1Password), et on commit le
#   ciphertext dans le repo privé bubble-vps-data.
#
# Invariants :
#   - Le clair NE TOUCHE JAMAIS le disque (pipe ssh|sudo cat | age).
#   - Le script NE COMMIT PAS, NE PUSH PAS — Joris fait la revue + commit
#     manuel comme dernière étape.
#   - Le script échoue fort si age est absent ou si la passphrase est vide.
#
# Usage :
#   ./backup-age-key.sh [--remote=user@host]
#
# Défaut --remote : hetzner (alias ~/.ssh/config).
#
# Étapes opérateur :
#   1. bash scripts/backup-age-key.sh
#   2. age demande deux fois la passphrase (saisie cachée)
#   3. Vérifier la sortie : ls -la projects/bubble-vps-data/disaster-recovery/
#   4. cd projects/bubble-vps-data && git add disaster-recovery/age-key-morty.age && git commit
#
# Sortie : projects/bubble-vps-data/disaster-recovery/age-key-morty.age
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts/ is inside projects/bubble-ops-loop/. The workspace root that
# contains projects/bubble-vps-data/ is two `..` levels up.
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
OUTPUT_DIR="$WORKSPACE_ROOT/projects/bubble-vps-data/disaster-recovery"
OUTPUT_FILE="$OUTPUT_DIR/age-key-morty.age"

usage() {
  cat <<'USAGE'
Usage : backup-age-key.sh [--remote=<user@host>]

Synopsis :
  Re-chiffre /etc/age/key.txt depuis Morty avec une passphrase symétrique
  (age --passphrase) et écrit le ciphertext dans
  projects/bubble-vps-data/disaster-recovery/age-key-morty.age.

Arguments :
  --remote=<host>   Cible SSH. Défaut : hetzner (alias ~/.ssh/config).
  --help            Affiche ce message.

IMPORTANT :
  - La passphrase est saisie deux fois par age (interactif).
  - Stocke-la dans 1Password sous le nom « age-key-morty backup passphrase ».
  - Sans cette passphrase, le backup devient inutilisable : on perd la
    capacité de restaurer la chaîne SOPS.
  - Le script NE COMMIT PAS le fichier. Joris fait : `cd projects/bubble-vps-data
    && git add disaster-recovery/age-key-morty.age && git commit`.

Exemple :
  ./backup-age-key.sh
  ./backup-age-key.sh --remote=root@1.2.3.4
USAGE
}

REMOTE="hetzner"

for arg in "$@"; do
  case "$arg" in
    --remote=*) REMOTE="${arg#*=}" ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: argument inconnu : $arg" >&2; usage >&2; exit 64 ;;
  esac
done

# --- Préflight : binaires requis ? ---
# openssl (chiffrement symétrique) + bubble-get-keychain (passphrase from Keychain)
# Note : on n'utilise PLUS `age --passphrase` (qui lit via /dev/tty et ne peut
# pas être scripté). À la place : `openssl enc -aes-256-cbc -pbkdf2` qui
# accepte la passphrase via file descriptor (process substitution).
# La passphrase vit dans le Keychain natif macOS (skill auth Flow 3) — refacto
# du 2026-05-21 (msg 2823-2825) pour éliminer la saisie interactive.
if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: binaire 'openssl' introuvable. Il devrait être préinstallé sur macOS." >&2
  exit 1
fi
if ! command -v bubble-get-keychain >/dev/null 2>&1; then
  echo "ERROR: bubble-get-keychain introuvable dans le PATH." >&2
  echo "Vérifie que ~/.local/bin est dans ton PATH." >&2
  echo "Si absent, installe la skill auth (operator-set-keychain-secret.sh)." >&2
  exit 1
fi

# --- Préflight : dossier de sortie existe / est créable ---
if [[ ! -d "$OUTPUT_DIR" ]]; then
  echo "[backup-age-key] création du dossier : $OUTPUT_DIR"
  mkdir -p "$OUTPUT_DIR"
fi

# --- Préflight : ne pas écraser silencieusement un backup existant ---
if [[ -f "$OUTPUT_FILE" ]]; then
  echo "ATTENTION : un backup existe déjà :"
  echo "  $OUTPUT_FILE"
  echo "  taille : $(wc -c < "$OUTPUT_FILE") octets"
  echo "  modifié : $(stat -f '%Sm' "$OUTPUT_FILE" 2>/dev/null || stat -c '%y' "$OUTPUT_FILE")"
  read -r -p "Écraser ? [yes/NO] " confirm
  if [[ "$confirm" != "yes" ]]; then
    echo "Annulé."
    exit 0
  fi
fi

echo "[backup-age-key] cible SSH       : $REMOTE"
echo "[backup-age-key] sortie locale   : $OUTPUT_FILE"
echo ""

# --- Récupération de la passphrase depuis le Keychain natif macOS ---
# La passphrase vit dans le Keychain sous le service "bubble-age-backup",
# account "morty". Si absente, on demande à l'opérateur de la créer
# maintenant via bubble-set-keychain (prompt osascript GUI, saisie masquée).
KEYCHAIN_SERVICE="bubble-age-backup"
KEYCHAIN_ACCOUNT="morty"

if ! PASSPHRASE=$(bubble-get-keychain --service="$KEYCHAIN_SERVICE" --account="$KEYCHAIN_ACCOUNT" 2>/dev/null); then
  echo "[backup-age-key] Passphrase absente du Keychain. Création maintenant…"
  echo ""
  echo "Un prompt macOS va s'ouvrir pour saisir la passphrase. Choisis-la"
  echo "FORTE (genre 4-5 mots aléatoires) et garde-la en mémoire OU dans"
  echo "1Password en plus du Keychain (redondance — Keychain peut être"
  echo "corrompu suite à OS update ou Time Machine restore)."
  echo ""
  if ! bubble-set-keychain \
        --service="$KEYCHAIN_SERVICE" \
        --account="$KEYCHAIN_ACCOUNT" \
        --label="Passphrase pour le backup chiffré de la clé age de Morty (sans elle, le backup est inutilisable)"; then
    echo "ERROR: Création de la passphrase annulée ou échouée." >&2
    exit 1
  fi
  # Re-fetch après création
  if ! PASSPHRASE=$(bubble-get-keychain --service="$KEYCHAIN_SERVICE" --account="$KEYCHAIN_ACCOUNT" 2>/dev/null); then
    echo "ERROR: Impossible de lire la passphrase juste stockée. Anomalie Keychain." >&2
    exit 1
  fi
  echo "[backup-age-key] Passphrase stockée dans le Keychain. Backups suivants seront silencieux."
  echo ""
else
  echo "[backup-age-key] Passphrase récupérée silencieusement depuis le Keychain."
fi

# --- Cœur : le clair voyage uniquement dans un pipe, jamais sur disque ---
# `ssh ... sudo cat` lit /etc/age/key.txt (mode 400 root) sur Morty.
# Le payload sort sur stdout via SSH, et est immédiatement consommé par
# `openssl enc -aes-256-cbc -pbkdf2` (chiffrement symétrique authentifié).
# La passphrase est passée via process substitution (-pass file:<(...)) =
# un file descriptor éphémère, jamais visible dans `ps`, jamais sur disque.
# Le ciphertext binaire est écrit sur OUTPUT_FILE.
#
# AUCUNE étape intermédiaire ne touche le disque. Pas de fichier temporaire.
# La passphrase ne touche jamais le filesystem (process substitution = pipe FD).
ssh "$REMOTE" "sudo cat /etc/age/key.txt" \
  | openssl enc -aes-256-cbc -pbkdf2 -salt \
      -pass file:<(printf '%s' "$PASSPHRASE") \
  > "$OUTPUT_FILE"

# Wipe local PASSPHRASE variable (best-effort)
PASSPHRASE=""

# --- Vérification post-écriture ---
if [[ ! -s "$OUTPUT_FILE" ]]; then
  echo "ERROR: le fichier de sortie est vide. Backup échoué." >&2
  rm -f "$OUTPUT_FILE"
  exit 1
fi

# Vérif rapide : le fichier doit commencer par l'en-tête OpenSSL "Salted__".
# (Format binaire AES-256-CBC PBKDF2 avec sel — premier 8 bytes = "Salted__").
if ! head -c 8 "$OUTPUT_FILE" | grep -q "Salted__"; then
  echo "ERROR: en-tête OpenSSL absent. Le fichier ne semble pas être un ciphertext openssl valide." >&2
  exit 1
fi

echo ""
echo "[backup-age-key] OK"
echo "  fichier : $OUTPUT_FILE"
echo "  taille  : $(wc -c < "$OUTPUT_FILE") octets"
echo ""
echo "PROCHAINE ÉTAPE (manuelle) :"
echo "  cd $WORKSPACE_ROOT/projects/bubble-vps-data"
echo "  git add disaster-recovery/age-key-morty.age"
echo "  git commit -m 'disaster-recovery: backup age key (passphrase-encrypted)'"
echo "  git push"
echo ""
echo "Le script NE COMMIT PAS automatiquement (revue humaine obligatoire)."
echo ""
echo "RAPPEL — perte de passphrase = perte de la capacité de restauration."
echo "  Keychain natif : passphrase stockée, accessible programmatiquement"
echo "  Pour redondance : ouvre Passwords.app et exporte/note la passphrase"
echo "    OU ajoute-la dans 1Password en plus (recommandé)."
echo "  Rotation : bubble-delete-keychain --service=$KEYCHAIN_SERVICE --account=$KEYCHAIN_ACCOUNT"
echo "             puis relance ce script (prompt à nouveau)."
