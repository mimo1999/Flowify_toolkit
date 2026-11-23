import importlib, sys, os, warnings
warnings.filterwarnings("ignore")
root = sys.argv[1]
sys.path.insert(0, root)
for m in [k for k in sys.modules if k == "vllm" or k.startswith("vllm.")]:
    del sys.modules[m]
import vllm.utils.math_utils as mu, vllm.utils.mem_constants as mc, vllm.utils.torch_version as tv
import vllm.logger as lg
out = {}
out["1 cdiv(4,2) / cdiv(0,2) / cdiv(5,2)"] = (mu.cdiv(4, 2), mu.cdiv(0, 2), mu.cdiv(5, 2))
lgr = lg.init_logger("probe")
out["2 logger has info_once"] = hasattr(lgr, "info_once")
import torch
out["3 torch %s: is_torch_equal_or_newer('2.6.0')" % torch.__version__.split("+")[0]] = tv.is_torch_equal_or_newer("2.6.0")
out["4 GiB_bytes == 2**30"] = mc.GiB_bytes == 2**30
try:
    import vllm.v1.core.kv_cache_utils as ku
    from vllm.utils.hashing import sha256
    a = ku.hash_block_tokens(sha256, None, [1, 2, 3, 4], ("lora-A",))
    b = ku.hash_block_tokens(sha256, None, [1, 2, 3, 4], ("lora-B",))
    out["5 different extra_keys give different hashes"] = a != b
except Exception as e:
    out["5 (kv_cache_utils import failed)"] = repr(e)[:120]
for k, v in out.items():
    print(f"  {k}: {v}")
