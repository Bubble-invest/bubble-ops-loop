# Disaster Recovery — Clé age de Morty

**Statut :** opérationnel depuis le sprint sécurité 2026-05-21.
**Risque adressé :** perte de `/etc/age/key.txt` sur Morty rend la totalité des secrets SOPS (env files, clé privée de l'App GitHub `bubble-ops-bot`) **inrécupérable**.

---

## 1. Le problème en une phrase

`/etc/age/key.txt` fait 184 octets, est en mode 400 root, et n'a aucune copie nulle part. C'est la racine de la chaîne SOPS pour toute la stack Bubble Invest. Si Morty meurt, tous les fichiers `*.sops.env` et `*.sops.pem` du repo `bubble-vps-data` deviennent du bruit chiffré sans clé.

## 2. La parade

On re-chiffre la clé age **symétriquement** avec une passphrase humaine (saisie par {{OPERATOR}}), via `age --encrypt --passphrase`. Le ciphertext qui en résulte est un fichier ASCII-armored qu'on commit dans `projects/bubble-vps-data/disaster-recovery/age-key-morty.age`. Trois propriétés en sortent :

1. **Le clair ne touche jamais le disque** — il transite uniquement dans le pipe `ssh hetzner 'sudo cat /etc/age/key.txt' | age --encrypt --passphrase --armor`.
2. **Le ciphertext est commit-safe** — sans la passphrase, c'est inexploitable. La passphrase est stockée en 1Password par {{OPERATOR}}.
3. **Le backup voyage avec le repo** — tous les clones de `bubble-vps-data` (Mac de {{OPERATOR}}, Morty, GitHub privé) portent la copie. Plus de single point of failure.

## 3. Faire le backup (étape unique, à refaire si la clé age change)

```bash
cd ~/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
bash scripts/backup-age-key.sh
```

`age` demande la passphrase deux fois (saisie cachée). Stocke-la **immédiatement** dans 1Password sous le nom `age-key-morty backup passphrase`.

Le script ne commit pas. Toi, oui :

```bash
cd ~/claude-workspaces/Rick_RnD/projects/bubble-vps-data
git add disaster-recovery/age-key-morty.age
git commit -m "disaster-recovery: backup age key (passphrase-encrypted)"
git push
```

## 4. Restaurer la clé sur un Morty neuf

Tu as provisionné un nouveau VPS Hetzner CX33 (cf. `docs/DISASTER-RECOVERY.md`, étape 1). Tu as ajouté ta clé SSH. Reste à remettre `/etc/age/key.txt` en place.

**Chemin canonique (un seul pipe, jamais de fichier en clair) :**

```bash
cd ~/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
bash scripts/restore-age-key.sh \
    | ssh root@<new-morty-ip> 'install -m 400 /dev/stdin /etc/age/key.txt'
```

`age` prompte la passphrase sur `/dev/tty` (donc tu la saisis sur ton terminal local). Le clair sort sur stdout, traverse le pipe SSH, et `install -m 400` l'écrit directement sur Morty avec les bons droits (root:root, 400). Pas de fichier temporaire.

## 5. Vérification post-restauration

Sur le nouveau Morty :

```bash
ssh root@<new-morty-ip> 'ls -la /etc/age/key.txt && wc -c /etc/age/key.txt'
# attendu : -r--------  1 root root  184 ... /etc/age/key.txt
#           184 /etc/age/key.txt
```

Puis vérifier qu'un secret SOPS se déchiffre :

```bash
ssh root@<new-morty-ip> \
    'SOPS_AGE_KEY_FILE=/etc/age/key.txt sops -d /etc/bubble/secrets.sops.env | head -3'
```

Si tu vois les variables d'environnement attendues, la chaîne SOPS est rétablie.

## 6. Si tu perds la passphrase

Le backup devient inutilisable. C'est le seul scénario où on ne peut pas récupérer.

**Mitigations préventives** (à faire MAINTENANT, pas après l'incident) :
- Stocker la passphrase en 1Password (compte {{OPERATOR}} + compte {{OPERATOR_2}} en partage).
- Imprimer la passphrase sur papier et la mettre dans le coffre.
- Faire un drill de restauration tous les 3 mois (cf. `docs/DISASTER-RECOVERY.md` §5).

## 7. Quand re-faire le backup ?

- Si la clé age est rotée (`age-keygen -o /etc/age/key.txt` avec re-chiffrement de tous les secrets SOPS).
- Tous les 6 mois en routine (drill de vérification).

Le fichier `age-key-morty.age` doit alors être ré-écrit par `backup-age-key.sh` avec la même passphrase (ou une nouvelle, si {{OPERATOR}} décide de la roter aussi — dans ce cas, mettre à jour 1Password).
