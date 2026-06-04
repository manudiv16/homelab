# Ansible — Homelab Cluster Provisioning

This directory contains everything needed to provision the k3s cluster on Proxmox LXC containers using [k3s-ansible](https://github.com/timothystewart6/k3s-ansible).

## Structure

```
ansible/
├── ansible.cfg                    # Points to my-cluster inventory
├── inventory/
│   └── my-cluster/
│       ├── hosts.ini              # Node definitions
│       └── group_vars/
│           ├── all.yml            # Cluster config (k3s, MetalLB, etc.)
│           └── proxmox.yml        # Proxmox SSH user
└── k3s-ansible/                   # Git submodule (upstream playbooks + roles)
```

## Cluster Nodes

| Role       | Hostname       | IP             | CT ID | RAM  | CPU |
|------------|----------------|----------------|-------|------|-----|
| Master     | k3s-master-1   | 192.168.4.201  | 200   | 2 GB | 2   |
| Worker     | k3s-worker-1   | 192.168.4.202  | 201   | 3 GB | 2   |
| Worker     | k3s-worker-2   | 192.168.4.203  | 202   | 3 GB | 2   |
| Proxmox    | proxmox        | 192.168.4.83   | —     | —    | —   |

## Usage

### Prerequisites
- Ansible installed locally
- SSH key access to Proxmox (`root@192.168.4.83`)
- SSH key access to LXC nodes (`ansibleuser@192.168.4.20x`)
- k3s-ansible submodule: `git submodule update --init`

### Deploy / Upgrade the cluster

```bash
cd ansible
ansible-playbook k3s-ansible/site.yml
```

### Reset the cluster

```bash
cd ansible
ansible-playbook k3s-ansible/reset.yml
```

### Kubeconfig

After deployment, the kubeconfig is at `~/.kube/config.k3s-homelab`.
Set with: `export KUBECONFIG=~/.kube/config.k3s-homelab`

## Key Config

- **k3s**: v1.35.5+k3s1 (stable)
- **CNI**: Flannel (eth0)
- **LoadBalancer**: MetalLB v0.15.3 (Layer2, range 192.168.4.210-220)
- **Ingress**: Traefik disabled (deploy separately via ArgoCD)
- **Storage**: local-path-provisioner (default)
- **LXC**: Privileged containers, apparmor unconfined

## Secrets

The k3s token and any sensitive values are not committed. To set them:

1. Uncomment `k3s_token` in `inventory/my-cluster/group_vars/all.yml`
2. Or use `ansible-vault encrypt inventory/my-cluster/group_vars/all.yml`
