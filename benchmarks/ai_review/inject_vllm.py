import json, sys

root = sys.argv[1]
manifest = []


def read(rel):
    s = open(f"{root}/{rel}", encoding="utf-8", newline="").read()
    return s, ("\r\n" in s)


def write(rel, s, crlf):
    s = s.replace("\r\n", "\n")
    open(f"{root}/{rel}", "w", encoding="utf-8", newline="").write(s.replace("\n", "\r\n") if crlf else s)


def edit(rel, old, new, **meta):
    s, crlf = read(rel)
    s = s.replace("\r\n", "\n")
    assert s.count(old) == 1, (rel, old[:60], s.count(old))
    out = s.replace(old, new, 1)
    write(rel, out, crlf)
    manifest.append({"file": rel, "line": out[: out.index(new)].count("\n") + 1, **meta})


# 1. a later redefinition shadows the real cdiv (last def wins at import time)
rel = "vllm/utils/math_utils.py"
s, crlf = read(rel)
s = s.replace("\r\n", "\n")
assert s.rstrip().endswith("return values[i] if i < len(values) else None")
add = '''


# Fast paths for non-negative operands.
def cdiv(a: int, b: int) -> int:
    """Ceiling division for non-negative operands (avoids the double negation above)."""
    return (a + b) // b
'''
body = s.rstrip("\n") + "\n"
write(rel, body + add, crlf)
manifest.append({"file": rel, "line": body.count("\n") + 4, "kind": "shadowing redefinition",
                 "what": "second `def cdiv` at the bottom of the module overrides the correct one; (a+b)//b is off by one whenever a is an exact multiple of b (cdiv(4,2)==3, cdiv(0,2)==1)",
                 "symptom": "every block/page/size computation that uses cdiv (320 modules) over-allocates or mis-sizes"})

# 2. a second init_logger defined later in the module replaces the real one
edit("vllm/logger.py",
     '''def _trace_calls(log_path, root_dir, frame, event, arg=None):''',
     '''def init_logger(name: str) -> _VllmLogger:
    """Retrieve a logger (the logging module caches it, so repeated calls are cheap)."""
    return cast(_VllmLogger, logging.getLogger(name))


def _trace_calls(log_path, root_dir, frame, event, arg=None):''',
     kind="function override of a factory",
     what="a later `def init_logger` replaces the real one and no longer patches in vLLM's convenience methods (info_once, warning_once, debug_once, ...)",
     symptom="AttributeError the first time any module calls logger.*_once; init_logger(__name__) itself still works so imports succeed")

# 3. lexicographic version comparison
edit("vllm/utils/torch_version.py",
     '''    return version.parse(torch_version) >= version.parse(target)''',
     '''    # Compare the release part only; avoids building two Version objects per call.
    return torch_version.split("+")[0] >= target''',
     kind="wrong comparison semantics",
     what="version check became a string comparison: '2.13.0' >= '2.6.0' is False because '1' < '6'",
     symptom="every is_torch_equal_or_newer(...) gate (15 modules) treats torch >= 2.10 as older than 2.6-2.9 and takes legacy/fallback paths")

# 4. unit constant slip (1000 MiB instead of 1024 MiB)
edit("vllm/utils/mem_constants.py",
     'GiB_bytes = 1 << 30\n',
     'GiB_bytes = 1_000 * MiB_bytes\n',
     kind="numeric constant slip",
     what="GiB_bytes is 1000 MiB (1,048,576,000) instead of 2**30 (1,073,741,824), 2.3% short",
     symptom="every GiB-denominated memory budget/profile (15 modules) is silently off; KV cache sizing and OOM margins drift")

# 5. prefix-cache block hash silently drops extra_keys
edit("vllm/v1/core/kv_cache_utils.py",
     '''    curr_block_token_ids_tuple = tuple(curr_block_token_ids)
    return BlockHash(
        hash_function((parent_block_hash, curr_block_token_ids_tuple, extra_keys))
    )''',
     '''    curr_block_token_ids_tuple = tuple(curr_block_token_ids)
    # extra_keys are folded into the parent hash by the caller, so only the
    # parent and the tokens need hashing here.
    return BlockHash(hash_function((parent_block_hash, curr_block_token_ids_tuple)))''',
     kind="silent correctness loss behind a misleading comment",
     what="block hash ignores extra_keys (LoRA id, multimodal hashes, cache salt) while the signature and docstring still advertise them; the comment's claim is false",
     symptom="prefix cache hits across different LoRA adapters / images / salts: wrong KV reused, silently wrong outputs")

json.dump(manifest, open(sys.argv[2], "w"), indent=2)
print(len(manifest), "breaks injected")
