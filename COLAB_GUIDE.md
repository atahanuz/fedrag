# Google Colab from the CLI

Notes on driving Colab sessions with `google-colab-cli` (0.6.0): unlocking the 80GB
A100, running vLLM in a session, and the environment quirks that bite along the way.

- [High-RAM sessions (80GB A100)](#high-ram-sessions-80gb-a100)
- [Running vLLM in a session](#running-vllm-in-a-session)
- [Session environment gotchas](#session-environment-gotchas)

## High-RAM sessions (80GB A100)

The released `google-colab-cli` (0.6.0) **cannot** request a high-RAM runtime — there is
no flag, no env var, no hidden option. `colab new --gpu A100` always gives you the
40GB A100. This documents a two-line local patch that unlocks the 80GB one.

### Usage

```bash
COLAB_HIGH_MEM=1 colab new --gpu A100
```

Omit the variable to get the normal 40GB A100. Verified output inside the session:

```text
NVIDIA A100-SXM4-80GB, 81920 MiB
SystemRAM_GB: 179.4
```

| | Standard | High-RAM |
| --- | --- | --- |
| GPU VRAM | 40 GB | **80 GB** |
| System RAM | ~83 GB | **179 GB** |

Works with `--gpu A100`, `H100`, `T4`, `G4`, and CPU sessions.
**Not** with `--gpu L4` or the TPUs (`v5e1`, `v6e1`) — those have a single machine
shape. The patch sends the parameter blindly, so just don't combine them.

Since `colab new` is the only patched entry point, `colab run` and `colab ssh` still
create standard-shape VMs regardless of the env var.

### Verifying which shape you got

`colab sessions` prints `Hardware: A100` for both shapes, so it can't tell them apart.
Use the endpoint hostname instead — high-RAM sessions carry an `-hm` segment:

```text
[hm-test] gpu-a100-hm-kkb-ass1c1-... | Hardware: A100 | Variant: GPU
              ^^
```

Or check directly inside the session:

```bash
echo 'import subprocess; print(subprocess.run(["nvidia-smi","--query-gpu=name,memory.total","--format=csv,noheader"],capture_output=True,text=True).stdout)' > /tmp/probe.py
colab exec -s SESSION_NAME -f /tmp/probe.py
```

A `ReadTimeout` on the first `colab exec` right after `Session READY` is normal — the
kernel is still booting. Just retry.

### The patch

Applied to `/Users/atahanuz/miniconda3/lib/python3.12/site-packages/colab_cli/client.py`.
A pristine copy is saved beside it as `client.py.orig`.

Add `os` to the imports, then in `Client._build_assign_url`:

```python
        if accelerator:
            params["accelerator"] = accelerator.value
+       if os.environ.get("COLAB_HIGH_MEM"):
+           params["shape"] = "hm"

        req = requests.Request("GET", url, params=params)
```

This appends `&shape=hm` to the `/tun/m/assign` request. With the variable unset the
generated URL is byte-identical to stock, so nothing else changes.

To revert:

```bash
cp /Users/atahanuz/miniconda3/lib/python3.12/site-packages/colab_cli/client.py{.orig,}
```

### ⚠️ Upgrading silently breaks this

`pip install --upgrade google-colab-cli` overwrites the patched file. It will **not**
error — you'll just quietly get 40GB A100s again, and an OOM much later is the first
symptom. Re-apply the patch after any upgrade, or check for the `-hm` hostname segment.

### Upstream status

Real support is an unmerged PR: [googlecolab/google-colab-cli#105](https://github.com/googlecolab/google-colab-cli/pull/105)
(opened 2026-08-10), tracked by [issue #47](https://github.com/googlecolab/google-colab-cli/issues/47).
It adds a proper `--high-mem` flag to `colab new`, `colab run`, and `colab ssh`, and
sends the same `shape=hm` parameter — so once it ships, revert the backup and use:

```bash
colab new --gpu A100 --high-mem
```

The CLI already has a `Shape` enum with `HIGH_RAM = 1` in `client.py`, but in 0.6.0 it
is only used to *parse* the shape of existing sessions, never to request one.

## Running vLLM in a session

Verified 2026-08-15 on a high-RAM A100 with vLLM 0.27.1 and `google/gemma-4-12B-it`:
about 4.5 minutes from launch to first answer, 23.3 GiB of weights and 43 GiB of KV
cache at `gpu_memory_utilization=0.85`.

### Install, then repair the torch stack

```bash
pip install -U vllm
pip uninstall -y torchaudio   # REQUIRED — see below
```

vLLM pulls **torch 2.13.0+cu130**, but Colab preinstalls **torchaudio 2.11.0+cu128**.
`transformers` imports torchaudio unconditionally when the package is present, and
torchaudio hard-errors on a CUDA version mismatch, so *any* `import transformers`
dies with:

```text
RuntimeError: Detected that PyTorch and TorchAudio were compiled with different CUDA
versions. PyTorch has CUDA version 13.0 whereas TorchAudio has CUDA version 12.8.
```

There is no matching cu130 torchaudio wheel yet — installing from
`--index-url https://download.pytorch.org/whl/cu130` succeeds but resolves to the same
cu128 build, so it does not help. Uninstalling is the fix; vLLM never needs torchaudio.
torchvision is fine on its own (`0.28.0+cu130` after the upgrade).

The wall of `pip's dependency resolver` conflicts about `cudf`, `cuml`, `rmm`, and
`cuda-python` is pre-existing RAPIDS pinning in the Colab image. It does not affect
vLLM — ignore it.

### Check the model is supported before waiting on a download

```python
from vllm.model_executor.models.registry import ModelRegistry
archs = set(ModelRegistry.get_supported_archs())
print("Gemma4UnifiedForConditionalGeneration" in archs)
```

Match this against `architectures` in the model's `config.json`. Ungated models
(`gated: False` from `https://huggingface.co/api/models/<repo>`) need no HF token —
the `You are sending unauthenticated requests to the HF Hub` warning only means
lower rate limits.

### Launch detached and poll

A model load easily outlives an `colab exec` round-trip, so never block on it. Have the
exec'd script spawn the real work with `start_new_session=True`, log to a file, and
write the result somewhere pollable:

```python
import subprocess, sys

runner = '''
import json, traceback
from vllm import LLM, SamplingParams
try:
    llm = LLM(model="google/gemma-4-12B-it", dtype="bfloat16",
              gpu_memory_utilization=0.85, max_model_len=4096)
    out = llm.chat([{"role": "user", "content": "What is 2+2?"}],
                   SamplingParams(temperature=0.0, max_tokens=128))
    json.dump({"ok": True, "answer": out[0].outputs[0].text}, open("/content/result.json", "w"))
except Exception as e:
    json.dump({"ok": False, "tb": traceback.format_exc()}, open("/content/result.json", "w"))
'''
open("/content/run.py", "w").write(runner)

log = open("/content/vllm.log", "w")
p = subprocess.Popen([sys.executable, "-u", "/content/run.py"],
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
print("launched pid", p.pid)
```

Then poll with a second script that tails `/content/vllm.log`, prints
`/content/result.json` if it exists, and checks `nvidia-smi`. If a run seems stuck,
`pip install py-spy` and `py-spy dump --pid $(pgrep -f run.py)` shows where it actually
is — progress bars in the log go quiet during non-GPU phases and look like a hang.

Note that the process **exits after generating**, releasing the GPU. Keep the
interpreter alive (or serve `vllm serve` behind a port) if you want the weights to
stay resident across questions.

## Session environment gotchas

**`colab exec` reuses one persistent kernel.** Every exec runs in the same interpreter,
so imports, module-level caches, and `lru_cache` results survive between calls. This
bites hardest after changing packages: uninstalling torchaudio did not help until the
check ran in a fresh process, because `transformers` had already cached
`is_torchaudio_available() == True` from an earlier exec. Anything sensitive to
installs must run via `subprocess.run([sys.executable, "-c", ...])`, not directly in
the exec'd script.

**Stop a session when done.** The command is `stop` — there is no `colab delete`:

```bash
colab stop -s SESSION_NAME
```

**First exec after `Session READY` may `ReadTimeout`.** The kernel is still booting.
Retry.

**Long jobs need detaching.** See the vLLM launch pattern above; it applies to any
multi-minute workload.
