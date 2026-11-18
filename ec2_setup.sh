#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# EC2 Setup Script for IDP_Prod on Amazon Linux 2023 (g4dn.xlarge / Tesla T4)
# Based on official AWS documentation:
# https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/public-nvidia-driver.html
#
# Run as ec2-user with sudo privileges.
# Usage: bash ec2_setup.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

echo "========================================================"
echo " Step 1: Install NVIDIA Drivers (if not already present)"
echo "========================================================"

# Check if nvidia-smi is already working
if nvidia-smi &>/dev/null; then
    echo "[OK] NVIDIA driver already installed."
    nvidia-smi
else
    echo "[INFO] Installing NVIDIA drivers using the official AWS/AL2023 method..."

    # 1. Install exact kernel headers matching the running kernel
    echo "[INFO] Installing kernel headers for: $(uname -r)"
    sudo dnf install -y kernel-devel-$(uname -r) kernel-headers-$(uname -r)

    # 2. Add the NVIDIA CUDA repo for AL2023
    echo "[INFO] Adding NVIDIA CUDA repo..."
    sudo dnf config-manager --add-repo \
        https://developer.download.nvidia.com/compute/cuda/repos/amzn2023/x86_64/cuda-amzn2023.repo

    # 3. Install CUDA toolkit (driver-only packages come via the module below)
    echo "[INFO] Installing CUDA toolkit..."
    sudo dnf clean all
    sudo dnf install -y cuda-toolkit

    # 4. Install the NVIDIA open kernel module driver — THE correct AL2023 command
    #    (This is a DNF module, not a regular package — hence 'dnf module install')
    echo "[INFO] Installing NVIDIA open kernel module driver (open-dkms)..."
    sudo dnf module install -y nvidia-driver:open-dkms

    # 5. Enable nvidia-persistenced (keeps GPU initialized between jobs)
    sudo systemctl enable nvidia-persistenced || true

    echo ""
    echo "[OK] NVIDIA driver installation complete."
    echo "     !! A REBOOT IS REQUIRED before the driver will activate !!"
    echo ""
    echo "     Run:  sudo reboot"
    echo "     Then SSH back in and re-run:  cd ~/IDP_Prod && bash ec2_setup.sh"
    exit 0
fi

echo ""
echo "========================================================"
echo " Step 2: Install NVIDIA Container Toolkit"
echo "========================================================"

if command -v nvidia-ctk &>/dev/null; then
    echo "[OK] NVIDIA Container Toolkit already installed."
else
    echo "[INFO] Adding NVIDIA Container Toolkit repo..."
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
        | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo

    sudo dnf install -y nvidia-container-toolkit
    echo "[OK] NVIDIA Container Toolkit installed."
fi

echo ""
echo "========================================================"
echo " Step 3: Configure Docker to use NVIDIA runtime"
echo "========================================================"

sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
echo "[OK] Docker restarted with NVIDIA runtime."

echo ""
echo "========================================================"
echo " Step 4: Fix db_init directory permissions"
echo "========================================================"

if [ -d "./db_init" ]; then
    sudo chmod -R 755 ./db_init
    sudo chown -R 999:999 ./db_init
    echo "[OK] db_init permissions fixed (owner: postgres uid 999)."
else
    echo "[WARN] ./db_init directory not found. Make sure you are in the IDP_Prod folder."
fi

echo ""
echo "========================================================"
echo " Step 5: Fix Redis memory overcommit warning"
echo "========================================================"

if grep -q "vm.overcommit_memory = 1" /etc/sysctl.conf; then
    echo "[OK] vm.overcommit_memory already configured."
else
    echo "vm.overcommit_memory = 1" | sudo tee -a /etc/sysctl.conf
    sudo sysctl vm.overcommit_memory=1
    echo "[OK] Redis memory overcommit fix applied."
fi

echo ""
echo "========================================================"
echo " Step 6: Verify NVIDIA GPU is accessible to Docker"
echo "========================================================"

echo "[INFO] Testing NVIDIA Docker GPU access..."
if docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi; then
    echo ""
    echo "[OK] GPU is accessible from Docker containers!"
else
    echo ""
    echo "[WARN] GPU test failed. Try:"
    echo "       sudo nvidia-ctk runtime configure --runtime=docker"
    echo "       sudo systemctl restart docker"
    echo "       Then run this test again:"
    echo "       docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi"
fi

echo ""
echo "========================================================"
echo " ALL DONE! Launch your IDP stack with:"
echo " docker compose up -d --build"
echo "========================================================"
