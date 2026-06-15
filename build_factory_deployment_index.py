import json
import os
import shutil
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from web3 import Web3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ETHERSCAN_API_KEY = ""  # Falls back to .env / environment variable ETHERSCAN_API_KEY.
RPC_URL = "http://192.168.1.12:8545"
CHAIN_ID = "1"

FACTORIES = [
    "0x0959158b6040D32d04c301A72CBFD6b39E21c9AE",
    "0x0c0e5f2fF0ff18a3be9b835635039256dC4B4963",
    "0x4F8846Ae9380B90d2E71D5e3D042dff3E7ebb40d",
    "0x6A8cbed756804B16E05E741eDaBd5cB544AE21bf",
    "0x98EE851a00abeE0d95D08cF4CA2BdCE32aeaAF7F",
    "0xB9fC157394Af804a3578134A6585C0dc9cc990d4",
    "0xF18056Bbd320E96A48e3Fbf8bC061322531aac99",
]

OUTPUT_PATH = Path("factory_deployment_index.json")
ARTIFACT_DIR = Path("factory_deployment_artifacts")
ABI_CACHE_DIR = ARTIFACT_DIR / "abi_cache"
ENV_PATH = Path(".env")

SAVE_EVERY = 1
PROGRESS_EVERY = 25
ETHERSCAN_SLEEP_SECONDS = 0.22
REQUEST_TIMEOUT_SECONDS = 30
RPC_TIMEOUT_SECONDS = 60
TRACE_TIMEOUT = "60s"

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEPLOY_POOL_FUNCTION_NAMES = {"deploy_pool", "deploy_plain_pool", "deploy_metapool"}

ABI_CACHE = {}
ABI_SOURCES = {}
ABI_STATUSES = {}
SOURCE_CACHE = {}
ABI_COUNTS = Counter()
SOURCE_COUNTS = Counter()
BLOCK_TIMESTAMPS = {}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def log(message):
    print(message, flush=True)


def load_dotenv():
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key():
    load_dotenv()
    return ETHERSCAN_API_KEY or os.environ.get("ETHERSCAN_API_KEY", "")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_datetime(timestamp):
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_hex(value):
    if value is None:
        return "0x"
    if isinstance(value, str):
        return value if value.startswith("0x") else "0x" + value
    if hasattr(value, "hex"):
        text = value.hex()
        return text if text.startswith("0x") else "0x" + text
    return str(value)


def to_jsonable(value):
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray)) or value.__class__.__name__ == "HexBytes":
        return as_hex(value)
    return value


def normalize_address(address):
    if not address:
        return None
    address = str(address).strip()
    if not address or address.lower() == ZERO_ADDRESS:
        return None
    return Web3.to_checksum_address(address)


def lower_address(address):
    address = normalize_address(address)
    return address.lower() if address else None


def count_nonzero_coins(coins):
    return sum(1 for coin in coins or [] if normalize_address(coin))


def function_names(abi):
    return {item.get("name") for item in abi or [] if item.get("type") == "function" and item.get("name")}


def function_is_deploy_pool(name):
    if not name:
        return False
    name = name.lower()
    if name in DEPLOY_POOL_FUNCTION_NAMES:
        return True
    if name.startswith("set_") or name.startswith("update_"):
        return False
    return "pool" in name and ("deploy" in name or "create" in name)


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


# ---------------------------------------------------------------------------
# Output shape / resume helpers
# ---------------------------------------------------------------------------


def empty_output():
    return {
        "metadata": {},
        "factories": {},
        "pools": [],
        "unique_addresses": {
            "implementations": [],
            "math_implementations": [],
            "views_implementations": [],
        },
    }


def load_output():
    return read_json(OUTPUT_PATH, empty_output())


def merge_listed_factory(record, factory, index):
    factory = normalize_address(factory)
    record.setdefault("listed_in_factories", [])
    if factory not in record["listed_in_factories"]:
        record["listed_in_factories"].append(factory)
        record["listed_in_factories"].sort(key=str.lower)
    record.setdefault("factory_list_index_by_factory", {})[factory] = index


def processed_factory_indices(output):
    processed = defaultdict(set)
    for record in output.get("pools", []):
        for factory, index in (record.get("factory_list_index_by_factory") or {}).items():
            processed[normalize_address(factory)].add(int(index))
    return processed


def pool_records_by_address(output):
    records = {}
    for record in output.get("pools", []):
        key = lower_address(record.get("pool_address"))
        if key:
            records[key] = record
    return records


def record_block_number(record):
    details = record.get("deployment_details") or {}
    block_number = details.get("blockNumber")
    return int(block_number) if block_number is not None else 10**18


def record_factory(record):
    details = record.get("deployment_details") or {}
    return normalize_address(details.get("factory"))


def record_function(record):
    details = record.get("deployment_details") or {}
    return details.get("function")


def compact_pool_record(record):
    compact = {
        "pool_address": record.get("pool_address"),
        "listed_in_factories": list(record.get("listed_in_factories") or []),
        "factory_list_index_by_factory": dict(record.get("factory_list_index_by_factory") or {}),
    }

    if record.get("deployment_details"):
        compact["deployment_details"] = {
            key: record["deployment_details"][key]
            for key in ("factory", "txHash", "blockNumber", "timestamp", "block_datetime", "function", "args")
            if key in record["deployment_details"] and record["deployment_details"][key] is not None
        }

    for field in (
        "lp_token_address",
        "implementation_address",
        "math_implementation_address",
        "views_implementation_address",
        "status",
        "unresolved_reason",
        "error",
    ):
        if record.get(field) is not None:
            compact[field] = record[field]
    return compact


def sort_pools(output):
    output["pools"] = sorted(
        [compact_pool_record(record) for record in output.get("pools", [])],
        key=lambda record: (record_block_number(record), record.get("pool_address") or ""),
    )


def summarize_output(output, factories):
    records = output.get("pools", [])
    statuses = Counter(record.get("status", "unknown") for record in records)
    factory_counts = Counter(record_factory(record) for record in records if record_factory(record))
    functions = Counter(record_function(record) for record in records if record_function(record))
    implementations = {lower_address(record.get("implementation_address")) for record in records if record.get("implementation_address")}
    math = {lower_address(record.get("math_implementation_address")) for record in records if record.get("math_implementation_address")}
    views = {lower_address(record.get("views_implementation_address")) for record in records if record.get("views_implementation_address")}

    output["metadata"] = {
        "generated_at": now_utc(),
        "rpc_url": RPC_URL,
        "chain_id": CHAIN_ID,
        "factory_count": len(factories),
        "unique_pool_count": len(records),
        "resolved_count": statuses.get("resolved", 0),
        "unresolved_count": statuses.get("unresolved", 0),
        "conflict_count": statuses.get("conflict", 0),
        "status_counts": dict(statuses),
        "factory_counts": dict(factory_counts),
        "deploy_function_counts": dict(functions),
        "unique_implementation_count": len(implementations),
        "unique_math_implementation_count": len(math),
        "unique_views_implementation_count": len(views),
        "abi_fetch_counts_this_run": dict(ABI_COUNTS),
        "source_fetch_counts_this_run": dict(SOURCE_COUNTS),
        "artifact_dir": str(ARTIFACT_DIR),
    }


def save_output(output, factories):
    sort_pools(output)
    summarize_output(output, factories)
    write_json(OUTPUT_PATH, output)


# ---------------------------------------------------------------------------
# Etherscan / RPC helpers
# ---------------------------------------------------------------------------


def etherscan_get(params):
    params = {"chainid": CHAIN_ID, **params, "apikey": get_api_key()}
    response = requests.get(ETHERSCAN_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    time.sleep(ETHERSCAN_SLEEP_SECONDS)
    return response.json()


def get_abi(address):
    address = normalize_address(address)
    if not address:
        raise ValueError("empty ABI address")
    key = address.lower()
    if key in ABI_CACHE:
        return ABI_CACHE[key], ABI_SOURCES[key], ABI_STATUSES[key]

    ABI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = ABI_CACHE_DIR / f"{key}.json"
    if cache_path.exists():
        abi = json.loads(cache_path.read_text())
        ABI_CACHE[key] = abi
        ABI_SOURCES[key] = str(cache_path)
        ABI_STATUSES[key] = "cached"
        return abi, str(cache_path), "cached"

    data = etherscan_get({"module": "contract", "action": "getabi", "address": address})
    ABI_COUNTS["etherscan_getabi_calls"] += 1
    if data.get("status") != "1":
        ABI_CACHE[key] = None
        ABI_SOURCES[key] = None
        ABI_STATUSES[key] = "unavailable"
        ABI_COUNTS["abis_unavailable"] += 1
        return None, None, "unavailable"

    abi = json.loads(data["result"])
    write_json(cache_path, abi)
    ABI_CACHE[key] = abi
    ABI_SOURCES[key] = str(cache_path)
    ABI_STATUSES[key] = "fetched"
    ABI_COUNTS["abis_fetched"] += 1
    return abi, str(cache_path), "fetched"


def get_contract_creation(address):
    address = normalize_address(address)
    data = etherscan_get({
        "module": "contract",
        "action": "getcontractcreation",
        "contractaddresses": address,
    })
    if data.get("status") != "1" or not data.get("result"):
        return None

    item = dict(data["result"][0])
    item.pop("creationBytecode", None)
    return {
        "contract_creator": normalize_address(item.get("contractCreator")) if item.get("contractCreator") else None,
        "contract_factory": normalize_address(item.get("contractFactory")) if item.get("contractFactory") else None,
        "deployment_tx": item.get("txHash") or item.get("transactionHash"),
        "raw": item,
    }


def get_source(address):
    address = normalize_address(address)
    if not address:
        raise ValueError("empty source address")
    key = address.lower()
    if key in SOURCE_CACHE:
        return SOURCE_CACHE[key]

    data = etherscan_get({"module": "contract", "action": "getsourcecode", "address": address})
    SOURCE_COUNTS["etherscan_getsourcecode_calls"] += 1
    if data.get("status") != "1" or not data.get("result"):
        record = {"address": address, "status": "api_error", "raw": {}}
        SOURCE_CACHE[key] = record
        SOURCE_COUNTS["sources_unavailable"] += 1
        return record

    raw = data["result"][0]
    source_code = raw.get("SourceCode") or ""
    abi_text = raw.get("ABI") or ""
    unavailable = (
        not source_code.strip()
        or source_code.strip().lower() == "contract source code not verified"
        or abi_text.strip().lower() == "contract source code not verified"
    )
    record = {"address": address, "status": "unavailable" if unavailable else "verified", "raw": raw}
    SOURCE_CACHE[key] = record
    if unavailable:
        SOURCE_COUNTS["sources_unavailable"] += 1
    else:
        SOURCE_COUNTS["sources_fetched"] += 1
    return record


def make_w3():
    return Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS}))


def contract_for(w3, address, abi):
    return w3.eth.contract(address=normalize_address(address), abi=abi)


def call_contract(contract, fn_name, args=None, block_number=None):
    fn = getattr(contract.functions, fn_name)(*(args or []))
    if block_number is None:
        return fn.call()
    return fn.call(block_identifier=block_number)


def safe_call_contract(contract, fn_name, args=None, block_number=None):
    try:
        return call_contract(contract, fn_name, args, block_number), None
    except Exception as exc:
        return None, repr(exc)


def block_timestamp(w3, block_number):
    if block_number not in BLOCK_TIMESTAMPS:
        BLOCK_TIMESTAMPS[block_number] = int(w3.eth.get_block(block_number)["timestamp"])
    return BLOCK_TIMESTAMPS[block_number]


def decode_calldata(w3, to_address, input_hex):
    to_address = normalize_address(to_address)
    input_hex = as_hex(input_hex)
    selector = input_hex[:10] if len(input_hex) >= 10 else "0x"
    decoded = {"target": to_address, "selector": selector, "decode_status": "not_decoded"}
    if selector == "0x":
        return decoded

    abi, source, abi_status = get_abi(to_address)
    decoded["abi_status"] = abi_status
    decoded["abi_source"] = source
    if not abi:
        decoded["decode_status"] = "abi_unavailable"
        return decoded

    try:
        contract = contract_for(w3, to_address, abi)
        fn, args = contract.decode_function_input(input_hex)
    except Exception as exc:
        decoded["decode_status"] = "decode_error"
        decoded["decode_error"] = repr(exc)
        return decoded

    decoded["decode_status"] = "decoded"
    decoded["function"] = fn.fn_name
    decoded["args"] = to_jsonable(dict(args))
    return decoded


# ---------------------------------------------------------------------------
# Trace decoding
# ---------------------------------------------------------------------------


def trace_transaction(w3, tx_hash):
    response = w3.provider.make_request(
        "debug_traceTransaction",
        [tx_hash, {"tracer": "callTracer", "timeout": TRACE_TIMEOUT}],
    )
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["result"]


def walk_trace(node, path, out):
    out.append((path, node))
    for index, child in enumerate(node.get("calls") or []):
        walk_trace(child, path + [index], out)


def node_at_path(trace, path):
    node = trace
    for index in path:
        node = node["calls"][index]
    return node


def path_to_string(path):
    return "root" if not path else ".".join(str(item) for item in path)


def find_create_path(trace, pool_address):
    pool = lower_address(pool_address)
    nodes = []
    walk_trace(trace, [], nodes)
    for path, node in nodes:
        if node.get("type") in {"CREATE", "CREATE2"} and lower_address(node.get("to")) == pool:
            return path
    return None


def decode_trace_node(w3, node):
    decoded = {
        "type": node.get("type"),
        "from": normalize_address(node.get("from")) if node.get("from") else None,
        "to": normalize_address(node.get("to")) if node.get("to") else None,
    }
    if not node.get("to") or node.get("type") in {"CREATE", "CREATE2"}:
        return decoded
    decoded.update(decode_calldata(w3, node.get("to"), node.get("input")))
    return decoded


def decoded_path_to_create(w3, trace, create_path):
    decoded = []
    for depth in range(len(create_path) + 1):
        path = create_path[:depth]
        item = decode_trace_node(w3, node_at_path(trace, path))
        item["path"] = path_to_string(path)
        item["depth"] = depth
        decoded.append(item)
    return decoded


def select_deploy_call(decoded_path):
    decoded_calls = [item for item in decoded_path[:-1] if item.get("decode_status") == "decoded"]
    for item in reversed(decoded_calls):
        if function_is_deploy_pool(item.get("function")):
            return item, "matched_deploy_pool_function"
    if decoded_calls:
        return decoded_calls[-1], "nearest_decoded_ancestor"
    return None, "no_decoded_ancestor"


def find_deploy_call(w3, tx, tx_hash, pool_address, factory_set):
    tx_to = normalize_address(tx.get("to")) if tx.get("to") else None
    if tx_to and tx_to.lower() in factory_set:
        decoded = decode_calldata(w3, tx_to, tx.get("input"))
        if decoded.get("decode_status") == "decoded" and function_is_deploy_pool(decoded.get("function")):
            return decoded, None

    trace = trace_transaction(w3, tx_hash)
    create_path = find_create_path(trace, pool_address)
    if create_path is None:
        return None, "pool_create_not_found_in_trace"

    deploy_call, reason = select_deploy_call(decoded_path_to_create(w3, trace, create_path))
    if deploy_call is None:
        return None, reason

    target = normalize_address(deploy_call.get("target") or deploy_call.get("to"))
    if not target or target.lower() not in factory_set:
        return None, "deploy_call_target_not_in_factory_list"
    return deploy_call, None


# ---------------------------------------------------------------------------
# Pool metadata resolution
# ---------------------------------------------------------------------------


def proxy_impl_from_pool_code(w3, pool_address, block_number):
    code = bytes(w3.eth.get_code(normalize_address(pool_address), block_identifier=block_number))
    if len(code) > 200:
        return None
    idx = code.find(bytes.fromhex("73"))
    if idx < 0 or idx + 21 > len(code):
        return None
    candidate = normalize_address("0x" + code[idx + 1 : idx + 21].hex())
    if candidate and len(w3.eth.get_code(candidate)) > 0:
        return candidate
    return None


def add_candidate(candidates, address):
    address = normalize_address(address)
    if address:
        candidates.append(address)


def resolve_implementation(w3, factory, pool_address, function_name, args, block_number):
    candidates = []
    add_candidate(candidates, proxy_impl_from_pool_code(w3, pool_address, block_number))

    abi, _, _ = get_abi(factory)
    if not abi:
        return None, "unresolved", "factory_abi_unavailable"

    contract = contract_for(w3, factory, abi)
    names = function_names(abi)
    idx = int(args.get("_implementation_idx", 0))

    if "get_implementation_address" in names:
        value, error = safe_call_contract(contract, "get_implementation_address", [pool_address], block_number)
        if not error:
            add_candidate(candidates, value)

    selector_candidates = []
    if "implementation_id" in args and "pool_implementations" in names:
        value, error = safe_call_contract(contract, "pool_implementations", [args["implementation_id"]], block_number)
        if not error:
            add_candidate(selector_candidates, value)

    if function_name == "deploy_plain_pool":
        if "pool_implementations" in names:
            value, error = safe_call_contract(contract, "pool_implementations", [idx], block_number)
            if not error:
                add_candidate(selector_candidates, value)
        if "plain_implementations" in names:
            n_coins = count_nonzero_coins(args.get("_coins", []))
            values, error = safe_call_contract(contract, "plain_implementations", [n_coins], block_number)
            if not error and values is not None and idx < len(values):
                add_candidate(selector_candidates, values[idx])

    if function_name == "deploy_metapool" and "metapool_implementations" in names:
        value, error = safe_call_contract(contract, "metapool_implementations", [idx], block_number)
        if not error:
            add_candidate(selector_candidates, value)
        elif args.get("_base_pool"):
            base_pool = normalize_address(args["_base_pool"])
            values, error = safe_call_contract(contract, "metapool_implementations", [base_pool], block_number)
            if not error and values is not None and idx < len(values):
                add_candidate(selector_candidates, values[idx])

    if selector_candidates:
        candidates.extend(selector_candidates)
    elif "pool_implementation" in names:
        value, error = safe_call_contract(contract, "pool_implementation", [], block_number)
        if not error:
            add_candidate(candidates, value)

    unique = sorted({lower_address(item): item for item in candidates}.values(), key=str.lower)
    if len(unique) == 1:
        return unique[0], "resolved", None
    if not unique:
        return None, "unresolved", "implementation_unresolved"
    return None, "conflict", "implementation_conflict"


def resolve_lp_token(w3, factory, pool_address, block_number):
    abi, _, _ = get_abi(factory)
    if abi and "get_token" in function_names(abi):
        token = call_contract(contract_for(w3, factory, abi), "get_token", [pool_address], block_number)
        return normalize_address(token)
    return normalize_address(pool_address)


def resolve_aux_implementations(w3, factory, block_number):
    abi, _, _ = get_abi(factory)
    if not abi:
        return {}

    contract = contract_for(w3, factory, abi)
    names = function_names(abi)
    result = {}
    for field, fn_name in (
        ("math_implementation_address", "math_implementation"),
        ("views_implementation_address", "views_implementation"),
    ):
        if fn_name not in names:
            continue
        value, error = safe_call_contract(contract, fn_name, [], block_number)
        address = normalize_address(value) if not error else None
        if address:
            result[field] = address
    return result


# ---------------------------------------------------------------------------
# Source artifacts
# ---------------------------------------------------------------------------


def source_extension(compiler_version, source_text):
    compiler = (compiler_version or "").lower()
    if compiler.startswith("vyper") or "vyper:" in compiler:
        return ".vy"
    if "# @version" in (source_text or "")[:500]:
        return ".vy"
    return ".sol"


def safe_relative_source_path(raw_path, default_extension):
    raw_path = (raw_path or "source").replace("\\", "/")
    parts = []
    for part in raw_path.split("/"):
        part = part.strip().replace("\x00", "")
        if part and part not in {".", ".."}:
            parts.append(part)
    if not parts:
        parts = ["source"]
    if "." not in parts[-1]:
        parts[-1] += default_extension
    return "/".join(parts)


def parse_json_source_payload(source_code):
    text = (source_code or "").strip()
    if not text:
        return None
    candidates = [text]
    if text.startswith("{{") and text.endswith("}}"):
        candidates.append(text[1:-1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except Exception:
                return None
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_etherscan_source_files(source_code, contract_name, compiler_version):
    if not source_code:
        return []
    default_extension = source_extension(compiler_version, source_code)
    parsed = parse_json_source_payload(source_code)
    if parsed and isinstance(parsed.get("sources"), dict):
        files = []
        for raw_path, item in parsed["sources"].items():
            content = item.get("content", "") if isinstance(item, dict) else str(item)
            files.append({"path": safe_relative_source_path(raw_path, default_extension), "content": content})
        return files
    return [{"path": safe_relative_source_path(contract_name or "source", default_extension), "content": source_code}]


def write_source_files(out_dir, source_record):
    source_dir = out_dir / "source"
    if source_dir.exists():
        shutil.rmtree(source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)

    raw = source_record.get("raw") or {}
    files = parse_etherscan_source_files(
        raw.get("SourceCode") or "",
        raw.get("ContractName"),
        raw.get("CompilerVersion"),
    )
    written = []
    for item in files:
        path = source_dir / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.get("content", ""))
        written.append(str(Path("source") / item["path"]))
    return written


def write_abi(out_dir, address, source_record):
    raw = source_record.get("raw") or {}
    abi_text = raw.get("ABI") or ""
    if abi_text.strip().startswith("["):
        abi = json.loads(abi_text)
        write_json(out_dir / "abi.json", abi)
        return "source_getsourcecode", "etherscan_getsourcecode"

    abi, source, status = get_abi(address)
    if abi:
        write_json(out_dir / "abi.json", abi)
    return status, source


def address_artifact_dir(kind, address):
    return ARTIFACT_DIR / kind / normalize_address(address)


def source_metadata(raw):
    keys = [
        "ContractName",
        "CompilerVersion",
        "OptimizationUsed",
        "Runs",
        "ConstructorArguments",
        "EVMVersion",
        "Library",
        "LicenseType",
        "Proxy",
        "Implementation",
        "SwarmSource",
    ]
    return {key: raw.get(key) for key in keys if key in raw}


def collect_usage(records, field):
    usage = {}
    for record in records:
        address = normalize_address(record.get(field))
        if not address:
            continue
        item = usage.setdefault(
            address,
            {
                "used_by_pool_count": 0,
                "first_seen_pool": record.get("pool_address"),
                "first_seen_factory": record_factory(record),
                "first_seen_block": record_block_number(record),
                "example_pools": [],
            },
        )
        item["used_by_pool_count"] += 1
        if len(item["example_pools"]) < 5:
            item["example_pools"].append(record.get("pool_address"))
        block_number = record_block_number(record)
        if block_number < item["first_seen_block"]:
            item["first_seen_block"] = block_number
            item["first_seen_pool"] = record.get("pool_address")
            item["first_seen_factory"] = record_factory(record)
    return usage


def write_address_artifact(kind, address, usage):
    address = normalize_address(address)
    out_dir = address_artifact_dir(kind, address)
    out_dir.mkdir(parents=True, exist_ok=True)

    old_runtime_code = out_dir / "runtime_code.hex"
    if old_runtime_code.exists():
        old_runtime_code.unlink()

    source_record = get_source(address)
    source_status = source_record.get("status")
    source_files = []
    if source_status == "verified":
        source_files = write_source_files(out_dir, source_record)
    elif (out_dir / "source").exists():
        shutil.rmtree(out_dir / "source")

    abi_status, abi_source = write_abi(out_dir, address, source_record)
    metadata = {
        "address": address,
        "kind": kind.rstrip("s"),
        "written_at": now_utc(),
        "abi_status": abi_status,
        "abi_source": abi_source,
        "source_status": source_status,
        "source_source": "etherscan_getsourcecode",
        "source_files": source_files,
        "source_file_count": len(source_files),
        "etherscan_source_metadata": source_metadata(source_record.get("raw") or {}),
        **usage,
    }
    write_json(out_dir / "metadata.json", metadata)
    return {
        "address": address,
        "used_by_pool_count": usage.get("used_by_pool_count", 0),
        "artifact_dir": str(out_dir),
        "abi_status": abi_status,
        "source_status": source_status,
        "source_file_count": len(source_files),
        "source_files": source_files,
    }


def refresh_unique_artifacts(output):
    output["unique_addresses"] = {
        "implementations": [],
        "math_implementations": [],
        "views_implementations": [],
    }
    for kind, field in (
        ("implementations", "implementation_address"),
        ("math_implementations", "math_implementation_address"),
        ("views_implementations", "views_implementation_address"),
    ):
        for address, usage in sorted(collect_usage(output.get("pools", []), field).items(), key=lambda item: item[0].lower()):
            output["unique_addresses"][kind].append(write_address_artifact(kind, address, usage))


# ---------------------------------------------------------------------------
# Main indexing
# ---------------------------------------------------------------------------


def get_pool_count(w3, factory):
    abi, source, status = get_abi(factory)
    if not abi:
        raise RuntimeError(f"factory ABI unavailable for {factory}: {status} {source}")
    contract = contract_for(w3, factory, abi)
    return int(call_contract(contract, "pool_count")), contract


def build_pool_record(w3, pool_address, listed_factory, factory_index, factory_set):
    pool_address = normalize_address(pool_address)
    listed_factory = normalize_address(listed_factory)
    record = {
        "pool_address": pool_address,
        "listed_in_factories": [listed_factory],
        "factory_list_index_by_factory": {listed_factory: factory_index},
    }

    creation = get_contract_creation(pool_address)
    if not creation or not creation.get("deployment_tx"):
        record["status"] = "unresolved"
        record["unresolved_reason"] = "contract_creation_tx_unavailable"
        return record

    tx_hash = creation["deployment_tx"]
    tx = w3.eth.get_transaction(tx_hash)
    block_number = int(tx["blockNumber"])
    timestamp = block_timestamp(w3, block_number)

    deploy_call, error = find_deploy_call(w3, tx, tx_hash, pool_address, factory_set)
    if not deploy_call:
        record["status"] = "unresolved"
        record["unresolved_reason"] = error or "deploy_call_not_found"
        return record

    factory = normalize_address(deploy_call.get("target") or deploy_call.get("to"))
    function_name = deploy_call.get("function")
    args = deploy_call.get("args") or {}

    record["deployment_details"] = {
        "factory": factory,
        "txHash": tx_hash,
        "blockNumber": str(block_number),
        "timestamp": str(timestamp),
        "block_datetime": utc_datetime(timestamp),
        "function": function_name,
        "args": args,
    }
    record["lp_token_address"] = resolve_lp_token(w3, factory, pool_address, block_number)

    implementation, status, reason = resolve_implementation(w3, factory, pool_address, function_name, args, block_number)
    record["status"] = status
    if reason:
        record["unresolved_reason"] = reason
    if implementation:
        record["implementation_address"] = implementation
        record.update(resolve_aux_implementations(w3, factory, block_number))
    return record


def process_factory(w3, factory, output, processed_indices, records_by_pool, factory_set, factories):
    factory = normalize_address(factory)
    if not factory:
        raise ValueError("empty factory address")
    pool_count, contract = get_pool_count(w3, factory)
    output.setdefault("factories", {})[factory] = {
        "pool_count": pool_count,
        "abi_status": ABI_STATUSES.get(factory.lower()),
        "abi_source": ABI_SOURCES.get(factory.lower()),
        "last_scanned_at": now_utc(),
    }

    changed = 0
    log(f"factory={factory} pool_count={pool_count}")
    for index in range(pool_count):
        if index in processed_indices.get(factory, set()):
            continue

        pool_address = normalize_address(call_contract(contract, "pool_list", [index]))
        existing = records_by_pool.get(lower_address(pool_address))
        if existing:
            merge_listed_factory(existing, factory, index)
            changed += 1
        else:
            record = build_pool_record(w3, pool_address, factory, index, factory_set)
            output["pools"].append(compact_pool_record(record))
            records_by_pool[lower_address(pool_address)] = output["pools"][-1]
            changed += 1
            if changed % PROGRESS_EVERY == 0 or record.get("status") != "resolved":
                log(f"  index={index} changed={changed} status={record.get('status')} pool={pool_address}")

        processed_indices[factory].add(index)
        if changed % SAVE_EVERY == 0:
            save_output(output, factories)

    return changed


def main():
    if not get_api_key():
        raise RuntimeError("Set ETHERSCAN_API_KEY at the top of this script, in .env, or in the environment")

    w3 = make_w3()
    if not w3.is_connected():
        raise RuntimeError(f"RPC is not connected: {RPC_URL}")

    factories = [normalize_address(factory) for factory in FACTORIES]
    if any(factory is None for factory in factories):
        raise ValueError("FACTORIES contains an empty or zero address")
    factory_set = {factory.lower() for factory in factories}
    output = load_output()
    output.setdefault("pools", [])
    output.setdefault("factories", {})
    output.setdefault("unique_addresses", {"implementations": [], "math_implementations": [], "views_implementations": []})

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    processed_indices = processed_factory_indices(output)
    records_by_pool = pool_records_by_address(output)
    total_changed = 0

    log(f"factories={len(factories)} existing_records={len(records_by_pool)}")
    for factory in factories:
        total_changed += process_factory(w3, factory, output, processed_indices, records_by_pool, factory_set, factories)
        save_output(output, factories)

    if total_changed == 0:
        log("no new factory indices")
        return

    log("refreshing unique implementation/math/views artifacts")
    refresh_unique_artifacts(output)
    save_output(output, factories)

    log("\nwritten:")
    log(f"output={OUTPUT_PATH}")
    log(f"artifacts={ARTIFACT_DIR}")
    log(f"resolved={output['metadata']['resolved_count']}")
    log(f"unresolved={output['metadata']['unresolved_count']}")
    log(f"conflicts={output['metadata']['conflict_count']}")
    log(f"unique_implementations={output['metadata']['unique_implementation_count']}")
    log(f"unique_math_implementations={output['metadata']['unique_math_implementation_count']}")
    log(f"unique_views_implementations={output['metadata']['unique_views_implementation_count']}")


if __name__ == "__main__":
    main()
