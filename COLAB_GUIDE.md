# Google Colab from the CLI

Notes on driving Colab sessions with `google-colab-cli` (0.6.0): unlocking the 80GB
A100, running vLLM in a session, and the environment quirks that bite along the way.

- [High-RAM sessions (80GB A100)](#high-ram-sessions-80gb-a100)
- [Running vLLM in a session](#running-vllm-in-a-session)
- [Session environment gotchas](#session-environment-gotchas)
- [Why sessions die, and how to keep them alive](#why-sessions-die-and-how-to-keep-them-alive)

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

Applied to `colab_cli/client.py` in the Python environment that runs `colab` (find it with
`python -c "import colab_cli.client as c; print(c.__file__)"`). A pristine copy is saved beside
it as `client.py.orig`.

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
F=$(python -c "import colab_cli.client as c; print(c.__file__)")
cp "$F.orig" "$F"
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

**Update 2026-10-04:** it shipped. Per its source, `google-colab-cli` 0.7.x (0.7.4 on PyPI) has
`colab new --high-mem`, so upgrading no longer costs you the 80GB A100 as long as you switch to the
flag. 0.7.x also refreshes the runtime-proxy token by itself, which fixes the one-hour session loss
described in [Why sessions die](#why-sessions-die-and-how-to-keep-them-alive). It also removes the
keep-alive daemon, which never prevented the idle reclaim anyway. Not yet run here: this repo still
uses 0.6.0 with the patch.

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

## Why sessions die, and how to keep them alive

Measured on 2026-10-04 with `google-colab-cli` 0.6.0 on T4 and A100 sessions, plus the CLI's own
history of about 60 earlier sessions (`~/.config/colab-cli/history/*.jsonl`). There are two separate
causes, and each needs its own fix.

### 1. The CLI drops the session after one hour

Each session talks to its VM with a runtime-proxy token, a JWT whose `exp` is one hour after it was
issued (`"tokenExpiresInSeconds": 3600`). 0.6.0 stores the token at `colab new` and never refreshes it.
After an hour the next `colab exec`, `upload` or `download` gets a 401. The CLI then prints
`Session 'X' appears to be lost (404/401). Cleaning up.`, deletes its record of the session and kills
the keep-alive daemon. **The VM is fine and still billing**: `colab sessions` lists it as `[?]`, and
Colab reclaims it about 20 minutes later because nothing talks to it any more. About half of the 60
sessions in the history ended this way, almost all of them 60-62 minutes after creation.

Fix: every assignment listing (`GET /tun/m/assignments`, which `colab sessions` calls) mints a fresh
token for each running assignment, so swap one into the session record before the old one expires.
colab-cli 0.7.x does this for every command. For 0.6.0, `scripts/colab_keepalive.py` does it
(`ensure_token`), and it can also re-adopt a VM whose record the CLI has already deleted.

### 2. Colab reclaims an idle VM after about 20 minutes

A GPU VM disappears 20-22 minutes after the last kernel-websocket traffic through the Colab proxy,
however busy the VM itself is. Measured on T4 sessions, timing from the last `colab exec`:

| During the idle period | Result |
| --- | --- |
| nothing (control) | gone after 22.3 min |
| the CLI's keep-alive daemon pinging `/tun/m/<endpoint>/keep-alive/` every 60 s (HTTP 200) | no effect (it ran in every session in this table) |
| `GET /api/kernels` through the runtime proxy every 4 min (HTTP 200) | gone after 20.7 min |
| kernel busy with a never-ending cell (`while True: time.sleep(30)`) | gone after 21.7 min |
| a process on the VM running `1+1` on its kernel through `localhost:8080` every 4 min | gone after 22.2 min |
| (A100, earlier) vLLM serving requests through a Cloudflare tunnel, GPU busy | gone 22.5 min after the last exec |
| a client connecting to the kernel websocket every 4.5 min (`kernel_info` handshake, no code) | alive at 28 min (stopped) |
| a client holding **one** kernel websocket open, no code at all | alive at 68 min (stopped), 8 min after its token expired |
| the VM holding a websocket to its own kernel through its **public** proxy URL | alive at 86 min (stopped): one connection, still open 26 min after its token expired |
| `gpu_server/keeper.py` (the previous row, driven by a usage policy), 35 min of use, then none | held the VM with no client at all, let go 5 min after use stopped; gone 58.5 min after the last client contact |
| (A100 80GB, the whole fedrag stack) keeper only, the gateway used through Cloudflare every 5 min | alive 26 min after the last client contact (stopped) |
| `colab exec` every few minutes | alive (dozens of A100 sessions in the history, until the CLI dropped them at 60 min) |

What the table shows:

- Colab's idle timer only counts kernel websocket connections through its proxy. An open connection is
  enough, with no code running. That is how a browser tab or VS Code keeps a runtime alive, and why a
  kernel busy with a long cell does not keep a CLI session alive. Colab's VS Code extension states the
  same limits in its source: an idle server "is reclaimed within ~30 minutes and none outlive 24 hours".
- The token is only checked when a connection opens. An open websocket outlives it.
- It does not matter where the connection comes from. A VM can keep itself alive by connecting to its
  own public proxy URL, as long as it has a token from the client to open that connection.
- CPU runtimes have a much longer idle window: a CPU control lasted 182 minutes after its last exec.

Fixes, in order of independence from your laptop:

1. **In-VM keeper.** `gpu_server/keeper.py` holds a websocket from the VM to a kernel of its own through
   the public proxy, using a token that the client uploads to `/content/colab_proxy.json`. It closes the
   connection when its policy says the VM is no longer in use, so an unused VM is still released. The
   VM then survives the client sleeping or going offline. fedrag's `launch.py` starts it with a "gateway
   used in the last 60 minutes" policy. For any other session:

   ```bash
   python scripts/colab_keepalive.py -s NAME --keeper-while train.py   # while it runs, plus 30 min
   python scripts/colab_keepalive.py -s NAME --keeper-hours 6
   python scripts/colab_keepalive.py -s NAME --keeper-release          # Colab reclaims it ~20 min later
   ```

2. **Client heartbeat.** `python scripts/colab_keepalive.py -s NAME --detach` runs a no-op cell every
   4 minutes and keeps the CLI's token fresh (and the keeper's, if one is installed). It works for any
   session with no setup on the VM, but without a keeper the VM is lost if the client machine sleeps or
   goes offline for more than ~20 minutes. On macOS the heartbeat therefore holds a `caffeinate`
   assertion while it runs.

Opening a kernel websocket through the proxy sometimes takes more than 10 seconds (often enough on a
far-away VM such as `asia-southeast1`). The CLI then gives up with a traceback, and the next attempt
usually connects in under a second, so retry `colab exec` failures (`vm_exec` in
`scripts/colab_keepalive.py` does).
