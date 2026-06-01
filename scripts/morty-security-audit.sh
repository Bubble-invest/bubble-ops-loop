#!/usr/bin/env bash
# =============================================================================
# morty-security-audit.sh — Security/backup sprint, deliverable D.
#
# Inventaire de la posture sécurité de Morty. Read-only — aucune
# mutation. Sortie en markdown sur stdout, pipe-friendly (mail / telegram).
#
# Sections émises :
#   ## SSH posture            PasswordAuthentication, PermitRootLogin, # de clés
#   ## Sudo posture           sudoers.d, NOPASSWD
#   ## Firewall               ufw status + ports d'écoute
#   ## Secret files           paths + tailles + mtimes (JAMAIS les contenus)
#   ## Backup status          dernier snapshot Restic, count, taille
#   ## Recent SSH logins      10 dernières
#   ## Systemd units          bubble-* + ops-loop-*
#   ## Disk usage             warning si > 80 %
#   ## Memory                 free -h
#   ## Updates pending        apt list --upgradable | wc -l
#
# Usage :
#   bash scripts/morty-security-audit.sh [--remote=<user@host>]
#
# Cadence recommandée : mensuel, ou après tout évènement sécurité.
#
# Pour piper vers Telegram :
#   bash scripts/morty-security-audit.sh | bubble-tg-send --chat=<id>
#
# Pour piper vers mail :
#   bash scripts/morty-security-audit.sh | mail -s "Morty audit $(date +%F)" joris@...
# =============================================================================
set -euo pipefail

REMOTE="hetzner"

for arg in "$@"; do
  case "$arg" in
    --remote=*) REMOTE="${arg#*=}" ;;
    --help|-h)
      cat <<'USAGE'
Usage : morty-security-audit.sh [--remote=<user@host>]

Inventaire read-only de la posture sécurité de Morty.
Sortie markdown sur stdout.
Défaut --remote : hetzner.
USAGE
      exit 0
      ;;
    *) echo "ERROR: argument inconnu : $arg" >&2; exit 64 ;;
  esac
done

# Helper : run a command on Morty, print its output, ignore errors so
# one missing tool doesn't kill the whole audit.
remote() {
  ssh "$REMOTE" "$1" 2>/dev/null || echo "(commande indisponible ou erreur)"
}

# --- En-tête ---
echo "# Morty — audit sécurité"
echo ""
echo "- **Cible :** \`$REMOTE\`"
echo "- **Date :** $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "- **Script :** scripts/morty-security-audit.sh"
echo ""
echo "---"
echo ""

# --- 1. SSH posture ---
echo "## SSH posture"
echo ""
echo "Sortie de \`sshd -T\` (config effective) :"
echo ""
echo '```'
remote "sudo sshd -T 2>/dev/null | grep -iE '^(passwordauthentication|permitrootlogin|pubkeyauthentication|permitemptypasswords|x11forwarding|maxauthtries|loginGraceTime|allowusers|denyusers|usepam)'" || true
echo '```'
echo ""
echo "Nombre de clés autorisées par utilisateur :"
echo ""
echo '```'
remote "for u in \$(getent passwd | awk -F: '\$3 >= 1000 && \$3 < 65534 {print \$1}'; echo root); do
  f=\$(getent passwd \"\$u\" | cut -d: -f6)/.ssh/authorized_keys
  if sudo test -f \"\$f\"; then
    n=\$(sudo wc -l < \"\$f\")
    printf '  %-12s %3d cles  (%s)\\n' \"\$u\" \"\$n\" \"\$f\"
  fi
done"
echo '```'
echo ""

# --- 2. Sudo posture ---
echo "## Sudo posture"
echo ""
echo "Fichiers dans \`/etc/sudoers.d/\` :"
echo ""
echo '```'
remote "sudo ls -la /etc/sudoers.d/ 2>/dev/null || true"
echo '```'
echo ""
echo "Entrées NOPASSWD (à scruter — chaque NOPASSWD est un risque privilege-escalation) :"
echo ""
echo '```'
remote "sudo grep -rEn 'NOPASSWD' /etc/sudoers /etc/sudoers.d/ 2>/dev/null | grep -v '^#' || echo '(aucune entrée NOPASSWD trouvée)'"
echo '```'
echo ""

# --- 3. Firewall / listening ports ---
echo "## Firewall + ports d'écoute"
echo ""
echo "Statut ufw :"
echo ""
echo '```'
remote "sudo ufw status verbose 2>/dev/null || echo '(ufw absent ou inactif)'"
echo '```'
echo ""
echo "Ports en écoute (LISTEN) — process + user :"
echo ""
echo '```'
remote "sudo ss -tlnpu 2>/dev/null | head -40"
echo '```'
echo ""

# --- 4. Secret files (paths + tailles + mtimes — JAMAIS les contenus) ---
echo "## Secret files (inventaire — pas de contenus)"
echo ""
echo "Listing métadonnées (mode / owner / size / mtime) :"
echo ""
echo '```'
# `ls -la` n'imprime que les métadonnées du fichier (pas le contenu).
# `stat` idem. On NE LIT JAMAIS le contenu.
remote "sudo ls -la /etc/age/ 2>/dev/null || true"
remote "sudo ls -la /etc/bubble/ 2>/dev/null || true"
remote "sudo ls -la /srv/bubble-secrets/ 2>/dev/null || true"
echo '```'
echo ""
echo "Empreintes (sha256 — pour détecter une modification sans révéler le contenu) :"
echo ""
echo '```'
remote "sudo find /etc/age /etc/bubble /srv/bubble-secrets -type f 2>/dev/null | sudo xargs -I{} sh -c 'echo \"\$(sha256sum {} | cut -c1-16)...  {}\"' 2>/dev/null || true"
echo '```'
echo ""

# --- 5. Backup status (Restic) ---
echo "## Backup status (Restic)"
echo ""
echo "Timers Restic actifs :"
echo ""
echo '```'
remote "systemctl list-timers 'bubble-restic-*' --no-pager 2>/dev/null || echo '(timers Restic absents — Restic pas encore provisionné ?)'"
echo '```'
echo ""
echo "Derniers snapshots :"
echo ""
echo '```'
remote "sudo RESTIC_REPOSITORY=/var/backups/bubble-restic RESTIC_PASSWORD_FILE=/etc/bubble/restic-password restic snapshots --no-lock 2>/dev/null | tail -15 || echo '(repo Restic absent ou passphrase incorrecte)'"
echo '```'
echo ""
echo "Stats du repo (taille after dédup, ratio) :"
echo ""
echo '```'
remote "sudo RESTIC_REPOSITORY=/var/backups/bubble-restic RESTIC_PASSWORD_FILE=/etc/bubble/restic-password restic stats --mode raw-data --no-lock 2>/dev/null || true"
echo '```'
echo ""

# --- 6. Recent SSH logins ---
echo "## Recent SSH logins (10 dernières)"
echo ""
echo '```'
remote "last -n 10 -F 2>/dev/null | head -15"
echo '```'
echo ""
echo "Échecs d'authentification SSH (10 derniers) :"
echo ""
echo '```'
remote "sudo journalctl -u ssh -u sshd --since '7 days ago' --no-pager 2>/dev/null | grep -iE 'failed|invalid' | tail -10 || echo '(aucun échec récent)'"
echo '```'
echo ""

# --- 7. Systemd units (bubble-* + ops-loop-*) ---
echo "## Systemd units (bubble-* + ops-loop-*)"
echo ""
echo '```'
remote "systemctl list-units --type=service --all 'bubble-*' 'ops-loop-*' 'claude-agent-*' --no-pager 2>/dev/null || true"
echo '```'
echo ""
echo "Timers actifs :"
echo ""
echo '```'
remote "systemctl list-timers --all 'bubble-*' 'ops-loop-*' --no-pager 2>/dev/null || true"
echo '```'
echo ""

# --- 8. Disk usage ---
echo "## Disk usage"
echo ""
echo '```'
remote "df -h --output=source,size,used,avail,pcent,target -x tmpfs -x devtmpfs -x squashfs 2>/dev/null"
echo '```'
echo ""
echo "WARNING si un volume dépasse 80 % :"
echo ""
echo '```'
remote "df -h --output=source,pcent,target -x tmpfs -x devtmpfs -x squashfs 2>/dev/null | awk 'NR > 1 && \$2+0 > 80 {print \"WARN:\", \$0}' || echo '(aucun volume > 80 %)'"
echo '```'
echo ""

# --- 9. Memory ---
echo "## Memory"
echo ""
echo '```'
remote "free -h 2>/dev/null"
echo '```'
echo ""

# --- 10. Updates pending ---
echo "## Updates pending"
echo ""
echo '```'
remote "apt list --upgradable 2>/dev/null | tail -n +2 | wc -l | xargs -I{} echo '{} paquets en attente de mise à jour'"
echo '```'
echo ""
echo "Détail des paquets sécurité :"
echo ""
echo '```'
remote "apt list --upgradable 2>/dev/null | grep -i security | head -20 || echo '(aucun update sécurité en attente)'"
echo '```'
echo ""

echo "---"
echo ""
echo "_Fin de l'audit. Read-only — aucune modification effectuée sur Morty._"
