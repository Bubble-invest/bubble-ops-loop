#!/usr/bin/env bash
# =============================================================================
# restore-age-key.sh — Security/backup sprint, deliverable A.
#
# Inverse symétrique de backup-age-key.sh : lit le ciphertext local
# projects/bubble-vps-data/disaster-recovery/age-key-morty.age, demande la
# passphrase à {{OPERATOR}}, et écrit le clair sur STDOUT.
#
# Invariants :
#   - Le clair NE TOUCHE JAMAIS le disque local. Sortie sur stdout uniquement.
#   - L'opérateur pipe la sortie via SSH vers une nouvelle Morty pour
#     l'installer en mode 400 root:root.
#
# Usage canonique (restauration sur un Morty neuf) :
#   bash scripts/restore-age-key.sh \
#       | ssh root@<new-morty-ip> 'install -m 400 /dev/stdin /etc/age/key.txt'
#
# Usage inspection (impression du clair sur le terminal — uniquement si
# tu sais ce que tu fais et que tu n'as personne derrière toi) :
#   bash scripts/restore-age-key.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INPUT_FILE="$WORKSPACE_ROOT/projects/bubble-vps-data/disaster-recovery/age-key-morty.age"

usage() {
  cat <<'USAGE'
Usage : restore-age-key.sh [--input=<path>]

Synopsis :
  Décrypte projects/bubble-vps-data/disaster-recovery/age-key-morty.age
  avec la passphrase fournie par {{OPERATOR}} et écrit le clair sur STDOUT.

Arguments :
  --input=<path>   Chemin du ciphertext. Défaut : le fichier canonique
                   dans bubble-vps-data/disaster-recovery/.
  --help           Affiche ce message.

CHEMIN CANONIQUE de restauration sur un Morty neuf :

  bash scripts/restore-age-key.sh \
      | ssh root@<new-morty-ip> 'install -m 400 /dev/stdin /etc/age/key.txt'

age prompte la passphrase sur /dev/tty (saisie cachée). La pipe SSH ne
voit que le clair, qui est immédiatement écrit en mode 400 root:root via
`install`. Aucun fichier temporaire en clair.

DANGER : si tu lances le script sans pipe, le clair s'imprime sur ton
terminal. Évite si tu n'es pas seul devant ton écran.
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --input=*) INPUT_FILE="${arg#*=}" ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: argument inconnu : $arg" >&2; usage >&2; exit 64 ;;
  esac
done

# --- Préflight : binaires requis ? ---
# openssl (déchiffrement symétrique — refacto du 2026-05-21 pour aligner
# avec backup-age-key.sh qui chiffre via openssl) + bubble-get-keychain.
if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: binaire 'openssl' introuvable. Il devrait être préinstallé sur macOS." >&2
  exit 1
fi
if ! command -v bubble-get-keychain >/dev/null 2>&1; then
  echo "ERROR: bubble-get-keychain introuvable dans le PATH." >&2
  echo "Installe la skill auth (operator-set-keychain-secret.sh)." >&2
  exit 1
fi

# --- Préflight : ciphertext lisible ? ---
if [[ ! -f "$INPUT_FILE" ]]; then
  echo "ERROR: fichier de ciphertext introuvable : $INPUT_FILE" >&2
  echo "" >&2
  echo "As-tu pull la dernière version de bubble-vps-data ?" >&2
  echo "  cd projects/bubble-vps-data && git pull" >&2
  exit 1
fi

if [[ ! -s "$INPUT_FILE" ]]; then
  echo "ERROR: fichier de ciphertext vide : $INPUT_FILE" >&2
  exit 1
fi

# --- Vérif rapide : c'est bien un fichier openssl symmétrique ? ---
# Format binaire AES-256-CBC PBKDF2 avec sel — premier 8 bytes = "Salted__".
if ! head -c 8 "$INPUT_FILE" | grep -q "Salted__"; then
  echo "ERROR: en-tête OpenSSL absent. Le fichier ne semble pas être un ciphertext openssl." >&2
  echo "" >&2
  echo "Si tu as un vieux backup au format age ASCII-armored (avant le refacto" >&2
  echo "du 2026-05-21), utilise : age --decrypt $INPUT_FILE" >&2
  exit 1
fi

# --- Avertissement TTY si on n'est pas dans un pipe ---
if [[ -t 1 ]]; then
  echo "ATTENTION : tu lances le script SANS pipe." >&2
  echo "Le clair de la clé age va s'imprimer sur ton terminal." >&2
  echo "Si tu n'es pas seul, annule (Ctrl-C) et utilise plutôt :" >&2
  echo "" >&2
  echo "  bash scripts/restore-age-key.sh \\" >&2
  echo "      | ssh root@<new-morty-ip> 'install -m 400 /dev/stdin /etc/age/key.txt'" >&2
  echo "" >&2
  read -r -p "Continuer quand même ? [yes/NO] " confirm
  if [[ "$confirm" != "yes" ]]; then
    echo "Annulé." >&2
    exit 0
  fi
fi

# --- Récupération passphrase depuis Keychain ---
KEYCHAIN_SERVICE="bubble-age-backup"
KEYCHAIN_ACCOUNT="morty"
if ! PASSPHRASE=$(bubble-get-keychain --service="$KEYCHAIN_SERVICE" --account="$KEYCHAIN_ACCOUNT" 2>/dev/null); then
  echo "ERROR: Passphrase absente du Keychain (service=$KEYCHAIN_SERVICE, account=$KEYCHAIN_ACCOUNT)." >&2
  echo "" >&2
  echo "Cas possibles :" >&2
  echo "  1. Tu es sur un Mac fresh (pas celui qui a fait le backup) — restaure" >&2
  echo "     la passphrase depuis 1Password puis :" >&2
  echo "     bubble-set-keychain --service=$KEYCHAIN_SERVICE --account=$KEYCHAIN_ACCOUNT" >&2
  echo "  2. Le Keychain a été corrompu/restauré — même solution." >&2
  echo "  3. La passphrase a été supprimée par erreur — restaure depuis 1Password." >&2
  exit 1
fi

# --- Cœur : décryption symétrique, sortie sur stdout ---
# openssl prend la passphrase via process substitution (file:<fd>).
# Le clair sort sur stdout. AUCUNE écriture sur disque local.
# AUCUNE passphrase visible dans `ps`.
openssl enc -aes-256-cbc -pbkdf2 -d \
  -pass file:<(printf '%s' "$PASSPHRASE") \
  -in "$INPUT_FILE"

# Wipe local
PASSPHRASE=""
