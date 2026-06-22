# Backup Strategy — Morty (Bubble Invest VPS)

**Statut :** phase 1 opérationnelle depuis le sprint sécurité 2026-05-21.
**Audience :** {{OPERATOR}} (opérateur) et tout dept-manager qui aurait besoin de comprendre ce qui est sauvegardé et comment restaurer.

---

## 1. Modèle de menace en une phrase

Avant ce sprint, **zéro backup automatisé** sur Morty (CX33 Hetzner). Pas de borg, pas de restic, pas de snapshot Hetzner, pas d'off-site. Si Morty tombe, on perd :
- `/etc/age/key.txt` — la clé racine SOPS (single point of failure absolu)
- `/etc/bubble/` — secrets chiffrés (récupérables seulement avec la clé age ci-dessus)
- `/srv/bubble-secrets/` — clé privée de l'App GitHub `bubble-ops-bot`
- `/home/claude/.claude/agent-memory/` — mémoire persistante des agents (pas dans git)
- `/home/claude/.claude/projects/` — transcripts JSONL de toutes les sessions agents
- `/home/claude/agents/<dept>/` — working clones avec l'état runtime du jour (heartbeats, outputs non encore pushés)

## 2. Ce que ce sprint apporte (phase 1)

Restic configuré avec un **repo local** `/var/backups/bubble-restic` (mode 700 root), backup toutes les 6 heures, rétention 24h/7j/4sem, le tout via systemd timer.

### Périmètre backé

| Chemin | Pourquoi | Récupérable autrement ? |
|---|---|---|
| `/etc/age/` | Racine de la chaîne SOPS | Seulement via `scripts/backup-age-key.sh` (passphrase-encrypted, commité dans `bubble-vps-data`) |
| `/etc/bubble/` | Secrets opérationnels chiffrés | Repo `bubble-vps-data` (privé) ; mais sans la clé age, inutilisable |
| `/srv/bubble-secrets/` | Clé privée App GitHub | Repo `bubble-vps-data` (privé) ; idem |
| `/home/claude/.claude/agent-memory/` | Mémoire agent persistante | **NON.** Pas dans git. Restic est la seule copie. |
| `/home/claude/.claude/projects/` | Transcripts JSONL | **NON.** Pas dans git. |
| `/home/claude/agents/<dept>/` | Working clones | Partiellement — GitHub a le dernier commit poussé, mais l'état runtime du jour (heartbeats, outputs en cours) ne l'est pas encore. |

### Exclusions

- `.cache/`
- `*.pyc`
- `__pycache__/`
- `.git/objects/` — déjà sur GitHub

### Cadence + rétention

- **Backup :** toutes les 6h (00:00 / 06:00 / 12:00 / 18:00 UTC), `Persistent=true` → rattrapage si Morty éteint au tick.
- **Forget + prune :** 1x/jour à 03:30 UTC.
- **Rétention :** 24 snapshots horaires, 7 journaliers, 4 hebdomadaires.

Concrètement avec 4 backups/jour, on garde :
- les ~24 dernières captures (= 6 jours de granularité 6h)
- + 7 captures journalières (= 7 jours de granularité quotidienne)
- + 4 hebdomadaires (= 4 semaines de granularité hebdo)

Soit ~5 semaines de rétention rolling.

## 3. TODO — passage off-site (phase 2)

**Limite critique de la phase 1 :** le repo est sur le même disque que Morty. Si Morty meurt (disque, hack, oups `rm -rf`), le backup meurt avec lui. C'est **mieux que rien** (recovery point-in-time + déduplication + chiffrement client-side + protection contre les corruptions partielles), mais ce n'est PAS du vrai backup off-site.

Migration prévue dès que {{OPERATOR}} fournit les credentials :

| Cible | Coût | Setup | Pour qui |
|---|---|---|---|
| **Backblaze B2** | ~1€/mois pour 20GB | `RESTIC_REPOSITORY=b2:bucket-name:bubble-morty` + `B2_ACCOUNT_ID` + `B2_ACCOUNT_KEY` | Recommandé : pay-as-you-go, S3-compatible, restic-native |
| **Hetzner Storage Box** | 3.81€/mois pour 100GB | `RESTIC_REPOSITORY=sftp:user@u123456.your-storagebox.de:/bubble-morty` | Si on veut rester chez Hetzner (un seul fournisseur, simpler billing) |

La migration consiste à :
1. Mettre à jour `Environment=RESTIC_REPOSITORY=` dans `bubble-restic-backup.service` (template)
2. Ajouter `Environment=B2_ACCOUNT_ID=...` / `B2_ACCOUNT_KEY=...` via un `EnvironmentFile=/etc/bubble/restic-b2.env` (en SOPS bien sûr)
3. `restic init` sur le nouveau repo (avec la même passphrase)
4. Optionnel : `restic copy` depuis le repo local pour ne pas perdre l'historique
5. `systemctl restart bubble-restic-backup.timer`

## 4. Comment vérifier que les backups réussissent

### Vue d'ensemble (santé des timers)

```bash
ssh hetzner 'systemctl list-timers bubble-restic-*'
```

Sortie attendue : deux timers `active`, prochain tick affiché.

### Logs du dernier run

```bash
ssh hetzner 'journalctl -u bubble-restic-backup.service --since today --no-pager | tail -50'
```

Cherche `processed N files, X.YZ GiB in <duration>` à la fin. Si tu vois `Files: 0 new` mais une duration > 0, c'est nominal — restic dédupe les blocs identiques.

### Liste des snapshots

```bash
ssh hetzner 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \
    RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \
    restic snapshots'
```

Tu dois voir au moins un snapshot dans les 6 dernières heures.

### Cheat-sheet de restauration

```bash
bash scripts/morty-restic-restore-doc.sh
```

Affiche la liste des commandes de restauration par scénario (fichier seul / snapshot ciblé / disaster recovery complet / check d'intégrité).

## 5. Playbook de restauration

### Cas 1 — restaurer un seul fichier (le plus fréquent)

```bash
ssh hetzner 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \
    RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \
    restic restore latest \
        --target /tmp/restore-$(date +%s) \
        --include /etc/age/key.txt'
```

Inspecte d'abord, recopie ensuite. Jamais d'écrasement direct.

### Cas 2 — restaurer un snapshot point-in-time (rollback)

```bash
# 1. Trouver le snapshot dans la liste
ssh hetzner 'sudo ... restic snapshots'

# 2. Restaurer ce snapshot vers /tmp
ssh hetzner 'sudo ... restic restore <SNAPSHOT_ID> --target /tmp/rollback'
```

### Cas 3 — disaster recovery complet

Cf. `docs/DISASTER-RECOVERY.md` pour le playbook chronologique (provisionner nouveau VPS → restaurer clé age → restaurer le reste). En phase 1, **si Morty est mort, le repo Restic est mort avec lui** — on s'appuie alors sur `bubble-vps-data` (git) + `scripts/restore-age-key.sh` + reclones GitHub.

## 6. Drill recommandé

**Tous les 3 mois** : {{OPERATOR}} fait un test de restauration depuis le repo Restic vers `/tmp/drill-<date>`, vérifie que les fichiers sont bien là, puis supprime `/tmp/drill-<date>`. Sans drill, on découvre que le backup est cassé le jour où on en a besoin — c'est la pire forme de Schrödinger.

## 7. Coût opérationnel (phase 1)

- Disque Morty : `/var/backups/bubble-restic` initialement ~1-2 GB après dédup, croît lentement.
- CPU pendant le backup : `Nice=10`, `IOSchedulingClass=best-effort` → invisible.
- Bande passante : zéro (local).

Phase 2 (off-site B2) ajoutera ~1€/mois et un peu de bande passante upload (~50 MB/jour après dédup).
