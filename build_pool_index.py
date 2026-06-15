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

OUTPUT_PATH = Path("pool_index.json")
HARD_CODED_PATH = Path("hardcoded_pool_deployment_index.json")
CACHE_DIR = Path("cache")
ABI_CACHE_DIR = CACHE_DIR / "abi_cache"
POOL_IMPL_DIR = Path("pool_implementations")
MATH_IMPL_DIR = Path("math_implementations")
VIEWS_IMPL_DIR = Path("views_implementations")
ENV_PATH = Path(".env")

SAVE_EVERY = 1
PROGRESS_EVERY = 25
ETHERSCAN_SLEEP_SECONDS = 0.22
REQUEST_TIMEOUT_SECONDS = 30
RPC_TIMEOUT_SECONDS = 60
TRACE_TIMEOUT = "60s"

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
NATIVE_ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
DEPLOY_POOL_FUNCTION_NAMES = {"deploy_pool", "deploy_plain_pool", "deploy_metapool"}

QUOTE_OR_SIMULATION_SKIP_NAMES = {
    "get_dy",
    "get_dy_underlying",
    "get_dx",
    "get_dx_underlying",
    "calc_withdraw_one_coin",
    "calc_withdraw_fixed_out",
    "calc_token_amount",
    "calc_token_fee",
    "fee_calc",
    "get_twap_balances",
}
ACCOUNT_SPECIFIC_SKIP_NAMES = {
    "balanceOf",
    "allowance",
    "nonces",
    "lp_allowlist",
}
LOADER_SKIP_REASONS = {
    **{name: "quote_or_simulation" for name in QUOTE_OR_SIMULATION_SKIP_NAMES},
    **{name: "account_specific" for name in ACCOUNT_SPECIFIC_SKIP_NAMES},
}
INDEXED_GETTER_CATEGORIES = {
    "coins(uint256)": "pool_coin",
    "coins(int128)": "pool_coin",
    "balances(uint256)": "pool_balance",
    "balances(int128)": "pool_balance",
    "admin_balances(uint256)": "pool_balance",
    "previous_balances(uint256)": "pool_balance",
    "oracle(uint256)": "pool_param",
    "price_scale(uint256)": "pool_param",
    "price_oracle(uint256)": "pool_param",
    "last_prices(uint256)": "pool_param",
    "last_price(uint256)": "pool_param",
    "ema_price(uint256)": "pool_param",
    "get_p(uint256)": "pool_param",
    "base_coins(uint256)": "reference_indexed_state",
    "BASE_COINS(uint256)": "reference_indexed_state",
    "underlying_coins(uint256)": "reference_indexed_state",
    "underlying_coins(int128)": "reference_indexed_state",
}
N_COINS_MINUS_1_GETTERS = {
    "price_scale(uint256)",
    "price_oracle(uint256)",
    "last_prices(uint256)",
    "last_price(uint256)",
    "ema_price(uint256)",
    "get_p(uint256)",
}
BASE_N_COINS_GETTERS = {
    "base_coins(uint256)",
    "BASE_COINS(uint256)",
}
PAIRWISE_GETTER_CATEGORIES = {
    "dynamic_fee(int128,int128)": "pool_param",
}

ERC20_STRING_ABI = [
    {"name": "name", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
]
ERC20_BYTES32_ABI = [
    {"name": "name", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bytes32"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bytes32"}]},
]

ABI_CACHE = {}
ABI_SOURCES = {}
ABI_STATUSES = {}
SOURCE_CACHE = {}
TOKEN_METADATA_CACHE = {}
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


def now_utc_plain():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_datetime(timestamp):
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_datetime_plain(timestamp):
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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


def abi_types(items):
    return [item["type"] for item in items]


def abi_function_signature(item):
    return f"{item['name']}({','.join(abi_types(item['inputs']))})"


def abi_state_mutability(item):
    if "stateMutability" in item:
        return item["stateMutability"]
    if item["constant"]:
        return "view"
    if item["payable"]:
        return "payable"
    return "nonpayable"


def abi_function_selector(signature):
    return Web3.keccak(text=signature)[:4].hex()


def sort_manifest_entries(entries):
    return sorted(entries, key=lambda entry: (entry["signature"], entry["name"]))


def build_loader_call_manifest(abi):
    calls = []
    skipped = []
    unknown_view_functions = []

    for item in abi:
        if item["type"] != "function":
            continue
        state_mutability = abi_state_mutability(item)
        if state_mutability not in ("view", "pure"):
            continue

        name = item["name"]
        inputs = abi_types(item["inputs"])
        outputs = abi_types(item["outputs"])
        signature = abi_function_signature(item)
        selector = abi_function_selector(signature)

        if name in LOADER_SKIP_REASONS:
            skipped.append({
                "signature": signature,
                "selector": selector,
                "name": name,
                "inputs": inputs,
                "reason": LOADER_SKIP_REASONS[name],
            })
        elif not inputs:
            calls.append({
                "signature": signature,
                "selector": selector,
                "name": name,
                "inputs": inputs,
                "outputs": outputs,
                "stateMutability": state_mutability,
                "arg_policy": "none",
                "category": "pool_param",
            })
        elif signature in INDEXED_GETTER_CATEGORIES:
            if signature in N_COINS_MINUS_1_GETTERS:
                arg_policy = "range:n_coins_minus_1"
            elif signature in BASE_N_COINS_GETTERS:
                arg_policy = "range:base_n_coins"
            else:
                arg_policy = "range:n_coins"
            calls.append({
                "signature": signature,
                "selector": selector,
                "name": name,
                "inputs": inputs,
                "outputs": outputs,
                "stateMutability": state_mutability,
                "arg_policy": arg_policy,
                "category": INDEXED_GETTER_CATEGORIES[signature],
            })
        elif signature in PAIRWISE_GETTER_CATEGORIES:
            calls.append({
                "signature": signature,
                "selector": selector,
                "name": name,
                "inputs": inputs,
                "outputs": outputs,
                "stateMutability": state_mutability,
                "arg_policy": "pairs:n_coins",
                "category": PAIRWISE_GETTER_CATEGORIES[signature],
            })
        else:
            unknown_view_functions.append({
                "signature": signature,
                "selector": selector,
                "name": name,
                "inputs": inputs,
                "outputs": outputs,
                "stateMutability": state_mutability,
            })

    return {
        "generated_at": now_utc(),
        "calls": sort_manifest_entries(calls),
        "skipped": sort_manifest_entries(skipped),
        "unknown_view_functions": sort_manifest_entries(unknown_view_functions),
    }


# ---------------------------------------------------------------------------
# Output shape / resume helpers
# ---------------------------------------------------------------------------


def empty_output():
    return {
        "metadata": {},
        "factories": {},
        "pools": [],
        "unique_addresses": {
            "pool_implementations": [],
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


def record_deployment_block_number(record):
    details = record.get("deployment_details") or {}
    block_number = details.get("blockNumber")
    return int(block_number) if block_number is not None else None


def compact_deployment_details(details):
    compact = {
        key: details[key]
        for key in ("factory", "txHash", "blockNumber", "timestamp", "block_datetime", "block_datetime_utc", "function")
        if key in details and details[key] is not None
    }
    if compact.get("timestamp") is not None:
        compact.setdefault("block_datetime", utc_datetime(compact["timestamp"]))
        compact.setdefault("block_datetime_utc", utc_datetime_plain(compact["timestamp"]))
    if "args" in details and details["args"] is not None:
        compact["args"] = dict(details["args"] or {})
    return compact


DERIVED_POOL_FIELDS = (
    "name",
    "symbol",
    "coins",
    "underlying_coins",
    "n_coins",
    "base_pool_address",
    "is_meta_pool",
    "pool_type",
    "registry_id",
    "implementation_type",
    "asset_type",
    "asset_type_name",
    "asset_types",
    "initial_A",
    "fee",
    "admin_fee",
    "gamma",
    "mid_fee",
    "out_fee",
    "allowed_extra_profit",
    "fee_gamma",
    "adjustment_step",
    "offpeg_fee_multiplier",
    "ma_exp_time",
    "ma_half_time",
    "method_id",
    "method_ids",
    "oracle",
    "oracles",
    "weth_address",
    "initial_price",
    "initial_prices",
)


def compact_pool_record(record):
    compact = {"pool_address": record.get("pool_address")}

    for field in DERIVED_POOL_FIELDS:
        if field in record and record[field] is not None:
            compact[field] = record[field]

    compact["listed_in_factories"] = list(record.get("listed_in_factories") or [])
    compact["factory_list_index_by_factory"] = dict(record.get("factory_list_index_by_factory") or {})

    if record.get("deployment_details"):
        compact["deployment_details"] = compact_deployment_details(record["deployment_details"])

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
        "schema_version": 2,
        "generated_at": now_utc(),
        "generated_at_utc": now_utc_plain(),
        "rpc_url": RPC_URL,
        "chain_id": CHAIN_ID,
        "factory_count": len(factories),
        "unique_pool_count": len(records),
        "hardcoded_pool_count": sum(1 for r in records if not r.get("listed_in_factories")),
        "resolved_count": statuses.get("resolved", 0),
        "unresolved_count": statuses.get("unresolved", 0),
        "conflict_count": statuses.get("conflict", 0),
        "status_counts": dict(statuses),
        "factory_counts": dict(factory_counts),
        "deploy_function_counts": dict(functions),
        "unique_pool_implementation_count": len(implementations),
        "unique_math_implementation_count": len(math),
        "unique_views_implementation_count": len(views),
        "abi_fetch_counts_this_run": dict(ABI_COUNTS),
        "source_fetch_counts_this_run": dict(SOURCE_COUNTS),
        "cache_dir": str(CACHE_DIR),
        "implementation_dirs": {
            "pool_implementations": str(POOL_IMPL_DIR),
            "math_implementations": str(MATH_IMPL_DIR),
            "views_implementations": str(VIEWS_IMPL_DIR),
        },
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


def clean_token_text(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value.rstrip("\x00").strip() or None
    if isinstance(value, (bytes, bytearray)) or value.__class__.__name__ == "HexBytes":
        return bytes(value).rstrip(b"\x00").decode("utf-8", errors="replace").strip() or None
    return str(value).strip() or None


def erc20_text(w3, address, fn_name, block_number=None):
    for abi in (ERC20_STRING_ABI, ERC20_BYTES32_ABI):
        contract = contract_for(w3, address, abi)
        value, error = safe_call_contract(contract, fn_name, [], block_number)
        if not error:
            return clean_token_text(value)
    return None


def token_metadata(w3, address, block_number=None):
    address = normalize_address(address)
    if not address:
        return {}
    if address.lower() == NATIVE_ETH_ADDRESS.lower():
        return {"symbol": "ETH", "name": "Ether", "decimals": "18"}

    key = address.lower()
    if key not in TOKEN_METADATA_CACHE:
        contract = contract_for(w3, address, ERC20_STRING_ABI)
        decimals, error = safe_call_contract(contract, "decimals", [], block_number)
        TOKEN_METADATA_CACHE[key] = {
            "symbol": erc20_text(w3, address, "symbol", block_number),
            "name": erc20_text(w3, address, "name", block_number),
            "decimals": str(decimals) if not error and decimals is not None else None,
        }
    return dict(TOKEN_METADATA_CACHE[key])


def deployment_args(record):
    return (record.get("deployment_details") or {}).get("args") or {}


def arg(args, *names):
    for name in names:
        if name in args:
            return args[name]
    return None


def set_value(record, field, value):
    if value is not None:
        record[field] = value


def make_coin(w3, value, block_number=None, is_base_pool_lp_token=False):
    existing = value if isinstance(value, dict) else {}
    address = normalize_address(existing.get("address") if existing else value)
    if not address:
        return None

    missing_metadata = any(existing.get(key) in (None, "") for key in ("symbol", "name", "decimals"))
    metadata = token_metadata(w3, address, block_number) if missing_metadata else {}
    decimals = existing.get("decimals") or metadata.get("decimals")
    return {
        "address": address,
        "symbol": existing.get("symbol") or metadata.get("symbol"),
        "name": existing.get("name") or metadata.get("name"),
        "decimals": str(decimals) if decimals is not None else None,
        "isBasePoolLpToken": bool(existing.get("isBasePoolLpToken", is_base_pool_lp_token)),
    }


def make_coins(w3, values, block_number=None, decimals=None):
    coins = []
    for index, value in enumerate(values or []):
        coin = make_coin(w3, value, block_number)
        if not coin:
            continue
        if coin.get("decimals") is None and decimals and index < len(decimals):
            coin["decimals"] = str(decimals[index])
        coins.append(coin)
    return coins


def pool_base_address(record):
    args = deployment_args(record)
    return normalize_address(args.get("_base_pool") or record.get("base_pool_address"))


def pool_coins(w3, record, records_by_pool):
    args = deployment_args(record)
    block_number = record_deployment_block_number(record)

    if record.get("coins"):
        return make_coins(w3, record["coins"], block_number, args.get("_decimals"))

    if args.get("_coin") and pool_base_address(record):
        base_pool = pool_base_address(record)
        base_record = records_by_pool.get(lower_address(base_pool), {})
        base_lp_token = base_record.get("lp_token_address") or base_pool
        return [
            coin for coin in (
                make_coin(w3, args["_coin"], block_number),
                make_coin(w3, base_lp_token, block_number, is_base_pool_lp_token=True),
            ) if coin
        ]

    return make_coins(w3, args.get("_coins") or [], block_number, args.get("_decimals"))


def underlying_coins(w3, record, records_by_pool):
    block_number = record_deployment_block_number(record)
    if record.get("underlying_coins"):
        return make_coins(w3, record["underlying_coins"], block_number)

    base_pool = pool_base_address(record)
    if not base_pool:
        return None

    meta_coins = [coin for coin in record.get("coins", []) if not coin.get("isBasePoolLpToken")]
    base_record = records_by_pool.get(lower_address(base_pool), {})
    base_coins = base_record.get("underlying_coins") or base_record.get("coins") or []
    return meta_coins + make_coins(w3, base_coins, block_number)


def fill_pool_metadata(w3, record, records_by_pool):
    args = deployment_args(record)
    details = record.get("deployment_details") or {}
    if details.get("timestamp") is not None:
        details["block_datetime"] = utc_datetime(details["timestamp"])
        details["block_datetime_utc"] = utc_datetime_plain(details["timestamp"])

    set_value(record, "name", args.get("_name"))
    set_value(record, "symbol", args.get("_symbol"))

    coins = pool_coins(w3, record, records_by_pool)
    if coins:
        record["coins"] = coins
        record["n_coins"] = len(coins)

    set_value(record, "base_pool_address", pool_base_address(record))
    if "is_meta_pool" not in record:
        record["is_meta_pool"] = bool(record.get("base_pool_address") or record_function(record) == "deploy_metapool")

    if args.get("poolType") is not None:
        record["pool_type"] = args["poolType"]
    elif record.get("pool_type") is None:
        record["pool_type"] = {
            "deploy_metapool": "stableswap_meta",
            "deploy_plain_pool": "stableswap_plain",
            "deploy_pool": "cryptoswap",
        }.get(record_function(record))

    set_value(record, "registry_id", args.get("registryId"))
    if args.get("implementationType") is not None:
        record["implementation_type"] = args["implementationType"]
    elif not record.get("listed_in_factories") and record_function(record) == "direct_constructor_unparsed":
        record["implementation_type"] = "direct"
    elif record.get("implementation_type") is None and record.get("listed_in_factories"):
        record["implementation_type"] = "factory"

    set_value(record, "asset_type", arg(args, "_asset_type", "assetType"))
    set_value(record, "asset_type_name", args.get("assetTypeName"))
    set_value(record, "asset_types", args.get("_asset_types"))
    set_value(record, "initial_A", arg(args, "_A", "A", "amplificationCoefficient"))
    set_value(record, "fee", args.get("_fee"))
    set_value(record, "admin_fee", args.get("admin_fee"))
    set_value(record, "gamma", args.get("gamma"))
    set_value(record, "mid_fee", args.get("mid_fee"))
    set_value(record, "out_fee", args.get("out_fee"))
    set_value(record, "allowed_extra_profit", args.get("allowed_extra_profit"))
    set_value(record, "fee_gamma", args.get("fee_gamma"))
    set_value(record, "adjustment_step", args.get("adjustment_step"))
    set_value(record, "offpeg_fee_multiplier", args.get("_offpeg_fee_multiplier"))
    set_value(record, "ma_exp_time", arg(args, "_ma_exp_time", "ma_exp_time"))
    set_value(record, "ma_half_time", args.get("ma_half_time"))
    set_value(record, "method_id", args.get("_method_id"))
    set_value(record, "method_ids", args.get("_method_ids"))
    set_value(record, "oracle", args.get("_oracle"))
    set_value(record, "oracles", args.get("_oracles"))
    set_value(record, "weth_address", normalize_address(args.get("_weth")) if args.get("_weth") else None)
    set_value(record, "initial_price", args.get("initial_price"))
    set_value(record, "initial_prices", args.get("initial_prices"))


def refresh_pool_derived_fields(w3, output):
    records_by_pool = pool_records_by_address(output)
    for record in output.get("pools", []):
        fill_pool_metadata(w3, record, records_by_pool)

    records_by_pool = pool_records_by_address(output)
    for record in output.get("pools", []):
        coins = underlying_coins(w3, record, records_by_pool)
        if coins:
            record["underlying_coins"] = coins


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

    if "math_implementation" in names:
        value, error = safe_call_contract(contract, "math_implementation", [], block_number)
        address = normalize_address(value) if not error else None
        if address:
            result["math_implementation_address"] = address

    if "views_implementation" in names:
        value, error = safe_call_contract(contract, "views_implementation", [])
        address = normalize_address(value) if not error else None
        if address:
            result["views_implementation_address"] = address

    return result


def refresh_current_views_implementations(w3, output):
    views_by_factory = {}
    for record in output.get("pools", []):
        factory = record_factory(record)
        if not factory or factory in views_by_factory:
            continue
        abi, _, _ = get_abi(factory)
        if not abi or "views_implementation" not in function_names(abi):
            views_by_factory[factory] = None
            continue
        contract = contract_for(w3, factory, abi)
        value, error = safe_call_contract(contract, "views_implementation", [])
        views_by_factory[factory] = normalize_address(value) if not error else None

    changed = 0
    for record in output.get("pools", []):
        factory = record_factory(record)
        views = views_by_factory.get(factory)
        if views and record.get("views_implementation_address") != views:
            record["views_implementation_address"] = views
            changed += 1
    return changed


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
    addr = normalize_address(address)
    if kind == "pool_implementations":
        return POOL_IMPL_DIR / addr
    if kind == "math_implementations":
        return MATH_IMPL_DIR / addr
    if kind == "views_implementations":
        return VIEWS_IMPL_DIR / addr
    raise ValueError(f"unknown kind: {kind}")


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
                "pool_addresses": [],
            },
        )
        pool_address = record.get("pool_address")
        item["used_by_pool_count"] += 1
        if pool_address:
            item["pool_addresses"].append(pool_address)
        if len(item["example_pools"]) < 5:
            item["example_pools"].append(pool_address)
        block_number = record_block_number(record)
        if block_number < item["first_seen_block"]:
            item["first_seen_block"] = block_number
            item["first_seen_pool"] = pool_address
            item["first_seen_factory"] = record_factory(record)
    return usage


def usage_metadata(usage):
    keys = [
        "used_by_pool_count",
        "first_seen_pool",
        "first_seen_factory",
        "first_seen_block",
        "example_pools",
    ]
    return {key: usage[key] for key in keys if key in usage}


def source_compare_key(source_record):
    raw = source_record.get("raw") or {}
    abi_text = raw.get("ABI") or ""
    try:
        abi_key = json.dumps(json.loads(abi_text), sort_keys=True, separators=(",", ":"))
    except Exception:
        abi_key = abi_text.strip()
    source_code = (raw.get("SourceCode") or "").replace("\r\n", "\n").replace("\r", "\n")
    return (
        source_code,
        abi_key,
        raw.get("ContractName") or "",
        raw.get("CompilerVersion") or "",
        raw.get("OptimizationUsed") or "",
        raw.get("Runs") or "",
        raw.get("EVMVersion") or "",
    )


def pool_source_fallback(usage):
    pools = usage.get("pool_addresses") or usage.get("example_pools") or []
    checked = []
    unverified = []
    mismatched = []
    first_pool = None
    first_record = None
    first_key = None

    for pool in pools:
        pool = normalize_address(pool)
        if not pool:
            continue
        record = get_source(pool)
        if record.get("status") != "verified":
            unverified.append(pool)
            continue
        key = source_compare_key(record)
        checked.append(pool)
        if first_record is None:
            first_pool = pool
            first_record = record
            first_key = key
        elif key != first_key:
            mismatched.append(pool)

    if not first_record or mismatched:
        return None
    return {
        "source_record": first_record,
        "source_pool_address": first_pool,
        "checked_pool_addresses": checked,
        "unverified_pool_addresses": unverified,
    }


def artifact_is_complete(kind, address):
    out_dir = address_artifact_dir(kind, address)
    meta_path = out_dir / "metadata.json"
    abi_path = out_dir / "abi.json"
    if not meta_path.exists() or not abi_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return False
    if meta.get("source_status") == "verified":
        for sf in meta.get("source_files") or []:
            if not (out_dir / sf).exists():
                return False
    return True


def ensure_address_artifact(kind, address, usage):
    if artifact_is_complete(kind, address):
        out_dir = address_artifact_dir(kind, address)
        meta = json.loads((out_dir / "metadata.json").read_text())
        return {
            "address": address,
            "used_by_pool_count": usage.get("used_by_pool_count", 0),
            "artifact_dir": str(out_dir),
            "abi_status": meta.get("abi_status"),
            "source_status": meta.get("source_status"),
            "source_file_count": meta.get("source_file_count"),
            "source_files": meta.get("source_files"),
        }
    return write_address_artifact(kind, address, usage)


def write_address_artifact(kind, address, usage):
    address = normalize_address(address)
    out_dir = address_artifact_dir(kind, address)
    out_dir.mkdir(parents=True, exist_ok=True)

    old_runtime_code = out_dir / "runtime_code.hex"
    if old_runtime_code.exists():
        old_runtime_code.unlink()

    source_record = get_source(address)
    source_status = source_record.get("status")
    source_source = "etherscan_getsourcecode"
    fallback = None
    if kind == "pool_implementations" and source_status != "verified":
        fallback = pool_source_fallback(usage)
        if fallback:
            source_record = fallback["source_record"]
            source_status = source_record.get("status")
            source_source = f"etherscan_getsourcecode_pool:{fallback['source_pool_address']}"

    source_files = []
    if source_status == "verified":
        source_files = write_source_files(out_dir, source_record)
    elif (out_dir / "source").exists():
        shutil.rmtree(out_dir / "source")

    abi_status, abi_source = write_abi(out_dir, address, source_record)
    if fallback and abi_status == "source_getsourcecode":
        abi_source = source_source

    metadata = {
        "address": address,
        "kind": kind.rstrip("s"),
        "written_at": now_utc(),
        "abi_status": abi_status,
        "abi_source": abi_source,
        "source_status": source_status,
        "source_source": source_source,
        "source_files": source_files,
        "source_file_count": len(source_files),
        "etherscan_source_metadata": source_metadata(source_record.get("raw") or {}),
        **usage_metadata(usage),
    }
    if kind == "pool_implementations":
        abi = json.loads((out_dir / "abi.json").read_text())
        metadata["loader_call_manifest"] = build_loader_call_manifest(abi)
    if fallback:
        metadata.update({
            "source_pool_address": fallback["source_pool_address"],
            "source_pool_count_checked": len(fallback["checked_pool_addresses"]),
            "source_pool_addresses_checked": fallback["checked_pool_addresses"],
            "source_pool_unverified_addresses": fallback["unverified_pool_addresses"],
        })
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
        "pool_implementations": [],
        "math_implementations": [],
        "views_implementations": [],
    }
    for kind, field in (
        ("pool_implementations", "implementation_address"),
        ("math_implementations", "math_implementation_address"),
        ("views_implementations", "views_implementation_address"),
    ):
        for address, usage in sorted(collect_usage(output.get("pools", []), field).items(), key=lambda item: item[0].lower()):
            output["unique_addresses"][kind].append(ensure_address_artifact(kind, address, usage))


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
        "block_datetime_utc": utc_datetime_plain(timestamp),
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
        "last_scanned_at_utc": now_utc_plain(),
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
    output.setdefault("unique_addresses", {"pool_implementations": [], "math_implementations": [], "views_implementations": []})

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    POOL_IMPL_DIR.mkdir(parents=True, exist_ok=True)
    MATH_IMPL_DIR.mkdir(parents=True, exist_ok=True)
    VIEWS_IMPL_DIR.mkdir(parents=True, exist_ok=True)
    processed_indices = processed_factory_indices(output)
    records_by_pool = pool_records_by_address(output)
    total_changed = 0

    log(f"factories={len(factories)} existing_records={len(records_by_pool)}")
    for factory in factories:
        total_changed += process_factory(w3, factory, output, processed_indices, records_by_pool, factory_set, factories)
        save_output(output, factories)

    if total_changed == 0:
        log("no new factory indices")

    # Merge hardcoded pools
    hardcoded_data = read_json(HARD_CODED_PATH, {"metadata": {}, "factories": {}, "pools": []})
    existing_keys = {lower_address(r["pool_address"]) for r in output["pools"]}
    hardcoded_added = 0
    for record in hardcoded_data.get("pools", []):
        key = lower_address(record.get("pool_address"))
        if not key or key in existing_keys:
            continue
        output["pools"].append(compact_pool_record(record))
        existing_keys.add(key)
        hardcoded_added += 1
    if hardcoded_added:
        log(f"hardcoded pools merged: added={hardcoded_added} "
            f"skipped_duplicates={len(hardcoded_data.get('pools', [])) - hardcoded_added}")
        save_output(output, factories)

    log("refreshing pool derived fields")
    refresh_pool_derived_fields(w3, output)
    save_output(output, factories)

    views_changed = refresh_current_views_implementations(w3, output)
    if views_changed:
        log(f"factory views implementations refreshed: changed={views_changed}")
        save_output(output, factories)

    log("refreshing implementation artifacts (pool / math / views)")
    refresh_unique_artifacts(output)
    save_output(output, factories)

    log("\nwritten:")
    log(f"output={OUTPUT_PATH}")
    log(f"cache={CACHE_DIR}")
    log(f"pool_implementations={POOL_IMPL_DIR}")
    log(f"math_implementations={MATH_IMPL_DIR}")
    log(f"views_implementations={VIEWS_IMPL_DIR}")
    log(f"pool_count={output['metadata']['unique_pool_count']}")
    log(f"resolved={output['metadata']['resolved_count']}")
    log(f"unresolved={output['metadata']['unresolved_count']}")
    log(f"conflicts={output['metadata']['conflict_count']}")
    log(f"unique_pool_implementations={output['metadata']['unique_pool_implementation_count']}")
    log(f"unique_math_implementations={output['metadata']['unique_math_implementation_count']}")
    log(f"unique_views_implementations={output['metadata']['unique_views_implementation_count']}")


if __name__ == "__main__":
    main()
