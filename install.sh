#!/bin/sh
# Install the Bonsai 2 27B runtime, model, launcher and Claude Code subagent.
# Safe to re-run: every step checks for its own result first.
#
# BONSAI_MLX=pypi (default) installs the stock mlx wheel pinned in requirements.txt.
# BONSAI_MLX=fork builds the PrismML fork from source. On an M5 Pro it measured
# 2.8x slower at prompt processing, because the fork turns off the NAX path.
set -eu

BONSAI_HOME="${BONSAI_HOME:-$HOME/.local/share/bonsai}"
BONSAI_MLX="${BONSAI_MLX:-pypi}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
MLX_SRC="$BONSAI_HOME/mlx-src"
VENV="$BONSAI_HOME/venv"
MODEL_DIR="$BONSAI_HOME/model"
MLX_BRANCH="v0-31-2-prism-macos27"

# PrismML-Eng/mlx v0.31.2_prism: upstream 0.31.2 plus the 1-bit kernels. The
# prism branch is older than mlx-lm 0.31.3 needs (no new_thread_local_stream).
FORK_BASE=e3e5339182497487258313e7104d3de1665d9bed
# Fork commit: M5-class GPUs computed wrong results on the NAX gemm/qmm path.
NAX_GATE=4446b4e6f544ad3bd02771389c445449856c78b6
# Upstream ml-explore/mlx#3963: Metal 4.1 (Xcode 27) rejects implicit `thread`.
METAL41_FIX=ef5fc0fab27c63e6976e96b07c234c9e2601d8a7

die() {
    echo "install: $*" >&2
    exit 1
}

prepare_fork_source() {
    command -v git >/dev/null || die "git not found"
    xcrun metal --version >/dev/null 2>&1 ||
        die "Metal Toolchain missing. Run: xcodebuild -downloadComponent MetalToolchain"
    if [ ! -d "$MLX_SRC/.git" ]; then
        git clone https://github.com/PrismML-Eng/mlx.git "$MLX_SRC"
        git -C "$MLX_SRC" remote add upstream https://github.com/ml-explore/mlx.git
    fi
    if ! git -C "$MLX_SRC" rev-parse --verify "refs/heads/$MLX_BRANCH" >/dev/null 2>&1; then
        git -C "$MLX_SRC" fetch origin v0.31.2_prism prism
        git -C "$MLX_SRC" fetch upstream main
        git -C "$MLX_SRC" switch -c "$MLX_BRANCH" "$FORK_BASE"
        git -C "$MLX_SRC" -c user.name=install -c user.email=install@localhost \
            cherry-pick -x "$NAX_GATE" "$METAL41_FIX"
    fi
    git -C "$MLX_SRC" switch "$MLX_BRANCH"
}

[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || die "Apple Silicon macOS only"
command -v uv >/dev/null || die "uv not found: https://docs.astral.sh/uv/"

mkdir -p "$BONSAI_HOME/logs"
[ -x "$VENV/bin/python" ] || uv venv --python 3.12 "$VENV"

case "$BONSAI_MLX" in
pypi)
    uv pip install --python "$VENV/bin/python" -r "$REPO_DIR/requirements.txt"
    ;;
fork)
    prepare_fork_source
    # The override replaces the mlx pin in requirements.txt with the fork build.
    OVERRIDE="$BONSAI_HOME/mlx-override.txt"
    echo "mlx @ file://$MLX_SRC" >"$OVERRIDE"
    uv pip install --python "$VENV/bin/python" --override "$OVERRIDE" \
        -r "$REPO_DIR/requirements.txt"
    ;;
*)
    die "BONSAI_MLX must be pypi or fork, got $BONSAI_MLX"
    ;;
esac

if [ ! -f "$MODEL_DIR/model.safetensors" ]; then
    uvx --from huggingface_hub hf download \
        prism-ml/Ternary-Bonsai-2-27B-mlx-2bit --local-dir "$MODEL_DIR"
fi

mkdir -p "$HOME/.local/bin" "$HOME/.claude/agents"
ln -sf "$REPO_DIR/bonsai-ask" "$HOME/.local/bin/bonsai-ask"
ln -sf "$REPO_DIR/agents/bonsai.md" "$HOME/.claude/agents/bonsai.md"

"$VENV/bin/python" -c "import mlx.core as mx; print('mlx', mx.__version__, mx.default_device())"
echo "Installed. Try: bonsai-ask --no-think 'What is 17 * 23?'"
