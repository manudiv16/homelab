# Homelab

Kubernetes homelab managed with ArgoCD (App of Apps pattern) on k3s.

## Structure

```
├── bootstrap/
│   └── argocd/          # ArgoCD installation + app-of-apps bootstrap
├── apps/                # ArgoCD Application definitions (app-of-apps)
│   └── infrastructure-base.yaml
└── infrastructure/
    └── base/            # Infrastructure-level manifests (namespaces, CRDs, etc.)
```

## Bootstrap

```bash
# Set kubeconfig
export KUBECONFIG=~/.kube/config.k3s-homelab

# Install ArgoCD
kubectl apply -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Update repoURL in bootstrap and apps manifests
# Then bootstrap the app-of-apps
kubectl apply -f bootstrap/argocd/app-of-apps.yaml
```

## Adding a new app

1. Add manifests under `apps/` or a new directory
2. Create an ArgoCD Application YAML in `apps/`
3. The app-of-apps will automatically pick it up via `recurse: true`
