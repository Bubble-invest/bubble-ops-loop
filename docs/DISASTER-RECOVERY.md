# Disaster Recovery — Bubble Invest VPS (Morty)

**Audience :** toi, à 3 h du matin, stressé, devant un Morty qui ne répond plus.
**Promesse :** suis ce document dans l'ordre. Il t'amène d'un Morty mort à un Morty vivant en moins de 4 heures (la phase 1 du sprint sécurité 2026-05-21 vise ce SLA).

**Docs et scripts compagnons :**
- `docs/DISASTER-RECOVERY-AGE-KEY.md` — détail du backup/restore de la clé age (deliverable A)
- `docs/BACKUP-STRATEGY.md` — politique Restic, périmètre, rétention (deliverable B)
- `scripts/backup-age-key.sh` + `scripts/restore-age-key.sh` — backup chiffré de la clé age
- `scripts/morty-restic-setup.sh` + `scripts/morty-restic-restore-doc.sh` — Restic
- `scripts/morty-security-audit.sh` — audit de posture (deliverable D)

**Avant tout :** respire. Si Morty est mort mais que :
- Tu as encore accès au Mac de Joris ou de Jade
- Tu as encore accès au repo `bubble-vps-data` (privé, GitHub)
- Tu te souviens des passphrases (1Password)

…alors tu peux tout reconstruire. Toutes les pièces sont là.

---

## 1. Si Morty est mort (playbook chronologique complet)

### Étape 1 — Provisionner un nouveau VPS Hetzner CX33

Cette étape est **MANUELLE**. Rick ne peut pas l'automatiser sans `HCLOUD_TOKEN` (Joris doit fournir le token API Hetzner, pas encore configuré au moment de ce sprint).

Va sur le dashboard Hetzner (https://console.hetzner.cloud/), section Cloud, et crée :
- **Type d'instance :** CX33 (ARM64, 8 GB RAM, 80 GB SSD) — c'est l'identique de l'ancien Morty.
- **OS :** Ubuntu 24.04 LTS.
- **Région :** Nuremberg ou Falkenstein (latence Europe).
- **Nom :** `morty` (réutilise le même nom — l'alias SSH `hetzner` dans `~/.ssh/config` pointera dessus).
- **Clé SSH :** ajoute ta clé publique (`~/.ssh/id_ed25519.pub` de Joris ou de Jade).
- **Réseau :** firewall par défaut suffit pour démarrer.

Note l'IP publique du nouveau Morty (ex: `1.2.3.4`).

### Étape 2 — Mettre à jour l'alias SSH

Sur le Mac qui lance les scripts :

```bash
# Édite ~/.ssh/config et change l'IP de l'alias hetzner.
# Exemple de bloc :
#   Host hetzner
#       HostName 1.2.3.4       # <-- nouvelle IP
#       User root
#       IdentityFile ~/.ssh/id_ed25519
ssh hetzner 'hostname && uname -a'
# Doit répondre : morty Linux ... aarch64 ...
```

### Étape 3 — Restaurer `/etc/age/key.txt`

Sans cette clé, aucun secret SOPS n'est exploitable. C'est l'étape la plus importante.

```bash
cd ~/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
bash scripts/restore-age-key.sh \
    | ssh hetzner 'install -m 400 /dev/stdin /etc/age/key.txt'
```

`age` te prompte la passphrase (celle stockée dans 1Password sous le nom `age-key-morty backup passphrase`). Le clair de la clé voyage uniquement dans le pipe SSH, puis `install` l'écrit en mode 400 root:root sur Morty.

Vérifie :

```bash
ssh hetzner 'ls -la /etc/age/key.txt && wc -c /etc/age/key.txt'
# attendu : -r-------- 1 root root 184 ... /etc/age/key.txt
```

### Étape 4 — Restaurer les fichiers SOPS-encrypted

Le repo privé `bubble-vps-data` contient déjà tous les `*.sops.env` et `*.sops.pem` chiffrés. Maintenant que `/etc/age/key.txt` est restauré, on les remet en place.

```bash
# Cloner le repo de données sur le nouveau Morty.
ssh hetzner 'sudo install -d -m 700 /srv && cd /srv && sudo git clone https://github.com/<owner>/bubble-vps-data.git'

# Remettre les fichiers à leur place canonique.
ssh hetzner 'sudo install -d -m 700 /etc/bubble /srv/bubble-secrets'
ssh hetzner 'sudo cp /srv/bubble-vps-data/tenants/bubble-internal/secrets.sops.env /etc/bubble/'
ssh hetzner 'sudo cp /srv/bubble-vps-data/tenants/bubble-internal/github-app-bubble-ops-bot.private-key.sops.pem /srv/bubble-secrets/'

# Vérifier qu'un secret se déchiffre.
ssh hetzner 'SOPS_AGE_KEY_FILE=/etc/age/key.txt sops -d /etc/bubble/secrets.sops.env | head -3'
```

Si tu vois les variables d'environnement attendues, la chaîne SOPS est rétablie.

### Étape 5 — Restaurer les working clones depuis GitHub

Chaque dept a son repo `bubble-ops-<slug>` sur GitHub. Recloner.

```bash
ssh hetzner 'sudo install -d -m 755 -o claude -g claude /home/claude/agents'
for slug in fixture maya ben tony miranda eliot; do
  ssh hetzner "sudo -u claude git clone https://github.com/vdk888/bubble-ops-$slug /home/claude/agents/$slug" || true
done
```

(Le `|| true` est OK : certains dept peuvent ne pas avoir de repo encore.)

### Étape 6 — Restaurer la mémoire agent depuis Restic (si disponible)

**Phase 1 :** le repo Restic était local sur l'ancien Morty → il est **mort avec lui**. Tu n'as donc rien à restaurer ici, sauf si Joris a déjà migré vers off-site (B2 ou Storage Box).

**Phase 2 (off-site activée) :** restaure `/home/claude/.claude/agent-memory/` + `/home/claude/.claude/projects/` depuis le repo Restic distant.

```bash
# Adapter RESTIC_REPOSITORY à la cible off-site configurée.
ssh hetzner 'sudo RESTIC_REPOSITORY=<off-site-repo> \
    RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \
    restic restore latest \
        --target / \
        --include /home/claude/.claude/agent-memory \
        --include /home/claude/.claude/projects'
```

Cf. `docs/BACKUP-STRATEGY.md` pour les détails.

### Étape 7 — Redémarrer les unités systemd

```bash
# Installer les unités ops-loop pour chaque dept actif (via deploy-to-morty.sh).
cd ~/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
for slug in fixture maya; do
  bash scripts/deploy-to-morty.sh --slug=$slug --remote=hetzner
done

# Re-installer les unités Restic.
bash scripts/morty-restic-setup.sh --remote=hetzner

# Vérifier que tout tourne.
ssh hetzner 'systemctl list-timers bubble-* ops-loop-*'
```

Envoie `/start` aux bots Telegram de chaque dept pour re-pairer ton chat_id.

---

## 2. Si seulement `/etc/age/key.txt` est corrompu

Cas moins grave : le reste de Morty est intact, seule la clé age a sauté (suppression accidentelle, disque corrompu sur ce fichier, etc.).

```bash
# Restaurer uniquement la clé depuis le backup chiffré.
cd ~/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
bash scripts/restore-age-key.sh \
    | ssh hetzner 'install -m 400 /dev/stdin /etc/age/key.txt'

# Vérifier qu'un secret SOPS redevient lisible.
ssh hetzner 'SOPS_AGE_KEY_FILE=/etc/age/key.txt sops -d /etc/bubble/secrets.sops.env | head -3'

# Redémarrer les services qui dépendent des secrets.
ssh hetzner 'sudo systemctl restart bubble-token-broker bubble-git-guard ops-loop-*'
```

Cf. `docs/DISASTER-RECOVERY-AGE-KEY.md` pour le détail du flux.

---

## 3. Si on perd la passphrase Restic

**Conséquence :** tous les backups Restic deviennent inutilisables. Le repo `/var/backups/bubble-restic` (ou son équivalent off-site) reste accessible mais aucun fichier ne peut être déchiffré.

**Ce qu'on a encore :**
- Le repo `bubble-vps-data` (privé, GitHub) avec les secrets SOPS chiffrés
- Le backup age-key chiffré dans `bubble-vps-data/disaster-recovery/age-key-morty.age` (si tu te souviens encore de SA passphrase — autre 1Password entry)
- Les working clones GitHub
- Pas de mémoire agent, pas de transcripts

**Mitigation préventive (à faire MAINTENANT, pas après l'incident) :**
- Stocker la passphrase Restic dans 1Password sous `Morty restic password`, partagée entre Joris et Jade.
- Imprimer la passphrase sur papier et la déposer dans le coffre physique.
- Idem pour la passphrase du backup age-key (`age-key-morty backup passphrase`).

```bash
# Tu peux vérifier que la passphrase 1Password fonctionne sans déclencher
# une restauration réelle :
ssh hetzner 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \
    RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \
    restic snapshots | head -5'
# Si ça liste les snapshots, la passphrase est bonne. À faire chaque
# trimestre dans le drill.
```

---

## 4. Si on perd l'accès au compte GitHub

Scénario extrême : ton compte GitHub a été compromis ou supprimé, et tu n'as plus accès à `bubble-vps-data`, `bubble-ops-platform`, ni aux repos `bubble-ops-<slug>`.

**Ce qu'on a encore en local sur les machines de Joris et Jade :**
- Tous les clones locaux dans `~/claude-workspaces/Rick_RnD/projects/` — chaque clone contient le repo complet (objects, refs, branches), pas juste le working tree.
- Le backup age-key chiffré (`bubble-vps-data/disaster-recovery/age-key-morty.age`) — il est sur ton disque local, pas seulement sur GitHub.
- Les fichiers SOPS chiffrés (idem — clonés en local).

```bash
# Recréer un compte GitHub (ou utiliser un compte de secours).
# Pousser les clones locaux vers le nouveau compte.
cd ~/claude-workspaces/Rick_RnD/projects/bubble-vps-data
git remote set-url origin git@github.com:<new-account>/bubble-vps-data.git
git push --mirror

# Idem pour chaque repo critique.
for proj in bubble-ops-loop bubble-vps-platform bubble-vps-data; do
  cd ~/claude-workspaces/Rick_RnD/projects/$proj
  git remote set-url origin git@github.com:<new-account>/$proj.git
  git push --mirror
done
```

Tu devras aussi recréer l'App GitHub `bubble-ops-bot` (le `client_id` est nouveau, mais la clé privée stockée dans `bubble-vps-data/tenants/bubble-internal/github-app-bubble-ops-bot.private-key.sops.pem` ne sert plus — il faut générer une nouvelle paire et re-chiffrer le tout). Cf. `projects/bubble-vps-platform/docs/` pour le flux de provisionnement App GitHub.

---

## 5. Drill — faire un test de restauration tous les 3 mois

Sans drill périodique, tu découvres que le backup est cassé le jour où tu en as besoin. C'est le pire mode de défaillance. Mets-toi un rappel calendrier.

**Drill rapide (15 minutes, à faire chaque trimestre) :**

```bash
# 1. Vérifier que les timers Restic tournent.
ssh hetzner 'systemctl list-timers bubble-restic-*'

# 2. Vérifier qu'un snapshot récent existe.
ssh hetzner 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \
    RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \
    restic snapshots | tail -10'

# 3. Restaurer un fichier dans /tmp et vérifier qu'il est lisible.
ssh hetzner 'sudo RESTIC_REPOSITORY=/var/backups/bubble-restic \
    RESTIC_PASSWORD_FILE=/etc/bubble/restic-password \
    restic restore latest \
        --target /tmp/drill-$(date +%Y%m%d) \
        --include /etc/age/key.txt'
ssh hetzner 'ls -la /tmp/drill-*/etc/age/key.txt'

# 4. Nettoyer.
ssh hetzner 'sudo rm -rf /tmp/drill-*'

# 5. Vérifier que tu te souviens des deux passphrases (1Password) :
#    - age-key-morty backup passphrase
#    - Morty restic password
```

**Drill complet (1 demi-journée, à faire chaque semestre) :**

Provisionner un VPS Hetzner CX21 (plus petit, moins cher) en mode jetable, et dérouler les étapes 1-7 de la section 1 ci-dessus jusqu'à un Morty fonctionnel. Puis détruire le VPS jetable. Si tu as buté sur une étape, mettre à jour ce document.

---

## 6. Hors-scope de ce sprint (limites connues)

Ce sprint sécurité ne couvre PAS :
- Authentification 2FA SSH sur Morty (mot de passe + token + clé)
- Tripwire / AIDE (détection d'intrusion sur fichiers système)
- Durcissement `PermitRootLogin` (passage en `prohibit-password`)
- Fail2ban pour SSH
- Audit log centralisé (export `auditd` vers un collecteur externe)
- HCLOUD_TOKEN configuré → snapshot Hetzner automatique
- B2 / Storage Box configuré → backup off-site réel

Ces items sont dans le backlog. Le sprint actuel adresse la criticité #1 : « si Morty meurt, peut-on revenir en moins de 4 heures ? » — désormais oui, **à condition que les passphrases 1Password soient sauvegardées**.
