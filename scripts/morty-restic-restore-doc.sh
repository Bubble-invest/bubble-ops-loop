#!/usr/bin/env bash
# =============================================================================
# morty-restic-restore-doc.sh — Security/backup sprint, deliverable B.
#
# Imprime la cheat-sheet de restauration Restic pour Morty. Aucune action
# destructive : le script ne lance pas restic restore tout seul. Il
# montre les commandes que l'opérateur doit exécuter selon le scénario.
#
# Pourquoi un script et pas juste un .md : l'opérateur peut le piper dans
# `less`, le rediriger vers Telegram, ou le coller dans une post-mortem,
# sans avoir à naviguer dans la doc.
#
# Usage :
#   bash scripts/morty-restic-restore-doc.sh [--remote=<user@host>]
# =============================================================================
set -euo pipefail

REMOTE="hetzner"

for arg in "$@"; do
  case "$arg" in
    --remote=*) REMOTE="${arg#*=}" ;;
    --help|-h)
      cat <<'USAGE'
Usage : morty-restic-restore-doc.sh [--remote=<user@host>]

Imprime la cheat-sheet de restauration Restic pour Morty.
Défaut --remote : hetzner.
USAGE
      exit 0
      ;;
    *) echo "ERROR: argument inconnu : $arg" >&2; exit 64 ;;
  esac
done

cat <<EOF
================================================================================
RESTIC — Cheat-sheet de restauration (Morty)
================================================================================

Repo  : /var/backups/bubble-restic   (phase 1 — LOCAL, voir TODO off-site)
Cible : $REMOTE
Passphrase : /etc/bubble/restic-password  (mode 400 root)
              + sauvegarde dans 1Password sous « Morty restic password »

--------------------------------------------------------------------------------
1. Lister les snapshots disponibles
--------------------------------------------------------------------------------

  ssh $REMOTE 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \\
      RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \\
      restic snapshots'

Sortie attendue : liste de snapshots avec ID court, date, paths, tags.

--------------------------------------------------------------------------------
2. Restaurer UN seul fichier (cas le plus fréquent)
--------------------------------------------------------------------------------

  # Exemple : récupérer /etc/age/key.txt depuis le dernier snapshot.

  ssh $REMOTE 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \\
      RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \\
      restic restore latest \\
          --target /tmp/restore-\$(date +%s) \\
          --include /etc/age/key.txt'

Puis inspecter /tmp/restore-<ts>/etc/age/key.txt avant de le remettre en
place (jamais d'écrasement direct).

--------------------------------------------------------------------------------
3. Restaurer un snapshot spécifique (rollback ciblé)
--------------------------------------------------------------------------------

  # Snapshot ID visible dans \`restic snapshots\`.

  ssh $REMOTE 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \\
      RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \\
      restic restore <SNAPSHOT_ID> \\
          --target /tmp/restore-<SNAPSHOT_ID>'

--------------------------------------------------------------------------------
4. Restauration COMPLÈTE (disaster recovery)
--------------------------------------------------------------------------------

  Cf. docs/DISASTER-RECOVERY.md pour le playbook chronologique complet.
  Résumé :
    1. Provisionner nouveau VPS Hetzner CX33 + Ubuntu 24.04
    2. Restaurer /etc/age/key.txt via scripts/restore-age-key.sh
    3. Restaurer le repo Restic (si encore accessible — phase 1 = local
       au vieux Morty, donc PERDU avec lui ; phase 2 off-site = OK)
    4. restic restore latest --target / (oui, à la racine, mais
       sur un système fraichement provisionné, pas en-place sur prod)
    5. Vérifier les unités systemd + redémarrer

--------------------------------------------------------------------------------
5. Vérifier l'intégrité du repo (drill mensuel)
--------------------------------------------------------------------------------

  ssh $REMOTE 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \\
      RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \\
      restic check --read-data-subset=5%'

5% = check rapide. Pour un check complet (long, IO-intensif) :
\`restic check --read-data\`.

--------------------------------------------------------------------------------
6. Statistiques (taille du repo, dédup ratio)
--------------------------------------------------------------------------------

  ssh $REMOTE 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \\
      RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \\
      restic stats --mode raw-data'

================================================================================
EOF
