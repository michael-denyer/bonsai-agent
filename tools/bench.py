#!/usr/bin/env python3
"""Measure Bonsai decode and prefill speed, and gate correctness against a reference.

Run with the Bonsai venv python. One JSON object goes to stdout; progress to stderr.

    bench.py --save-ref ref.npz    # trusted runtime: record reference outputs, measure
    bench.py --ref ref.npz         # candidate runtime: compare, then measure
"""

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

import mlx.core as mx
import numpy as np

BONSAI_HOME = Path(os.environ.get("BONSAI_HOME") or Path.home() / ".local/share/bonsai")
MODEL_DIR = BONSAI_HOME / "model"

GATE_PROMPTS = (
    "What is 17 * 23? Reply with the number only.",
    "Name the capital of Australia and one fact about it.",
)
DECODE_PROMPT = (
    "Write a long, detailed explanation of how a B-tree index works in a database,"
    " covering node structure, search, insertion, splitting and deletion."
)
FILLER = (
    "The quick brown fox jumps over the lazy dog while the engineer reviews the"
    " quarterly storage report and notes every anomaly in the replication log. "
)


def log(message):
    print(message, file=sys.stderr, flush=True)


def chat(processor, text):
    return processor.tokenizer.apply_chat_template(
        [{"role": "user", "content": text}],
        add_generation_prompt=True,
        tokenize=False,
        enable_thinking=False,
    )


def filler_prompt(processor, tokens):
    """Build a chat prompt of roughly the requested token count."""
    per_copy = len(processor.tokenizer.encode(FILLER))
    body = FILLER * max(1, tokens // per_copy)
    return chat(processor, body + "\nSummarise the text above in one sentence.")


def last_logits(model, processor, prompt):
    ids = mx.array([processor.tokenizer.encode(prompt)])
    logits = model.language_model(ids).logits[:, -1, :].astype(mx.float32)
    mx.eval(logits)
    return np.asarray(logits)[0]


def gate(model, processor, generate, ref_path, save_path):
    """Compare logits and greedy text with the reference, or record the reference."""
    logits = {
        "short": last_logits(model, processor, chat(processor, GATE_PROMPTS[0])),
        "long": last_logits(model, processor, filler_prompt(processor, 512)),
        "xlong": last_logits(model, processor, filler_prompt(processor, 4096)),
    }
    texts = [
        generate(
            model,
            processor,
            chat(processor, p),
            max_tokens=96,
            temperature=0.0,
            verbose=False,
        ).text
        for p in GATE_PROMPTS
    ]
    if save_path:
        np.savez(save_path, texts=np.array(texts), **logits)
        return {"saved": str(save_path)}
    ref = np.load(ref_path)
    report = {"texts_identical": texts == list(ref["texts"])}
    for name, got in logits.items():
        want = ref[name]
        report[f"{name}_max_abs_diff"] = float(np.max(np.abs(got - want)))
        report[f"{name}_argmax_same"] = bool(got.argmax() == want.argmax())
        report[f"{name}_top5_same"] = bool(
            (np.argsort(got)[-5:] == np.argsort(want)[-5:]).all()
        )
    report["pass"] = bool(
        report["texts_identical"]
        and all(report[f"{n}_argmax_same"] and report[f"{n}_top5_same"] for n in logits)
    )
    return report


def measure(model, processor, generate, runs):
    def median_of(prompt, max_tokens, field):
        samples = []
        for _ in range(runs):
            result = generate(
                model,
                processor,
                prompt,
                max_tokens=max_tokens,
                temperature=0.0,
                verbose=False,
            )
            samples.append(getattr(result, field))
            log(
                f"  {field} {samples[-1]:.2f} ({result.prompt_tokens}p/{result.generation_tokens}g)"
            )
        return {
            "median": round(statistics.median(samples), 2),
            "min": round(min(samples), 2),
            "max": round(max(samples), 2),
        }

    metrics = {}
    log("decode, short context, 300 tokens")
    metrics["decode_tps"] = median_of(
        chat(processor, DECODE_PROMPT), 300, "generation_tps"
    )
    log("decode after a 4k prompt, 100 tokens")
    long_prompt = filler_prompt(processor, 4096)
    metrics["decode_at_4k_tps"] = median_of(long_prompt, 100, "generation_tps")
    log("prefill 512")
    metrics["prefill_512_tps"] = median_of(
        filler_prompt(processor, 512), 1, "prompt_tps"
    )
    log("prefill 4k")
    metrics["prefill_4k_tps"] = median_of(long_prompt, 1, "prompt_tps")
    metrics["peak_memory_gib"] = round(mx.get_peak_memory() / 2**30, 2)
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", type=Path, help="reference .npz to compare against")
    parser.add_argument(
        "--save-ref", type=Path, help="record the reference instead of comparing"
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--gate-only", action="store_true")
    args = parser.parse_args()
    if not (args.ref or args.save_ref):
        parser.error("give --ref or --save-ref")

    sys.path.insert(0, str(MODEL_DIR / "runtime"))
    from mlx_vlm import generate
    from vision_artifact import load_vl_model

    model, processor, _ = load_vl_model(str(MODEL_DIR))

    out = {
        "mlx": mx.__version__,
        "gate": gate(model, processor, generate, args.ref, args.save_ref),
    }
    if not args.gate_only:
        out["metrics"] = measure(model, processor, generate, args.runs)
    print(json.dumps(out, indent=2))
    return 0 if out["gate"].get("pass", True) else 1


if __name__ == "__main__":
    sys.exit(main())
