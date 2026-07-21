# Debian 12 host bootstrap release

This is the effective implementation of the host-bootstrap section of frozen Runbook 01 under
ADR 0004. It supports Debian 12 Bookworm/aarch64 on Oracle Cloud only. It never enables Binance
transport or installs a production credential.

## Owner-controlled inputs

Create `/root/aiq-bootstrap-inputs` with mode `0700`. The following four inputs stay outside the
repository:

1. `aiqops_authorized_key.pub`: exactly one Ed25519 SSH **public** key for the operator account.
   Keep the matching private key on the operator computer; do not upload or paste it into chat.
2. `bootstrap-owner-ed25519.pub.pem`: the Ed25519 approval **public** key in PEM format. Keep the
   matching private key off the server.
3. `off-host-backup-evidence.json`: a closed JSON object naming a real, at-most-24-hour-old OCI boot
   volume backup, encrypted off-host archive, or Git remote backup. `repository_head` must equal the
   committed release HEAD. An OCI example is:

   ```json
   {"schema_version":"1.0.0","provider":"oci-boot-volume-backup","artifact_id":"<OCI_BACKUP_OCID>","created_at":"<UTC_RFC3339_Z>","repository_head":"<GIT_COMMIT_SHA>"}
   ```

4. `bootstrap-approval.json`: create this only after reviewing the exact plan. It expires within
   one hour and is cryptographically bound to the plan hash.

The only private material used by this phase is the operator SSH private key and owner approval
private key. Both remain off-host. No Binance, Telegram, OpenAI, database or archive secret belongs
in these files.

## Plan

After the current repository HEAD has an off-host backup, run from the existing SSH session:

```bash
cd /root/quantify/ai-quant-system
./scripts/bootstrap-host.sh plan \
  --toolchain-lock deploy/host-toolchain.lock.yaml \
  --hardening-dir deploy/host-hardening \
  --ssh-port 22 \
  --ssh-source-cidr 198.51.100.164/32 \
  --operator-public-key /root/aiq-bootstrap-inputs/aiqops_authorized_key.pub \
  --approval-public-key /root/aiq-bootstrap-inputs/bootstrap-owner-ed25519.pub.pem \
  --off-host-backup-evidence /root/aiq-bootstrap-inputs/off-host-backup-evidence.json \
  --recovery-console-confirmed \
  --output /root/aiq-bootstrap-inputs/bootstrap-plan.json
sha256sum /root/aiq-bootstrap-inputs/bootstrap-plan.json
```

`plan` validates the exact apt metadata, controlled artifacts, hardening manifest, current SSH
source, OCI host identity, Docker configuration, nftables syntax and sshd syntax. It is read-only.

Review the plan on the trusted operator computer and create the approval there:

```bash
./scripts/create_bootstrap_approval.py \
  --plan bootstrap-plan.json \
  --private-key '<OFF_HOST_OWNER_PRIVATE_KEY>' \
  --approver '<OWNER_ID>' \
  --expires-minutes 30 \
  --output bootstrap-approval.json
```

Upload only `bootstrap-approval.json` to `/root/aiq-bootstrap-inputs`.

## Two-stage apply and SSH proof

The first apply installs exact tools, creates accounts/directories and installs the operator public
key. It deliberately exits with status 10 before changing SSH or firewall policy:

```bash
./scripts/bootstrap-host.sh apply \
  --plan /root/aiq-bootstrap-inputs/bootstrap-plan.json \
  --approval /root/aiq-bootstrap-inputs/bootstrap-approval.json
```

Open a second session as `aiqops` with the operator private key. In that second session run:

```bash
/root/quantify/ai-quant-system/scripts/bootstrap-host.sh prove-ssh \
  --plan /root/aiq-bootstrap-inputs/bootstrap-plan.json \
  --output /home/aiqops/bootstrap-ssh-proof.json
```

The root session then completes apply using that fresh, UID-bound proof:

```bash
./scripts/bootstrap-host.sh apply \
  --plan /root/aiq-bootstrap-inputs/bootstrap-plan.json \
  --approval /root/aiq-bootstrap-inputs/bootstrap-approval.json \
  --ssh-proof /home/aiqops/bootstrap-ssh-proof.json
./scripts/bootstrap-host.sh verify \
  --plan /root/aiq-bootstrap-inputs/bootstrap-plan.json \
  --output /var/lib/ai-quant/evidence/bootstrap-evidence.json
```

Final apply copies the existing Docker data root without deleting the old copy, activates UTC,
chrony, Docker/journald/sysctl/limits hardening, changes SSH to source-bound key-only `aiqops`, and
adds a separate default-drop nftables input table. Oracle Cloud console rollback commands are
included in the verification evidence.
